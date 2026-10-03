# IncidentOps

Incident investigation with structured LLM assessment, a bounded evidence loop,
durable human approval of exact rollback actions, and deterministic recovery
verification, orchestrated by LangGraph.
An asynchronous host API exposes that lifecycle through native FastAPI SSE.

## Development

Start from the root environment example (keep an existing `.env`):

```powershell
Copy-Item .env.example .env
```

Populate `OPENROUTER_API_KEY` locally when live LLM tests are desired. The root
example supplies the explicit model and all five host-side integration URLs.
Start the five Compose services before using those URLs. Pytest loads the root
`.env`; application callers still inject configuration explicitly. The real `.env`
is excluded from Git and Docker images; the example contains no real secret.

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

The accepted test harness loads the ignored root `.env` using python-dotenv.
If it contains an OpenRouter key, supply the full live model/infrastructure
configuration below; partial live configuration fails instead of skipping.
For an offline full-suite run, disable `.env` loading with
`PYTHON_DOTENV_DISABLED=1` and clear the external test configuration from that test
process. Unit tests alone require no external configuration.

## Graph contract

`incidentops.graph.build_graph(checkpointer=...)` compiles
`START → initialize_incident → collect_initial_evidence → assess_evidence` with the
caller's checkpointer. Assessment routes a proposal through
`prepare_action → approval_gate` and interrupts for human approval. Approval resumes
through `execute_action → verify_recovery`, then `finalize_resolved → END` or
`finalize_verification_failure → END`; rejection through `reject_action → END`.
Assessment can also end with escalation, or route
through `collect_requested_evidence → assess_evidence` once. A second request for
more evidence routes through `escalate_evidence_limit → END`.
Input requires non-empty `incident_id` and `user_report` strings and
`target_service` equal to `checkout` or `inventory`. Initialization adds
`status = "investigating"`. Invalid inputs raise `ValueError` before capability reads.

The caller supplies `config={"configurable": {"thread_id": "INC-001"}}` to
`await graph.ainvoke(..., context=IncidentRuntimeContext(...))` and can inspect
checkpoints using `await graph.aget_state(config)` without an MCP runtime context.
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
The full Compose environment also includes the two MCP servers below.
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

## MCP capabilities

The official MCP Python SDK v2 (`mcp>=2,<3`) provides two independently addressable
`MCPServer` processes over Streamable HTTP:

| Server | Loopback MCP endpoint | Exact tools |
| --- | --- | --- |
| `observability-mcp` | `http://127.0.0.1:8003/mcp` | `get_service_health`, `get_metrics`, `query_logs`, `probe_checkout` |
| `operations-mcp` | `http://127.0.0.1:8004/mcp` | `get_recent_deployments`, `rollback_deployment` |

Observability and deployment reads remain read-only. Operations also exposes the
single fixed checkout rollback mutation. Raw deploy/reset controls remain synthetic
test infrastructure; the rollback tool uses only `/__control/rollback` internally.
The reasoner receives evidence and can propose additional reads and rollback as
data; it has no MCP tools. Read and write application contracts remain separate.

Health, metrics, and logs accept only `service="checkout"` or `service="inventory"`.
`probe_checkout` accepts no arguments; surplus arguments are explicitly rejected.
It is annotated read-only and closed-world and returns only `service`, `ok`, and
`observed_status` as structured data. Business failure is a successful observation
with `ok=false`; infrastructure failure is a failed tool call.
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

## Graph evidence collection and persistence

The application-facing `ReadCapabilities` protocol exposes five named async
methods. The model's evidence request schema remains restricted to the original
four reads; the graph invokes the probe only for post-action verification.
`open_read_capabilities` owns two official SDK clients for one graph run,
reuses their connections, disables tool caching, and closes both on exit. The caller
provides MCP URLs explicitly; the frozen `IncidentRuntimeContext` carries the adapter
through LangGraph's native runtime context rather than checkpointed state:

