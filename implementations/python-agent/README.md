# Illustrative agent (Python)

A minimal agent that reports its state with ASP. It does no real AI work: it simulates tasks so that you can see what an agent would report. Standard library only.

## Run

Start the status server first, then:

```bash
python3 agent.py
```

Options:

| Option | Effect |
|---|---|
| `--id`, `--name` | Agent identifier and display name |
| `--interval N` | Heartbeat interval in seconds (default 5) |
| `--stall-after N` | Declared `stallAfterSec` (default 20) |
| `--stall` | Keeps sending heartbeats but stops making progress, to trigger `stalled` |
| `--crash-after N` | Exits abruptly after N seconds without `deregister`, to trigger `unreachable` |
| `--server`, `--token` | Status server URL and bearer token (also `ASP_SERVER`, `ASP_TOKEN`). Without `--token` and `ASP_TOKEN`, on macOS the token is read from the Keychain item `asp-token` |

## What it shows

- `register` with a declared heartbeat interval, then heartbeats sent from the main loop, carrying `progressSeq` and an `activity` hint.
- Task transitions `submitted → working → input-required → working → completed`, each with its `taskId`.
- Lifecycle `idle` when there is no task and `at-capacity` while its single task runs, since this agent handles one task at a time.
- `deregister` on `Ctrl+C`.

## Change an agent's governance

`governance.py` acts as an operator. Without arguments it lists the agents; with an agent id and a state it changes that agent's governance:

```bash
python3 governance.py
python3 governance.py urn:agent:example:research-assistant quarantined
python3 governance.py urn:agent:example:research-assistant normal
```

States: `normal`, `sandboxed`, `quarantined`, `policy-violation`, `under-review`. It takes `--server` and `--token` like the agent. The example agent does not read its governance, so it keeps working; only what the server and the observer report changes.

`AspClient` in `agent.py` is a small reusable client, also used by the stateless adapter example.
