# ADR-001 — LangGraph owns orchestration

Status: Accepted

## Context
Incident response needs explicit workflow state and routing.

## Decision
LangGraph owns incident state transitions and routing. Future LLMs make bounded
reasoning decisions within that workflow.

## Alternatives considered
A custom agent loop; model-directed orchestration.

## Why
An explicit graph makes workflow behavior inspectable and testable without
delegating control flow to the model.

## Consequences
Task 001 implements one real node. Later workflow changes extend this graph
rather than introduce a competing orchestration loop.
