# IncidentOps

A minimal LangGraph foundation for incident response.

## Development

Create and activate a Python 3.11+ virtual environment, then install:

```text
python -m pip install -e ".[dev]"
```

Run verification from the repository root:

```text
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
git diff --check
```

## Graph contract

`incidentops.graph.build_graph(checkpointer=...)` compiles
`START → initialize_incident → END` with the caller's checkpointer.
Input requires non-empty `incident_id` and `user_report` strings; initialization
adds `status = "investigating"`. Whitespace-only inputs raise `ValueError`.

The caller supplies `config={"configurable": {"thread_id": "INC-001"}}` to
`graph.invoke(...)` and can inspect checkpoints using `graph.get_state(config)`.
The eventual architecture uses `thread_id = incident_id`; this slice does not
enforce that identity. Unit tests supply `InMemorySaver`; production checkpointing
is deferred. The Phase 0 decisions are recorded in `docs/adr/`.
