#!/usr/bin/env python3
"""Minimal, illustrative ASP v0.1 agent.

Python standard library only. The agent registers with a status server, sends
heartbeats from its main loop, and walks a few simulated tasks through their
states. It does no real AI work: it only shows what an agent would report.

Examples:
  python3 agent.py
  python3 agent.py --name "Planner" --id urn:agent:example:planner --interval 5
  python3 agent.py --stall        keeps sending heartbeats but stops making progress
  python3 agent.py --crash-after 30   stops abruptly after 30 s, without deregister
"""

import argparse
import json
import os
import random
import time
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AspClient:
    def __init__(self, server, agent_id, token=None):
        self.server = server.rstrip("/")
        self.agent_id = agent_id
        self.token = token
        self.seq = 0

    def send(self, event_type, **fields):
        self.seq += 1
        event = {
            "aspVersion": "0.1",
            "type": event_type,
            "agentId": self.agent_id,
            "eventId": str(uuid.uuid4()),
            "seq": self.seq,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **fields,
        }
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(f"{self.server}/v1/events", data=json.dumps(event).encode(), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=5) as response:
                return response.status
        except HTTPError as err:
            print(f"  server rejected {event_type}: {err.code} {err.read().decode()}")
            return err.code
        except URLError as err:
            print(f"  status server not reachable: {err.reason}")
            return None


def main():
    parser = argparse.ArgumentParser(description="Illustrative ASP agent")
    parser.add_argument("--server", default=os.environ.get("ASP_SERVER", "http://127.0.0.1:8080"))
    parser.add_argument("--token", default=os.environ.get("ASP_TOKEN"))
    parser.add_argument("--id", default="urn:agent:example:research-assistant")
    parser.add_argument("--name", default="Research Assistant")
    parser.add_argument("--interval", type=int, default=5, help="heartbeat interval in seconds")
    parser.add_argument("--stall-after", type=int, default=20, help="declared stallAfterSec")
    parser.add_argument("--stall", action="store_true", help="stop making progress after the first task starts")
    parser.add_argument("--crash-after", type=int, default=0, help="exit abruptly after N seconds, without deregister")
    args = parser.parse_args()

    asp = AspClient(args.server, args.id, args.token)
    asp.send("register", name=args.name, version="0.1.0", protocols=["a2a"],
             capabilities=["summarization"], heartbeatIntervalSec=args.interval,
             stallAfterSec=args.stall_after)
    print(f"{args.name} registered as {args.id}")

    started = time.time()
    progress = 0
    task_number = 0
    task_id, task_state, task_step = None, None, 0
    last_heartbeat = 0.0

    try:
        while True:
            if args.crash_after and time.time() - started > args.crash_after:
                print("simulated crash: exiting without deregister")
                os._exit(1)

            if task_id is None and random.random() < 0.3:
                task_number += 1
                task_id, task_step = f"t-{task_number:04d}", 0
                asp.send("state_change", dimension="task", **{"from": "submitted", "to": "working"},
                         taskId=task_id, maxDurationSec=120)
                task_state = "working"
                print(f"task {task_id}: submitted -> working")

            if task_id is not None and not args.stall:
                progress += 1
                task_step += 1
                if task_step == 3 and random.random() < 0.5:
                    asp.send("state_change", dimension="task", **{"from": "working", "to": "input-required"},
                             taskId=task_id, reason="clarification-needed")
                    task_state = "input-required"
                    print(f"task {task_id}: working -> input-required")
                elif task_state == "input-required" and task_step == 5:
                    asp.send("state_change", dimension="task", **{"from": "input-required", "to": "working"}, taskId=task_id)
                    task_state = "working"
                    print(f"task {task_id}: input-required -> working")
                elif task_step >= 7:
                    asp.send("state_change", dimension="task", **{"from": task_state, "to": "completed"}, taskId=task_id)
                    print(f"task {task_id}: {task_state} -> completed")
                    task_id, task_state = None, None

            if time.time() - last_heartbeat >= args.interval:
                busy = task_id is not None
                beat = {
                    # This agent handles one task at a time, so a running task leaves it at capacity.
                    "lifecycle": "at-capacity" if busy else "idle",
                    "health": "healthy",
                    "progressSeq": progress,
                    "metrics": {"activeTasks": 1 if busy else 0, "maxConcurrentTasks": 1},
                }
                if busy:
                    beat["activity"] = "model-call" if task_state == "working" else "waiting-input"
                asp.send("heartbeat", **beat)
                last_heartbeat = time.time()

            time.sleep(1)
    except KeyboardInterrupt:
        asp.send("deregister", reason="scheduled-shutdown")
        print("\nderegistered")


if __name__ == "__main__":
    main()
