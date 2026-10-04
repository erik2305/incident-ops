# ADR-006 — Agent observability

Status: Accepted; amended for v1 release

## Context
Graph execution needs inspectable evidence and workflow state.

## Decision
The initial decision selected LangSmith as the intended tracing approach.
The v1 release amendment defers LangSmith tracing: no tracing stack is required
for v1. OpenTelemetry remains outside the MVP.

## Alternatives considered
OpenTelemetry; custom tracing infrastructure.

## Why
The initial choice avoided competing tracing stacks. Final v1 relies on durable
workflow state, bounded API events, evaluator counters and retained results, which
are sufficient for the portfolio scope without a tracing integration.

## Consequences
LangSmith and OpenTelemetry instrumentation are deferred. No tracing setup,
service, credentials or explicit tracing dependency is required. Existing framework
dependencies may transitively include their libraries; this is not an integration.