```python
from incidentops.graph.runtime import IncidentRuntimeContext
from incidentops.llm.openrouter import open_openrouter_reasoner
from incidentops.mcp.client import open_read_capabilities

async with (
    open_read_capabilities(observability_url, operations_url) as capabilities,
    open_openrouter_reasoner(api_key=api_key, model=model) as reasoner,
):
    result = await graph.ainvoke(
        {
            "incident_id": "INC-001",
            "user_report": "Checkout requests return errors",
            "target_service": "checkout",
        },
        config={"configurable": {"thread_id": "INC-001"}},
        context=IncidentRuntimeContext(
            read_capabilities=capabilities, reasoner=reasoner
        ),
    )
```

The evidence node reads health, metrics, and logs (limit 20), in that order, for
either target. Checkout additionally reads recent deployments (limit 10).
Each built-in dictionary has exactly `capability`, `service`, `data`, and
`trust="untrusted_operational_data"`. Suspicious log text is preserved unchanged.
The adapter checks failed MCP calls before accepting validated structured JSON;
text content is never a fallback. A failed read raises `ReadCapabilityError` with
a safe message. The node publishes its complete evidence list only after every
required read succeeds, so failure leaves no partial evidence update.

Graph state contains only incident strings and plain evidence data. Clients, URLs,
and runtime context are excluded from checkpoints. PostgreSQL retains its strict
serializer and caller-owned saver lifecycle. The original graph and PostgreSQL
regressions use fake capabilities, keeping database-only verification independent
of MCP and OpenRouter. The graph additionally checkpoints structured assessments,
evidence round, terminal status, and a proposal or escalation reason. There is no
model execution authority. The approval/execution phase is described below.

Run the live graph → MCP → PostgreSQL proof and then the fully configured suite:

```powershell
docker compose up -d --build --wait
$env:INCIDENTOPS_TEST_DATABASE_URL = 'postgresql://incidentops_dev:incidentops_dev_only@127.0.0.1:5433/incidentops_test'
$env:INCIDENTOPS_TEST_CHECKOUT_URL = 'http://127.0.0.1:8002'
$env:INCIDENTOPS_TEST_INVENTORY_URL = 'http://127.0.0.1:8001'
$env:INCIDENTOPS_TEST_OBSERVABILITY_MCP_URL = 'http://127.0.0.1:8003/mcp'
$env:INCIDENTOPS_TEST_OPERATIONS_MCP_URL = 'http://127.0.0.1:8004/mcp'
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_graph_mcp_evidence.py -o cache_dir=.pytest-tmp-task005-cache
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-task005-full -o cache_dir=.pytest-tmp-task005-cache
docker compose down
Remove-Item Env:INCIDENTOPS_TEST_OBSERVABILITY_MCP_URL, Env:INCIDENTOPS_TEST_OPERATIONS_MCP_URL, Env:INCIDENTOPS_TEST_CHECKOUT_URL, Env:INCIDENTOPS_TEST_INVENTORY_URL, Env:INCIDENTOPS_TEST_DATABASE_URL -ErrorAction SilentlyContinue
```

The workspace-local temporary/cache paths avoid Windows permission conflicts and
are ignored by Git. The new test prepares defective checkout v2, creates a real
500 response, and checkpoints evidence through the graph's MCP adapter. Runtime A
closes its clients, saver, and event loop; runtime B uses a fresh saver and graph
to recover the exact evidence without reconnecting to MCP. Test cleanup resets
both synthetic services and deletes the unique checkpoint thread. Docker shutdown
retains the database volume. No tracing or evaluation subsystem is added; existing
SDK dependencies include LangSmith and the OpenTelemetry API transitively.

## Bounded reasoning with OpenRouter

