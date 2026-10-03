# ADR-005 — MCP boundaries

Status: Accepted

## Context
Future investigation and operations need clear external capability boundaries.

## Decision
V1 uses two logical MCP domains: observability MCP and operations MCP. Runbook
retrieval stays inside the IncidentOps application. Both domains use MCP
Streamable HTTP. Observability exposes health, metrics, and logs; operations
exposes deployment reads now, with approved mutations reserved for later tasks.

## Alternatives considered
One combined MCP domain; a separate runbook MCP server.

## Why
The two domains separate investigation from operational capabilities without
adding another integration boundary for application-owned runbooks.

## Consequences
Task 004 exposes only read-only MCP tools backed by synthetic HTTP evidence.
Raw synthetic controls are not MCP capabilities. LangGraph consumes MCP through
a run-scoped application-side client; connections live in runtime context, never
checkpoint state. Runbook retrieval remains deferred.
