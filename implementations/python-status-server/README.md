# Illustrative status server (Python)

A minimal ASP v0.1 status server written with the Python standard library only. It exists to make the proposal concrete and to support experiments; it is **not** a conformance-tested reference implementation and is not intended for production use.

## Run

```bash
python3 server.py
```

Then open http://127.0.0.1:8080 for the live observer page.

Environment variables: `ASP_HOST` (default `127.0.0.1`), `ASP_PORT` (default `8080`) and `ASP_TOKEN`. When `ASP_TOKEN` is set, every API request must carry `Authorization: Bearer <token>`; the observer page accepts it as `?token=<token>`.

## What it implements

- Ingest: `POST /v1/events` and `POST /v1/events:batch`, with the response codes of the proposal (202, 400, 401, 409).
- Distribution: `GET /v1/agents`, `GET /v1/agents/{agentId}` and the SSE stream `GET /v1/stream`, with `Last-Event-ID` replay and an optional `agentId` filter.
- `register` required before any other event; `eventId` deduplication; `seq` ordering per agent and per sender.
- Heartbeat timeout: no event from the agent itself within 2.5 declared intervals sets `unreachable`, and last known tasks are marked stale.
- Progress: when `stallAfterSec` is declared and `progressSeq` stops increasing while a task is `working` or `retrying`, health becomes `stalled`.
- Terminal task states cannot transition further.
- Governance precedence: `operator` above `guardrail` above `self`; an agent cannot clear a state set by a guardrail or an operator.
- Agents cannot report `unreachable` or `stalled` about themselves.

## What it does not implement

OTLP ingest, active probing, event signing, rate limiting, persistence and credential scoping per agent. State lives in memory and is lost on restart.
