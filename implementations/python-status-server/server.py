#!/usr/bin/env python3
"""Minimal, illustrative ASP v0.1 status server.

Python standard library only. Implements the reference HTTP + SSE binding in a
simplified form, to make the proposal concrete. It is not a conformance-tested
reference implementation and is not intended for production use.

Implemented:
  POST /v1/events, POST /v1/events:batch        ingest
  GET  /v1/agents, GET /v1/agents/{agentId}     snapshot
  GET  /v1/stream[?agentId=...]                 SSE, with Last-Event-ID replay
  GET  /                                        live observer page
  - register-before-anything (409), eventId deduplication, seq ordering per sender
  - heartbeat timeout (2.5 x declared interval) -> lifecycle "unreachable"
  - progress detection (stallAfterSec + progressSeq) -> health "stalled"
  - terminal task states cannot transition (409)
  - governance precedence: operator > guardrail > self
  - optional bearer token (ASP_TOKEN), accepted as header or ?token= query

Not implemented: OTLP ingest, active probing, event signing, rate limiting.
"""

import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

HOST = os.environ.get("ASP_HOST", "127.0.0.1")
PORT = int(os.environ.get("ASP_PORT", "8080"))
TOKEN = os.environ.get("ASP_TOKEN")
TIMEOUT_MULTIPLIER = 2.5
HISTORY_SIZE = 5000
MAX_EVENT_BYTES = 16 * 1024

TERMINAL_TASK_STATES = {"completed", "failed", "canceled", "rejected", "timed-out"}
PROGRESS_TASK_STATES = {"working", "retrying"}
SERVER_ONLY_STATES = {"unreachable", "stalled"}
AUTHORITY = {"self": 0, "peer": 0, "supervisor": 1, "guardrail": 2, "operator": 3, "server": 3}
EVENT_TYPES = {"register", "heartbeat", "state_change", "floor_change", "deregister"}

