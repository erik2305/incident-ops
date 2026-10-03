# ADR-005 — MCP boundaries

Status: Accepted

## Context
Future investigation and operations need clear external capability boundaries.

## Decision
V1 uses two logical MCP domains: observability MCP and operations MCP. Runbook
retrieval stays inside the IncidentOps application.

## Alternatives considered
One combined MCP domain; a separate runbook MCP server.

## Why
The two domains separate investigation from operational capabilities without
adding another integration boundary for application-owned runbooks.

## Consequences
Later integrations follow these domains and keep runbook retrieval local to the
application. Task 001 implements no MCP or retrieval code.
