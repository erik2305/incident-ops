# ADR-003 — The model never directly receives mutating capabilities

Status: Accepted

## Context
Incident investigation may eventually require operational changes.

## Decision
The model may propose a mutation as structured data. Only a deterministic executor
node calls the actual mutating operation, after human approval.

## Alternatives considered
Expose mutating tools directly to the model; allow automatic execution.

## Why
Separating proposals from execution establishes an explicit human control boundary.

## Consequences
Future model capabilities remain read-only. Mutation and approval implementation
are deferred; Task 001 adds neither.
