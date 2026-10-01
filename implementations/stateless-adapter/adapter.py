#!/usr/bin/env python3
"""Illustrative ASP adapter for a stateless (serverless-style) agent.

A stateless agent exists only for the duration of one invocation: between
invocations there is no process that could send heartbeats, and it keeps no
record of its tasks. Following the proposal, a component that persists across
invocations reports on its behalf. Here that component is this adapter, which
plays the role of the orchestrator or hosting platform:

  - it registers the *logical* agent once, with a stable agentId;
  - it sends heartbeats on the agent's behalf, including while no instance runs;
  - it reports one task per invocation, with a single seq counter for all instances.

The "agent" itself is the stateless function `handle`, invoked in short-lived
threads that stand in for serverless instances. It knows nothing about ASP.

Example:
  python3 adapter.py --server http://127.0.0.1:8080
"""

import argparse
import os
import random
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python-agent"))
from agent import AspClient  # noqa: E402


def handle(request):
    """The stateless agent: one invocation, no memory, no ASP."""
    time.sleep(random.uniform(1, 6))
    if random.random() < 0.1:
        raise RuntimeError("model call failed")
    return f"summary of {request}"


class Adapter:
    def __init__(self, asp, max_instances):
        self.asp = asp
        self.max_instances = max_instances
        self.lock = threading.Lock()
        self.in_flight = 0
        self.progress = 0
        self.counter = 0

    def invoke(self, request):
        with self.lock:
            if self.in_flight >= self.max_instances:
                return False
            self.in_flight += 1
            self.counter += 1
            task_id = f"inv-{self.counter:05d}"
            self.asp.send("state_change", dimension="task", **{"from": "submitted", "to": "working"}, taskId=task_id)
        threading.Thread(target=self._run, args=(task_id, request), daemon=True).start()
        return True

    def _run(self, task_id, request):
        try:
            handle(request)
            outcome, reason = "completed", None
        except RuntimeError:
            outcome, reason = "failed", "model-call-failed"
        with self.lock:
            fields = {"reason": reason} if reason else {}
            self.asp.send("state_change", dimension="task", **{"from": "working", "to": outcome}, taskId=task_id, **fields)
            self.in_flight -= 1
            self.progress += 1

    def heartbeat(self):
        with self.lock:
            if self.in_flight == 0:
                lifecycle = "idle"
            elif self.in_flight < self.max_instances:
                lifecycle = "busy"
            else:
                lifecycle = "at-capacity"
            self.asp.send("heartbeat", lifecycle=lifecycle, health="healthy", progressSeq=self.progress,
                          metrics={"activeTasks": self.in_flight, "maxConcurrentTasks": self.max_instances})


def main():
    parser = argparse.ArgumentParser(description="Illustrative ASP adapter for a stateless agent")
    parser.add_argument("--server", default=os.environ.get("ASP_SERVER", "http://127.0.0.1:8080"))
    parser.add_argument("--token", default=os.environ.get("ASP_TOKEN"))
    parser.add_argument("--id", default="urn:agent:example:summarizer-fn")
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--max-instances", type=int, default=3)
    args = parser.parse_args()

    asp = AspClient(args.server, args.id, args.token)
    asp.send("register", name="Summarizer (stateless)", version="0.1.0", protocols=["a2a"],
             capabilities=["summarization"], heartbeatIntervalSec=args.interval)
    adapter = Adapter(asp, args.max_instances)
    print(f"adapter registered logical agent {args.id}")

    last_heartbeat = 0.0
    try:
        while True:
            if random.random() < 0.4:
                adapter.invoke(f"document-{random.randint(1, 999)}")
            if time.time() - last_heartbeat >= args.interval:
                adapter.heartbeat()
                last_heartbeat = time.time()
            time.sleep(1)
    except KeyboardInterrupt:
        asp.send("deregister", reason="scheduled-shutdown")
        print("\nderegistered")


if __name__ == "__main__":
    main()
