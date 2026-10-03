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

The PostgreSQL service uses intentionally local-development
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

## Synthetic checkout environment

The synthetic environment comprises `postgres`, `inventory`, and `checkout`.
The full Compose environment also includes the two read-only MCP servers below.
The two target applications use a shared small Dockerfile with
service-specific source files, Python 3.13, FastAPI, Uvicorn (one worker), and HTTPX.
They keep their runtime state in memory, independently of LangGraph/PostgreSQL.
Restarting an application clears its state. Logs are limited to 100 records;
checkout deployment history retains the latest 100 entries. Business counters
exclude private ops/control traffic and input rejected before entering a handler.

Start the environment and inspect health in PowerShell:

```powershell
docker compose up -d --build --wait
docker compose ps
$checkout = 'http://127.0.0.1:8002'
$inventory = 'http://127.0.0.1:8001'
$order = '{"sku":"SKU-001","quantity":2}'
Invoke-RestMethod "$inventory/inventory/SKU-001"
Invoke-RestMethod "$checkout/checkout" -Method Post -ContentType 'application/json' -Body $order
```

`SKU-001` has 25 items; `SKU-002` has 8. Each unit costs 1999 cents. Checkout uses
a lifespan-managed async HTTP client to reach `http://inventory:8000`. A per-process
lock serializes checkout business requests, deployments, and resets, including
the downstream call (five-second timeout). Inventory protects its own counters
and log buffer independently. This keeps controls and evidence consistent within
the small single-process environment.

Activate the defective implementation and send another real request:

```powershell
Invoke-RestMethod "$checkout/__control/deploy" -Method Post -ContentType 'application/json' -Body '{"version":"v2"}'
try {
    Invoke-RestMethod "$checkout/checkout" -Method Post -ContentType 'application/json' -Body $order
} catch {
    Write-Output "Checkout HTTP status: $([int]$_.Exception.Response.StatusCode)"
}
Invoke-RestMethod "$checkout/__ops/health"
Invoke-RestMethod "$inventory/__ops/health"
Invoke-RestMethod "$checkout/__ops/metrics"
Invoke-RestMethod "$checkout/__ops/logs"
Invoke-RestMethod "$checkout/__ops/deployments"
```

Both process health endpoints remain healthy during checkout application errors.
Each service has `/__ops/health`, `/__ops/metrics`, and `/__ops/logs`; only checkout
has `/__ops/deployments`. Metrics and structured logs derive from business requests,
while deployment history records control operations. Downstream connectivity or
server failures return 502 with a `downstream_error` log, separate from checkout
calculation failures returning 500. No endpoint supplies a root-cause answer.

Restore checkout and then reset both applications:

```powershell
Invoke-RestMethod "$checkout/__control/deploy" -Method Post -ContentType 'application/json' -Body '{"version":"v1"}'
Invoke-RestMethod "$checkout/checkout" -Method Post -ContentType 'application/json' -Body $order
Invoke-RestMethod "$checkout/__ops/metrics"
Invoke-RestMethod "$checkout/__ops/logs"
Invoke-RestMethod "$checkout/__control/reset" -Method Post
Invoke-RestMethod "$inventory/__control/reset" -Method Post
```

Recovery retains prior errors and counters. Reset clears counters/logs and restores
checkout to `v1` with one fresh baseline deployment record. These private controls
exist only for local synthetic environment preparation and reset.

Run the Docker-backed tests explicitly:

```powershell
$env:INCIDENTOPS_TEST_CHECKOUT_URL = $checkout
$env:INCIDENTOPS_TEST_INVENTORY_URL = $inventory
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_synthetic_environment.py
```

