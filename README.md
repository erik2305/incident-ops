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
enforce that identity. Unit tests supply `InMemorySaver`; PostgreSQL checkpointing
uses the official `AsyncPostgresSaver`. The Phase 0 decisions are in `docs/adr/`.

## PostgreSQL integration tests

Compose provides only PostgreSQL, using intentionally local-development
credentials and a named data volume. Start Docker Desktop, then run from the
repository root in PowerShell:

```powershell
docker compose up -d --wait postgres
$env:INCIDENTOPS_TEST_DATABASE_URL = 'postgresql://incidentops_dev:incidentops_dev_only@127.0.0.1:5433/incidentops_test'
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_postgres_checkpointing.py
```

Integration tests initialize schema through LangGraph's public `setup()` API,
use unique thread IDs, and clean up through `adelete_thread()`. They prove recovery
using a fresh saver, graph, connection, and event loop after the writer closes.
They skip when the test URL is absent; a configured connection failure fails.

Run the independent in-memory unit suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit
```

Stop and remove the local service (retaining its data volume):

```powershell
docker compose down
Remove-Item Env:INCIDENTOPS_TEST_DATABASE_URL -ErrorAction SilentlyContinue
```

To also remove the development database data, use `docker compose down --volumes`.

Application callers pass the database URL to `setup_checkpoints(database_url)`
explicitly during provisioning, then use `async with open_checkpointer(database_url)`
and inject that saver into `build_graph(checkpointer=...)`. Schema setup is separate
from opening a saver. Use `ainvoke` and `aget_state` within the open context.
The serializer allows only safe built-in msgpack types and disables pickle fallback.
No application code reads environment variables or falls back to in-memory state.

`psycopg[binary]` supplies the PostgreSQL driver without a system libpq install,
including on Windows. On Windows, async callers need a selector event loop;
the integration tests use `asyncio.Runner(loop_factory=asyncio.SelectorEventLoop)`.
