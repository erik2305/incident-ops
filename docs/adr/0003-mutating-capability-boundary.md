# ADR-003 — The model never directly receives mutating capabilities

Status: Accepted

## Context
Incident investigation can require operational changes.

## Decision
The model may propose a mutation as structured data. Only a deterministic executor
node calls the actual mutating operation, after human approval.

## Alternatives considered
Expose mutating tools directly to the model; allow automatic execution.

## Why
Separating proposals from execution establishes an explicit human control boundary.

## Consequences
Task 001 deferred mutation and approval. Final v1 implements a deterministic,
approval-bound checkout rollback executor. The model has no executable tools;
it emits validated advisory read requests and proposals. A fresh runtime resumes
the exact persisted action without a reasoner, then independently verifies recovery.