The tests use real HTTP, reset each application before/after each test, and skip
when neither synthetic test URL is configured. Configured service failures fail
the tests. With the PostgreSQL test URL above also set, run the complete suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
docker compose down
Remove-Item Env:INCIDENTOPS_TEST_CHECKOUT_URL, Env:INCIDENTOPS_TEST_INVENTORY_URL, Env:INCIDENTOPS_TEST_DATABASE_URL -ErrorAction SilentlyContinue
```

All exposed ports bind to loopback: inventory `8001`, checkout `8002`, PostgreSQL
`5433`. Shutdown retains the PostgreSQL data volume.

## Read-only MCP capabilities

The official MCP Python SDK v2 (`mcp>=2,<3`) provides two independently addressable
`MCPServer` processes over Streamable HTTP:

| Server | Loopback MCP endpoint | Exact tools |
| --- | --- | --- |
| `observability-mcp` | `http://127.0.0.1:8003/mcp` | `get_service_health`, `get_metrics`, `query_logs` |
| `operations-mcp` | `http://127.0.0.1:8004/mcp` | `get_recent_deployments` |

All current MCP tools are read-only. The raw `/__control/*` endpoints remain
synthetic test/demo infrastructure and are not MCP capabilities. LangGraph and
LLMs do not consume MCP yet. No rollback tool is registered.

Observability accepts only `service="checkout"` or `service="inventory"`.
Deployment reads accept only `service="checkout"` (the default). Logs and deployment
reads accept `limit` from 1 to 100 (default 20) and return newest entries first.
Structured results preserve health/counters, wrap logs as `{"logs": [...]}`, and
retain `active_version` and `deployment_history` for deployment reads. Log contents
are untrusted evidence and are passed through unchanged.

Each server owns one lifespan-managed async HTTP client with a three-second
upstream timeout, no retries, no redirects, and no evidence cache. Compose supplies
`CHECKOUT_BASE_URL=http://checkout:8000` to both and
`INVENTORY_BASE_URL=http://inventory:8000` to observability. Missing or invalid base
URL configuration fails startup. Tool inputs never contain URLs or configurable
paths. Upstream connection/timeouts, non-success responses, invalid JSON, or
unexpected response shapes become failed MCP calls (`is_error=True`), with no
fabricated data or caller-visible tracebacks.

Build/start all five services and verify their status:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
docker compose up -d --build --wait
docker compose ps
$env:INCIDENTOPS_TEST_CHECKOUT_URL = 'http://127.0.0.1:8002'
$env:INCIDENTOPS_TEST_INVENTORY_URL = 'http://127.0.0.1:8001'
$env:INCIDENTOPS_TEST_OBSERVABILITY_MCP_URL = 'http://127.0.0.1:8003/mcp'
$env:INCIDENTOPS_TEST_OPERATIONS_MCP_URL = 'http://127.0.0.1:8004/mcp'
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_mcp_read_capabilities.py
```

MCP Docker healthchecks test TCP listening only; actual MCP `Client` integration
tests establish protocol readiness. They verify exact tool sets/annotations,
baseline and defective-deployment evidence, input rejection, and failed-call
semantics using an isolated local MCP process with an unreachable loopback upstream.
No unrelated container is stopped for the failure test. MCP tests skip when neither
MCP test URL is configured; partial configuration or configured failures fail.

With the PostgreSQL test URL also configured, run the complete suite and shut down:

```powershell
$env:INCIDENTOPS_TEST_DATABASE_URL = 'postgresql://incidentops_dev:incidentops_dev_only@127.0.0.1:5433/incidentops_test'
.\.venv\Scripts\python.exe -m pytest -q
docker compose down
Remove-Item Env:INCIDENTOPS_TEST_OBSERVABILITY_MCP_URL, Env:INCIDENTOPS_TEST_OPERATIONS_MCP_URL, Env:INCIDENTOPS_TEST_CHECKOUT_URL, Env:INCIDENTOPS_TEST_INVENTORY_URL, Env:INCIDENTOPS_TEST_DATABASE_URL -ErrorAction SilentlyContinue
```

The existing PostgreSQL volume is retained.
