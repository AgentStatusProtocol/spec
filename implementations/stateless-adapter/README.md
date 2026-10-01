# Adapter for a stateless agent (Python)

A stateless agent, such as a serverless function, exists only for one invocation. Between invocations nothing runs that could send heartbeats, several short-lived instances may serve the same logical agent, and no instance remembers the tasks of the others.

The proposal recommends that a component that persists across invocations report on the agent's behalf. In this example that component is `adapter.py`, which stands in for the orchestrator or hosting platform:

- it registers the **logical** agent once, with a stable `agentId`;
- it sends heartbeats for it, reporting `idle`, `busy` or `at-capacity` from the number of instances in flight;
- it reports one task per invocation (`working → completed` or `failed`), using a single `seq` counter for all instances.

The agent itself is the function `handle`, which knows nothing about ASP.

## Run

Start the status server first, then:

```bash
python3 adapter.py
```

Options: `--id`, `--interval`, `--max-instances`, `--server`, `--token`.