`IncidentReasoner` is a run-scoped application protocol. Its sole concrete provider
is `langchain-openrouter`'s `ChatOpenRouter`, configured explicitly with an API key
and exactly one model. The factory uses a fixed seed (0), a 60-second model timeout,
no LangChain or SDK retries, medium reasoning effort with returned reasoning
excluded, and no provider fallbacks. Native strict JSON-schema output is converted
immediately into plain dictionaries; graph-side semantic validation follows.
No raw model responses, messages, model objects, keys, or chain-of-thought are
checkpointed. The SDK's synchronous and asynchronous transports close on context exit.
Temperature is omitted because GPT-6 Luna endpoints do not advertise support for
it; sending temperature 0 with strict parameter routing yields no eligible endpoint.

Assessments contain `decision`, `root_cause`, `confidence`, concise `summary`,
`evidence_requests`, and nullable `proposed_remediation`. Decisions are
`need_more_evidence`, `propose_remediation`, or `escalate`; root causes are
`bad_deployment`, `downstream_failure`, or `unknown`; confidence is low/medium/high.
Evidence requests contain only a closed capability and service. They must be
novel, unique, compatible, and limited to 1–3 reads. Logs/deployments keep the
application's fixed 20/10 limits. Additional collection is sequential and atomic.

There are at most **two evidence rounds and two assessments**. After round 2,
another valid evidence request causes deterministic escalation with
`escalation_reason="evidence_budget_exhausted"`. Provider, parsing, contract, or
missing runtime dependency failures raise application errors; they are not incident
escalations. A valid proposal continues to `awaiting_approval`; successful execution
checkpoints `action_executed` before verification. Confirmed recovery ends with
`resolved`; completed unsuccessful verification or rejection ends with `escalated`.

The only proposal is `rollback_deployment` for checkout. Its target must occur in
observed checkout deployment history and differ from both the active and newest
version. A proposal is data only until a human approves the exact persisted action.
Rollback and HITL are implemented. No executable tools are bound to the model.

Trusted system instructions explain that operational text cannot redefine the task
or authorize actions. A separate human/application message contains clearly labeled
untrusted JSON evidence and the report. Suspicious text is preserved. Closed schemas,
graph validation, and absence of execution authority provide structural boundaries;
prompt wording and the unit tests do not establish complete prompt-injection resistance.

For live verification, set `OPENROUTER_API_KEY` locally and explicitly configure
`INCIDENTOPS_TEST_LLM_MODEL`. The recommended Task 006 model is
`openai/gpt-6-luna`; no substitute model is selected automatically. Application
code does not read `.env`; pytest infrastructure loads it. This optional PowerShell
step also loads test configuration into the shell without printing the key:

```powershell
foreach ($line in Get-Content -LiteralPath .env) {
    if ($line -match '^\s*(OPENROUTER_API_KEY|INCIDENTOPS_TEST_LLM_MODEL)\s*=\s*(.*?)\s*$') {
        $value = $Matches[2].Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($Matches[1], $value, 'Process')
    }
}
$env:INCIDENTOPS_TEST_LLM_MODEL = 'openai/gpt-6-luna'
docker compose up -d --build --wait
$env:INCIDENTOPS_TEST_DATABASE_URL = 'postgresql://incidentops_dev:incidentops_dev_only@127.0.0.1:5433/incidentops_test'
$env:INCIDENTOPS_TEST_CHECKOUT_URL = 'http://127.0.0.1:8002'
$env:INCIDENTOPS_TEST_INVENTORY_URL = 'http://127.0.0.1:8001'
$env:INCIDENTOPS_TEST_OBSERVABILITY_MCP_URL = 'http://127.0.0.1:8003/mcp'
$env:INCIDENTOPS_TEST_OPERATIONS_MCP_URL = 'http://127.0.0.1:8004/mcp'
.\.venv\Scripts\python.exe -m pytest -q -s tests/integration/test_graph_llm_reasoning.py -o cache_dir=.pytest-tmp-task006-cache
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-task006-full -o cache_dir=.pytest-tmp-task006-cache
docker compose down
Remove-Item Env:OPENROUTER_API_KEY, Env:INCIDENTOPS_TEST_LLM_MODEL, Env:INCIDENTOPS_TEST_DATABASE_URL, Env:INCIDENTOPS_TEST_CHECKOUT_URL, Env:INCIDENTOPS_TEST_INVENTORY_URL, Env:INCIDENTOPS_TEST_OBSERVABILITY_MCP_URL, Env:INCIDENTOPS_TEST_OPERATIONS_MCP_URL -ErrorAction SilentlyContinue
```

