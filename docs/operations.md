# Local operational contracts

The root README is the current quickstart; these details preserve useful technical
contracts from the incremental development README. All endpoints are local/demo
surfaces without auth/RBAC. Use the documented local-only PostgreSQL credentials
only in this synthetic environment.

## Ports and capability ownership

| Host endpoint | Role |
|---|---|
| `127.0.0.1:5433` | PostgreSQL LangGraph checkpoint storage |
| `127.0.0.1:8001` | Inventory HTTP substrate |
| `127.0.0.1:8002` | Checkout HTTP substrate |
| `127.0.0.1:8003/mcp` | Observability Streamable HTTP MCP |
| `127.0.0.1:8004/mcp` | Operations Streamable HTTP MCP |
| `127.0.0.1:8010` | Host IncidentOps API |

Observability MCP exposes `get_service_health`, `get_metrics`, `query_logs`, and
argument-free `probe_checkout`. Operations MCP exposes `get_recent_deployments`
and `rollback_deployment`. The graph fixes evidence limits, validates service and
capability pairs, and separates run-scoped reads/writes from checkpoint state.

## Synthetic controls and observation

Private raw controls are never model/MCP tools. Inventory and checkout each expose
`POST /__control/reset`; checkout also has deploy and idempotent rollback controls.
Inventory has exactly one fault toggle with a strict mode/body:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/__control/fault -ContentType application/json -Body '{"mode":"unavailable"}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/__control/reset
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8002/__control/reset
```

Unavailable inventory returns business/probe-stock HTTP 503 while process health
stays healthy. Reset clears the fault, counters and logs. Normal checkout v1
returns 200; v2's real price-field defect returns 500; dependency failures return
502. Inventory `GET /__ops/probe-stock` and checkout `GET /__ops/probe` share
business computation but suppress ordinary counters/logs. Checkout's probe fixes
SKU-001/quantity 1 and returns successful HTTP 200 with `{service, ok,
observed_status}` when it observes 200/500/502 behavior. Infrastructure failure
at the probe/MCP boundary remains a capability error. Observability contains no
fault-mode/expected-action/root-cause scenario labels.

Recovery requires the target active/newest deployment plus probe `ok=true` and
status 200. Historical error counters and process health cannot establish it.
Negative verification preserves execution records and ends
`escalated/verification_failed`. A checkpointed verification infrastructure failure
can be retried by an explicit embedding caller with `graph.ainvoke(None, ...,
context=IncidentRuntimeContext(read_capabilities=reads))`; this is not an API route
or automatic retry and does not repeat the successful rollback.

## API/SSE boundaries

The API server generates UUID incident IDs and uses them as checkpoint thread IDs.
Its GET projections preserve untrusted evidence labels. Imports open no external
clients; startup opens one strict PostgreSQL saver. Schema setup is explicit via
`python -m incidentops.api.setup`, after setting `INCIDENTOPS_DATABASE_URL`.

SSE events are `incident_started`, `progress`, `approval_required`, `completed`,
and `error`. Progress is bounded to incident ID, completed node and status. No
raw model response, prompt, token stream, hidden reasoning or credential is emitted.
The approval event carries the actual persisted action ID, fingerprint and payload.
The stream ends there; later approval uses a new request/runtime context.

Pre-stream errors are JSON HTTP 422 for invalid/extra fields, 404 for unknown
incidents, 409 for wrong lifecycle/action ID or overlapping operations, and 503
for checkpoint inspection failures. `/result` returns 409 until terminal state.
Post-stream errors emit a fixed safe `error` and close, preserving the last saved
checkpoint rather than inventing a resolved/escalated result.

SSE delivery is transient: no event IDs, Last-Event-ID replay, stored event history,
or background continuation. Disconnect cancels the request and closes owned
resources. A disconnect before the first checkpoint may leave no durable state.
The process-local reservation prevents overlapping operations within one API
process only. Fingerprints detect internal action drift, not database-admin
tampering; execution guards and primitive idempotency are not an exactly-once protocol.

Run mutation-bearing tests and evaluation serially against an idle environment.
Shutdown with `docker compose down`, without `--volumes`, retains PostgreSQL data.
