# ADR-007 — LLM reasoning boundary

Status: Accepted

## Context
Task 005 proves the read-only evidence path independently of model behavior.
Task 006 adds bounded investigation before mutation and approval infrastructure.

## Decision
An application-side IncidentReasoner returns strict structured decisions/data.
The sole provider adapter is ChatOpenRouter. LangGraph owns validation, routing,
checkpointing, and a fixed maximum of two assessments/two evidence rounds.
The LLM receives no executable tools. Operational data remains explicitly untrusted.
Rollback proposals refer only to observed, non-current checkout versions and confer
no execution authority. Run-scoped clients/models are never checkpointed.

## Consequences
Provider and contract errors fail the run; escalation is a distinct valid conclusion.
Runbook retrieval, HITL, and mutation remain future work. A fresh runtime inspects
checkpointed conclusions without MCP or model connections.