The live test skips only when the API key is absent; with a key, missing model/URL
configuration or a configured provider failure fails. It prepares real checkout v2
errors, reads evidence through MCP, obtains a real structured rollback-to-v1 proposal,
materializes its exact pending action and pauses at the approval interrupt,
and independently verifies checkout remains on v2 and still returns 500. It then
closes runtime A's MCP/model/saver/event loop and recovers the entire structured
state in runtime B without MCP or LLM connections. Raw controls remain test setup
and cleanup only. The Task 005 live data-plane regression uses a fake reasoner.

## Durable exact-action approval and rollback

`prepare_action` revalidates the observed proposal and deterministically persists
`pending_action` before `approval_gate` interrupts. The pending action contains
only `action_id`, `fingerprint`, `action`, `service`, and `target_version`.
The fingerprint is SHA-256 of the executable payload serialized as sorted-key JSON
with stable separators and UTF-8. The action ID hashes a fixed versioned namespace,
incident ID, and fingerprint. Display text is excluded. The same incident/action
has the same identity; conflicting existing actions fail instead of being replaced.

The interrupt displays `kind="approval_required"`, incident ID, action ID,
fingerprint, and the exact persisted action payload. Its response schema permits
only `decision="approve"|"reject"` and `action_id`. Replacement arguments or a
human-supplied fingerprint are rejected. The gate performs only deterministic
integrity checks and payload construction before `interrupt()`, so re-entry is safe.

After the original runtime closes, open a fresh saver/graph using the same thread.
An externally supplied human decision resumes the graph directly:

```python
from langgraph.types import Command
from incidentops.graph.runtime import IncidentRuntimeContext
from incidentops.mcp.client import open_read_capabilities, open_write_capabilities

# approval_decision contains only the human's approve/reject and persisted action_id.
async with (
    open_write_capabilities(operations_url) as writes,
    open_read_capabilities(observability_url, operations_url) as reads,
):
    result = await graph.ainvoke(
        Command(resume=approval_decision),
        config={"configurable": {"thread_id": incident_id}},
        context=IncidentRuntimeContext(
            write_capabilities=writes, read_capabilities=reads
        ),
    )

# For rejection, use Command(resume=approval_decision) with no runtime context.
```

Reject resumes require no MCP, model, or write dependency and retain the pending
action/approval for audit. They end with `escalated`, `action_rejected`. Approve
resumes require fresh read and write MCP clients for execution plus verification,
with no LLM/reasoner. The
executor checks action identity and both persisted fingerprints, rejects an
existing execution record, then calls rollback using only PendingAction arguments.
No reasoning or evidence read occurs between approval and mutation. Successful
execution stores a plain structured result with the same action ID/fingerprint.
Write failures raise `WriteCapabilityError`, create no success record, and receive
no automatic retry or incident-escalation substitution.

The MCP rollback accepts only checkout and target `v1`/`v2`; unknown or surplus
arguments fail. Its structured result contains `service`, `previous_version`,
`active_version`, `target_version`, and `applied`. It is annotated non-read-only,
destructive, idempotent, and closed-world. These annotations are metadata;
IncidentOps/LangGraph enforces approval. The synthetic primitive checks deployment
eligibility under the service lock. At the existing target it returns `applied=false`
without another history event; otherwise it changes version and appends one event.

