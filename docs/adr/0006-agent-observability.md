# ADR-006 — Agent observability

Status: Accepted

## Context
V1 will need visibility into graph execution and model reasoning.

## Decision
LangSmith is the primary tracing approach for v1. OpenTelemetry is outside the MVP.

## Alternatives considered
OpenTelemetry; custom tracing infrastructure.

## Why
Selecting one primary tracing approach keeps the initial observability scope
focused on agent execution.

## Consequences
Tracing integration is deferred. Task 001 adds no tracing setup or explicit
LangSmith SDK dependency; LangGraph may supply its own transitive dependencies.
