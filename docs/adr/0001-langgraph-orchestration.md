# ADR-001 — LangGraph owns orchestration

Status: Accepted

## Context
Incident response needs explicit workflow state and routing.

## Decision
LangGraph owns incident state transitions and routing. LLMs make bounded
reasoning decisions within that workflow.

## Alternatives considered
A custom agent loop; model-directed orchestration.

## Why
An explicit graph makes workflow behavior inspectable and testable without
delegating control flow to the model.

## Consequences
Task 001 began with one real node. Final v1 extends the same graph through bounded
investigation, durable approval, execution and verification, without a competing
orchestration loop.