The application execution guard prevents a second successful write. Primitive
idempotency covers a narrow mutation-before-checkpoint crash window. Neither is a
distributed exactly-once guarantee. SHA-256 detects internal payload drift, not
tampering by an administrator who can rewrite both payload and hashes. Operations
MCP is an internal endpoint without authentication/RBAC; direct hostile network
access is outside this MVP threat model. The security claim is that the IncidentOps
graph never invokes rollback before valid human approval, not that every possible
network client is prevented from invoking the internal capability.

After execution, `verify_recovery` rechecks pending action, approved action identity,
fingerprint, and matching execution result before making two sequential reads:
`get_recent_deployments("checkout", limit=3)` and `probe_checkout()`. The target
comes from PendingAction. Recovery requires both the active version and newest
deployment entry to equal that target, and a probe with `ok=true` and status 200.
Malformed observations raise `ReadCapabilityError`. No historical error counters
or process health result can establish recovery.

Checkout's fixed `GET /__ops/probe` uses SKU-001 and quantity 1. It shares
`calculate_checkout` with `POST /checkout`, makes a real HTTP inventory call, and
runs the same version-dependent price calculation. Inventory's private fixed
`GET /__ops/probe-stock` shares `stock_for` with its business endpoint and suppresses
ordinary traffic recording. Neither service's request/error counters or business
logs change during a probe. The actual v2 missing-price-field defect yields a
valid observed status 500; v1 computes successfully. No version-to-health oracle
exists. Inventory transport/HTTP/malformed-contract failures cause raw probe HTTP
503 and hence an MCP tool/capability error, rather than `ok=false`.

Only after both valid observations does verification publish its plain
`verification_result`: action ID, approved target, deployment match, probe status,
probe success, and recovery decision. Pure terminal nodes set `resolved` or
`escalated` with `verification_failed`, retaining every audit record. A completed
negative verification does not trigger more reasoning, reads, or remediation.
Rollback failure raises `WriteCapabilityError` without execution/verification
success records. Verification infrastructure failure raises an exception and
retains the saved execution and `action_executed` status, without fabricating an
incident conclusion. A caller may explicitly retry the failed verification using
`graph.ainvoke(None, config=config, context=IncidentRuntimeContext(read_capabilities=reads))`;
this resumes verification and does not execute rollback again. No automatic retry
or new resume path after terminal resolution is introduced.

Runtime A has reads and a reasoner, Runtime B has reads and writes with no reasoner,
and Runtime C inspects checkpoints with no operational capabilities. Execution
still uses only writes; verification uses only reads. Strict serialization and
untrusted evidence policy remain unchanged. FastAPI/SSE approval transport and
final incident reports remain outside this slice.

With the five infrastructure URLs above configured, explicitly verify both layers:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_mcp_rollback.py -o cache_dir=.pytest-tmp-task007-cache
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_mcp_probe.py -o cache_dir=.pytest-tmp-task008-cache
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_graph_hitl.py -o cache_dir=.pytest-tmp-task007-cache
# Also configure the ignored OpenRouter key and explicit model as shown above.
.\.venv\Scripts\python.exe -m pytest -q -s tests/integration/test_graph_llm_reasoning.py -o cache_dir=.pytest-tmp-task007-cache
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-task007-full -o cache_dir=.pytest-tmp-task007-cache
docker compose down
```

The durable HITL test uses real PostgreSQL/MCP and a deterministic fake reasoner.
Runtime A investigates and closes at the persisted interrupt while checkout still
fails on v2. Fresh runtime B approves with read/write authority, performs the
real rollback and observes deployment plus checkout behavior, ending `resolved`.
Fresh runtime C recovers the exact action, approval, execution, and verification
with no MCP/LLM. The separate live OpenRouter test stops at approval and never
auto-approves or verifies. Probe tests check v2 failure, v1 success, argument
rejection, and unchanged business counters/logs in both services. Integration
tests skip when their corresponding configuration is absent; partial configuration
or configured infrastructure/provider failures fail.
Tests reset synthetic services and delete unique threads. Shutdown retains the volume.

## Local API and live SSE demo

The control plane is an unauthenticated local/demo MVP. Run it on loopback; it is
not an internet-ready API. The existing five Compose services remain unchanged.
FastAPI uses its native `EventSourceResponse`/`ServerSentEvent` support, available
from version 0.135.0. No external SSE integration is used by the API.

For a fresh configuration, copy the root example and fill the local key. Keep an
existing `.env` and add the four API runtime settings from `.env.example` to it:

```powershell
Copy-Item .env.example .env
# Edit .env locally: populate OPENROUTER_API_KEY, never commit the key.
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
docker compose up -d --build --wait

