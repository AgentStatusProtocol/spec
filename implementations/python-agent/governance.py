#!/usr/bin/env python3
"""Read or change an agent's governance state as an operator.

Python standard library only. The token is taken from --token, then ASP_TOKEN,
then (on macOS) the Keychain item asp-token, as for the example agent.

Examples:
  python3 governance.py --server URL
      lists the agents with their lifecycle, health and governance
  python3 governance.py --server URL urn:agent:example:research-assistant quarantined
      puts the agent in quarantine
  python3 governance.py --server URL urn:agent:example:research-assistant normal
      releases it
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from agent import default_token

STATES = ["normal", "sandboxed", "quarantined", "policy-violation", "under-review"]


def call(server, token, path, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = Request(server + path, data=data, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urlopen(request, timeout=90) as response:
            return response.status, json.loads(response.read() or b"null")
    except HTTPError as err:
        try:
            detail = json.loads(err.read()).get("error", "")
        except ValueError:
            detail = ""
        return err.code, detail
    except URLError as err:
        sys.exit(f"Status server not reachable: {err.reason}")


def fail(code, detail):
    if code == 401:
        sys.exit("The server did not accept the token. Check --token, ASP_TOKEN or the Keychain item asp-token.")
    if code == 404:
        sys.exit("No agent with that id on the server. Run the command without arguments to list the agents.")
    sys.exit(f"The server answered {code}: {detail}")


def list_agents(server, token):
    code, agents = call(server, token, "/v1/agents")
    if code != 200:
        fail(code, agents)
    if not agents:
        print("No agents on the server.")
    for a in agents:
        print(f"{a['agentId']}  ({a['name']})")
        print(f"    lifecycle {a['lifecycle']}, health {a['health']}, governance {a['governance']} (set by {a['governanceSetBy']})")


def set_governance(server, token, agent_id, state):
    path = "/v1/agents/" + quote(agent_id, safe="")
    for attempt in range(2):
        code, agent = call(server, token, path)
        if code != 200:
            fail(code, agent)
        current = agent["governance"]
        if current == state:
            print(f"{agent_id} is {state}.")
            return
        event = {
            "aspVersion": "0.1",
            "type": "state_change",
            "agentId": agent_id,
            "eventId": str(uuid.uuid4()),
            "seq": int(time.time()),
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "operator",
            "dimension": "governance",
            "from": current,
            "to": state,
            "reason": "operator-request",
        }
        code, detail = call(server, token, "/v1/events", event)
        if code != 202:
            fail(code, detail)
        # The server ignores an operator event whose seq (the current second) is not
        # higher than the previous one. If that happened, wait a second and try again.
        time.sleep(1.1)
    code, agent = call(server, token, path)
    if code == 200 and agent["governance"] == state:
        print(f"{agent_id} is {state}.")
    else:
        sys.exit("The change was not applied. Try again in a few seconds.")


def main():
    parser = argparse.ArgumentParser(description="Read or change an agent's governance state as an operator")
    parser.add_argument("agent_id", nargs="?", help="agent to change; without it, the agents are listed")
    parser.add_argument("state", nargs="?", choices=STATES, help="new governance state")
    parser.add_argument("--server", default=os.environ.get("ASP_SERVER", "http://127.0.0.1:8080"))
    parser.add_argument("--token", help="bearer token; default: ASP_TOKEN, then the macOS Keychain item asp-token")
    args = parser.parse_args()
    server = args.server.rstrip("/")
    token = args.token or default_token()
    if args.agent_id is None:
        list_agents(server, token)
    elif args.state is None:
        parser.error("give the new state after the agent id: " + ", ".join(STATES))
    else:
        set_governance(server, token, args.agent_id, args.state)


if __name__ == "__main__":
    main()