lock = threading.Lock()
agents = {}
seen_event_ids = set()
history = deque(maxlen=HISTORY_SIZE)
server_seq = 0
subscribers = []


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Rejected(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def publish(event):
    """Append an event to history and push it to all SSE subscribers."""
    global server_seq
    server_seq += 1
    item = (server_seq, event)
    history.append(item)
    for queue in list(subscribers):
        queue.append(item)


def new_agent(event):
    return {
        "agentId": event["agentId"],
        "name": event.get("name", event["agentId"]),
        "heartbeatIntervalSec": event["heartbeatIntervalSec"],
        "stallAfterSec": event.get("stallAfterSec"),
        "lifecycle": "registering",
        "health": "healthy",
        "healthSelfReported": "healthy",
        "peerHealth": None,
        "governance": "normal",
        "governanceSetBy": "self",
        "tasks": {},
        "conversations": {},
        "metrics": {},
        "activity": None,
        "progressSeq": None,
        "lastProgressAt": time.time(),
        "lastSelfEventAt": time.time(),
        "lastSeq": {},
        "stale": False,
        "monitored": True,
    }


def validate(event):
    for field in ("aspVersion", "type", "agentId", "eventId", "seq", "ts"):
        if field not in event:
            raise Rejected(400, f"missing field: {field}")
    if event["type"] not in EVENT_TYPES:
        raise Rejected(400, f"unknown type: {event['type']}")
    source = event.get("source", "self")
    if source not in AUTHORITY or source == "server":
        raise Rejected(400, f"invalid source: {source}")
    if event["type"] == "register":
        interval = event.get("heartbeatIntervalSec")
        if not isinstance(interval, int) or not 1 <= interval <= 300:
            raise Rejected(400, "heartbeatIntervalSec must be an integer between 1 and 300")
    if event["type"] == "state_change":
        for field in ("dimension", "from", "to"):
            if field not in event:
                raise Rejected(400, f"missing field: {field}")
        if event["dimension"] == "task" and "taskId" not in event:
            raise Rejected(400, "taskId is required for the task dimension")
    if event["type"] == "floor_change":
        for field in ("conversationId", "from", "to"):
            if field not in event:
                raise Rejected(400, f"missing field: {field}")
    reported = {event.get("lifecycle"), event.get("health"), event.get("to")}
    if source == "self" and reported & SERVER_ONLY_STATES:
        raise Rejected(400, "unreachable and stalled are server-assigned")


def ingest(event):
    """Apply one event. Returns nothing on success, raises Rejected otherwise."""
    validate(event)
    with lock:
        if event["eventId"] in seen_event_ids:
            return
        agent_id = event["agentId"]
        source = event.get("source", "self")
        agent = agents.get(agent_id)

        if event["type"] == "register":
            if agent is None or not agent["monitored"]:
                agent = agents[agent_id] = new_agent(event)
        elif agent is None:
            raise Rejected(409, "event sent before register")

        last = agent["lastSeq"].get(source, -1)
        if event["seq"] <= last:
            seen_event_ids.add(event["eventId"])
            publish(dict(event, outOfOrder=True))
            return

        was_unreachable = agent["lifecycle"] == "unreachable"
        apply(agent, event, source)
        seen_event_ids.add(event["eventId"])
        agent["lastSeq"][source] = event["seq"]
        publish(event)

        if source == "self":
            agent["lastSelfEventAt"] = time.time()
            if was_unreachable and agent["monitored"]:
                if agent["lifecycle"] == "unreachable":
                    agent["lifecycle"] = "online"
                agent["stale"] = False
                publish(server_event(agent, "lifecycle", "unreachable", agent["lifecycle"], "event-received"))


def apply(agent, event, source):
    kind = event["type"]
    if kind == "register":
        agent["lifecycle"] = "online"
    elif kind == "heartbeat":
        if "lifecycle" in event:
            agent["lifecycle"] = event["lifecycle"]
        if "health" in event:
            agent["healthSelfReported"] = event["health"]
            if agent["health"] != "stalled":
                agent["health"] = event["health"]
        agent["metrics"] = event.get("metrics", agent["metrics"])
        agent["activity"] = event.get("activity")
        progress = event.get("progressSeq")
        if progress is not None and (agent["progressSeq"] is None or progress > agent["progressSeq"]):
            agent["progressSeq"] = progress
            agent["lastProgressAt"] = time.time()
            if agent["health"] == "stalled":
                agent["health"] = agent["healthSelfReported"]
                publish(server_event(agent, "health", "stalled", agent["health"], "progress-resumed"))
    elif kind == "state_change":
        dim, to = event["dimension"], event["to"]
        if dim == "task":
            task_id = event["taskId"]
            current = agent["tasks"].get(task_id)
            if current in TERMINAL_TASK_STATES:
                raise Rejected(409, f"task {task_id} is in terminal state {current}")
            agent["tasks"][task_id] = to
            if to in PROGRESS_TASK_STATES:
                agent["lastProgressAt"] = time.time()
        elif dim == "governance":
            if AUTHORITY[source] < AUTHORITY[agent["governanceSetBy"]]:
                raise Rejected(409, f"governance set by {agent['governanceSetBy']} cannot be changed by {source}")
            agent["governance"] = to
            agent["governanceSetBy"] = source
        elif dim == "health":
            if source == "peer":
                agent["peerHealth"] = {"state": to, "reason": event.get("reason"), "ts": event["ts"]}
            else:
                agent["healthSelfReported"] = to
                agent["health"] = to
        elif dim == "lifecycle":
            agent["lifecycle"] = to
    elif kind == "floor_change":
        agent["conversations"][event["conversationId"]] = {"state": event["to"], "role": event.get("role")}
    elif kind == "deregister":
        agent["lifecycle"] = "offline"
        agent["monitored"] = False


def server_event(agent, dimension, old, new, reason):
    return {
        "aspVersion": "0.1",
        "type": "state_change",
        "agentId": agent["agentId"],
        "source": "server",
        "dimension": dimension,
        "from": old,
        "to": new,
        "reason": reason,
        "ts": now_iso(),
    }


def liveness_loop():
    while True:
        time.sleep(1)
        with lock:
            for agent in agents.values():
                if not agent["monitored"]:
                    continue
                silence = time.time() - agent["lastSelfEventAt"]
                limit = TIMEOUT_MULTIPLIER * agent["heartbeatIntervalSec"]
                if agent["lifecycle"] != "unreachable" and silence > limit:
                    old = agent["lifecycle"]
                    agent["lifecycle"] = "unreachable"
                    agent["stale"] = True
                    publish(server_event(agent, "lifecycle", old, "unreachable", f"no-heartbeat-{int(silence)}s"))
                stall = agent["stallAfterSec"]
                waiting = any(state in PROGRESS_TASK_STATES for state in agent["tasks"].values())
                if stall and waiting and agent["health"] != "stalled" and agent["lifecycle"] != "unreachable":
                    if time.time() - agent["lastProgressAt"] > stall:
                        old = agent["health"]
                        agent["health"] = "stalled"
                        publish(server_event(agent, "health", old, "stalled", f"no-progress-{stall}s"))


def snapshot(agent):
    visible = {k: v for k, v in agent.items() if k not in ("lastSeq", "lastProgressAt", "lastSelfEventAt")}
    visible["lastEventAgeSec"] = round(time.time() - agent["lastSelfEventAt"], 1)
    return visible


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def authorised(self):
        if not TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        query = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        return header == f"Bearer {TOKEN}" or query == TOKEN

    def send_json(self, code, body):
        data = json.dumps(body, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self.authorised():
            return self.send_json(401, {"error": "unauthorized"})
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_EVENT_BYTES * (50 if path.endswith(":batch") else 1):
            return self.send_json(413, {"error": "event too large"})
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return self.send_json(400, {"error": "malformed JSON"})
        if path == "/v1/events":
            events = [body]
        elif path == "/v1/events:batch" and isinstance(body, list):
            events = body
        else:
            return self.send_json(404, {"error": "not found"})
        try:
            for event in events:
                ingest(event)
        except Rejected as err:
            return self.send_json(err.code, {"error": err.message})
        self.send_json(202, {"accepted": len(events)})

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            page = (Path(__file__).parent / "observer.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            return self.wfile.write(page)
        if not self.authorised():
            return self.send_json(401, {"error": "unauthorized"})
        if url.path == "/v1/agents":
            with lock:
                return self.send_json(200, [snapshot(a) for a in agents.values()])
        if url.path.startswith("/v1/agents/"):
            agent_id = unquote(url.path[len("/v1/agents/"):])
            with lock:
                agent = agents.get(agent_id)
                if agent is None:
                    return self.send_json(404, {"error": "unknown agent"})
                return self.send_json(200, snapshot(agent))
        if url.path == "/v1/stream":
            return self.stream(parse_qs(url.query).get("agentId", [None])[0])
        self.send_json(404, {"error": "not found"})

    def stream(self, agent_filter):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        queue = deque()
        last_id = int(self.headers.get("Last-Event-ID", "0") or 0)
        with lock:
            for item in history:
                if item[0] > last_id:
                    queue.append(item)
            subscribers.append(queue)
        try:
            while True:
                while queue:
                    event_id, event = queue.popleft()
                    if agent_filter and event.get("agentId") != agent_filter:
                        continue
                    lines = [f"event: {event['type']}", f"id: {event_id}", f"data: {json.dumps(event)}", "", ""]
                    self.wfile.write("\n".join(lines).encode())
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with lock:
                subscribers.remove(queue)


def main():
    threading.Thread(target=liveness_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ASP status server on http://{HOST}:{PORT}  (observer page at /)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
