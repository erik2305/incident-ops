# ADR-008 — Native SSE exposes the durable incident lifecycle

Status: Accepted

## Context

The core graph already owns investigation, exact-action approval, execution, and
recovery verification. Clients need a small asynchronous HTTP interface.

## Decision

Use FastAPI native SSE for start and approval requests. Project asynchronous graph
node updates into bounded API-owned events. End the initial stream at the actual
durable approval interrupt; a later request resumes the same checkpoint thread.
One lifespan-managed PostgreSQL saver is the workflow source of truth, with schema
setup remaining explicit provisioning. GET endpoints inspect native graph state.

Run-scoped dependencies live inside the response: start opens reads/reasoning,
approve opens reads/writes, and reject opens neither. Client disconnect cancels
the request's execution and closes dependencies. No worker or event bus continues
the run. SSE events are transient and have no persisted sequence or replay.

## Consequences

Clients recover current state through GET after reconnecting. Checkpoints survive
API restarts; event delivery does not. A small process-local reservation rejects
overlapping operations for a thread, without cross-process arbitration or an
exactly-once claim. Graph integrity and primitive idempotency remain unchanged.
The host API binds to loopback for this unauthenticated local/demo MVP. No incident
SQL table, frontend, worker, or API container is introduced.