# Load only API runtime settings into this shell; this does not print values.
foreach ($line in Get-Content -LiteralPath .env) {
    if ($line -match '^\s*(OPENROUTER_API_KEY|INCIDENTOPS_DATABASE_URL|INCIDENTOPS_OBSERVABILITY_MCP_URL|INCIDENTOPS_OPERATIONS_MCP_URL|INCIDENTOPS_OPENROUTER_MODEL)\s*=\s*(.*?)\s*$') {
        $value = $Matches[2].Trim().Trim('"').Trim("'")
        [Environment]::SetEnvironmentVariable($Matches[1], $value, 'Process')
    }
}

# Provision checkpoint schema explicitly, before starting the API.
.\.venv\Scripts\python.exe -m incidentops.api.setup
.\.venv\Scripts\python.exe -m incidentops.api
```

The launch module runs Uvicorn at `http://127.0.0.1:8010` on a selector event loop,
including on Windows where async Psycopg cannot use a Proactor loop. App factory
`incidentops.api.app:create_app` also accepts explicit configuration and dependency
factories for embedding/testing. Importing the module opens no database, MCP, or
OpenRouter client. Startup opens one existing strict PostgreSQL saver and builds
the accepted graph; schema setup is never performed implicitly by the app.

In a second PowerShell window, prepare the existing synthetic v2 incident and open
the initial SSE response. The temporary JSON files avoid Windows command-line
JSON quoting issues:

```powershell
$checkout = 'http://127.0.0.1:8002'
$inventory = 'http://127.0.0.1:8001'
$api = 'http://127.0.0.1:8010'
Invoke-RestMethod "$checkout/__control/reset" -Method Post
Invoke-RestMethod "$inventory/__control/reset" -Method Post
Invoke-RestMethod "$checkout/__control/deploy" -Method Post -ContentType 'application/json' -Body '{"version":"v2"}'
# Expected 500 creates actual incident evidence.
@{ sku = 'SKU-001'; quantity = 1 } | ConvertTo-Json | Set-Content -LiteralPath "$env:TEMP\incidentops-order.json" -Encoding utf8
curl.exe -s -o NUL -w '%{http_code}' -H 'Content-Type: application/json' --data-binary "@$env:TEMP\incidentops-order.json" "$checkout/checkout"

@{
    user_report = 'Checkout returns HTTP 500 after the deployment.'
    target_service = 'checkout'
} | ConvertTo-Json | Set-Content -LiteralPath "$env:TEMP\incidentops-start.json" -Encoding utf8
curl.exe --no-buffer -H 'Content-Type: application/json' --data-binary "@$env:TEMP\incidentops-start.json" "$api/incidents"
```

The first event is `incident_started`, followed by bounded node `progress` events.
When the actual LangGraph interrupt is reached, `approval_required` displays its
persisted incident ID, action ID, fingerprint, and exact action, then closes the
connection. Copy the incident/action IDs from that event, review the action, and
resume it later:

