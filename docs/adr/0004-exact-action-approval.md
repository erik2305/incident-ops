# ADR-004 — Approval binds to an exact persisted action

Status: Accepted

## Context
Approval must authorize the same operation that subsequently executes.

## Decision
Persist an immutable action payload and action identity. Approval references that
exact action; the model does not regenerate arguments after approval.

## Alternatives considered
Approve a broad intent; ask the model to recreate arguments after approval.

## Why
Binding approval to a fixed payload prevents the executed operation from drifting
from the reviewed proposal.

## Consequences
Later approval and executor nodes share a persisted action identity and payload.
No action schema, approval flow, or executor is implemented in Task 001.
