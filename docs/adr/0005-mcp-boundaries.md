# ADR-005 — MCP boundaries

Status: Accepted

## Context
Investigation and operations need clear external capability boundaries.

## Decision
V1 uses two logical MCP domains: observability MCP and operations MCP. Both use
MCP Streamable HTTP. Observability exposes health, metrics, logs and the fixed
checkout probe; operations exposes deployment reads and checkout rollback.
The original plan placed future runbook retrieval inside the application rather
than another MCP server. Final v1 defers runbooks entirely: with one remediation
type in the synthetic scope, retrieval would be mostly decorative.

## Alternatives considered
One combined MCP domain; a separate runbook MCP server.

## Why
The two domains separate investigation from operational capabilities without
adding another integration boundary.

## Consequences
Task 004 began with read-only MCP tools backed by synthetic HTTP evidence; later
work added the guarded rollback and observational probe within the same domains.
Raw synthetic controls are not MCP capabilities. LangGraph consumes MCP through
a run-scoped application-side client; connections live in runtime context, never
checkpoint state. The graph enforces exact approval before rollback; tool
annotations are metadata. Private reset/deploy/fault controls remain outside MCP.