```powershell
$incidentId = '<incident_id from SSE>'
$actionId = '<action_id from approval_required>'
Invoke-RestMethod "$api/incidents/$incidentId"
# GET /result returns 409 while the incident awaits approval.

@{
    decision = 'approve' # Use 'reject' to refuse the exact pending action.
    action_id = $actionId
} | ConvertTo-Json | Set-Content -LiteralPath "$env:TEMP\incidentops-approval.json" -Encoding utf8
curl.exe --no-buffer -H 'Content-Type: application/json' --data-binary "@$env:TEMP\incidentops-approval.json" "$api/incidents/$incidentId/approval"
Invoke-RestMethod "$api/incidents/$incidentId"
Invoke-RestMethod "$api/incidents/$incidentId/result"
```

Approval opens fresh read/write MCP clients, resumes the same thread, streams
execution and verification progress, then emits `completed` with `resolved` or
`escalated`. It creates no reasoner. Rejection opens no capabilities, records
`action_rejected`, and emits `completed/escalated`. The result endpoint returns
structured root cause and execution/verification facts or an escalation reason,
without generating a prose report.

| Route | Contract |
| --- | --- |
| `POST /incidents` | Strict `user_report`, closed `target_service`; server-generated UUID; SSE |
| `GET /incidents/{id}` | API-owned projection of current checkpoint state; absent fields omitted |
| `POST /incidents/{id}/approval` | Only strict `decision` and `action_id`; SSE |
| `GET /incidents/{id}/result` | Compact terminal result; 409 for a non-terminal incident |
| `GET /health` | Process health; no downstream calls |

Event names are exactly `incident_started`, `progress`, `approval_required`,
`completed`, and `error`. Progress contains only incident ID, completed node, and
current status. Completed contains ID, terminal status, and optional escalation
reason. Error contains ID, a fixed code, and safe message. No evidence, raw graph
metadata, prompts, model responses/tokens, runtime credentials, or hidden reasoning
appears in progress events. Detailed operational state is available through GET,
with its existing untrusted-evidence labels preserved.

Pre-stream errors are ordinary JSON HTTP errors: 422 for invalid/extra request
fields, 404 for an unknown incident, 409 for wrong action/lifecycle/overlapping
operations, and 503 if checkpoint inspection fails. Approval preflight also
requires an actual interrupt; graph-level integrity validation remains authoritative.
Failures after streaming begins emit one safe `error` and close the stream, without
fabricating an incident escalation. A failed execution/verification remains visible
at its last saved checkpoint. This slice adds no retry endpoint.

**SSE transport is live and non-durable. LangGraph/PostgreSQL state is durable.**
There are no event IDs, Last-Event-ID replay, persisted event history, or event bus.
Reconnect using GET to inspect current state. If the client disconnects, cancellation
stops the active request-driven graph run and closes MCP/model resources; already
written checkpoints remain. The API does not continue execution in a background
worker. A disconnect before the first checkpoint may leave no durable state for
the streamed ID. A small process-local reservation rejects overlapping operations
for one incident; it does not provide cross-process arbitration or exactly-once
semantics. Existing action guards and rollback idempotency remain the backstop.

Run the network-level API proof with all five test URLs configured as above:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_api_lifecycle.py --basetemp=.pytest-tmp-task009-api -o cache_dir=.pytest-tmp-task009-api-cache
.\.venv\Scripts\python.exe -m pytest -q -s tests/integration/test_graph_llm_reasoning.py --basetemp=.pytest-tmp-task009-live -o cache_dir=.pytest-tmp-task009-live-cache
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp-task009-full -o cache_dir=.pytest-tmp-task009-full-cache
```

API integrations use real PostgreSQL, synthetic services, MCP, and live loopback
HTTP streams with a deterministic fake reasoner. They close app A at approval and
use a fresh app B for later approval/rejection; no live model is needed. Unit API
tests also prove first-event delivery during blocked reasoning, disconnect
cancellation, safe error projection, and dependency ownership. API integrations
skip when all five test URLs are absent; partial/configured failures fail. The
existing live OpenRouter regression retains its separate key/model requirements.

After the demo, stop the host API with Ctrl+C, reset the two synthetic services,
and run `docker compose down` without `--volumes` to retain PostgreSQL data.
