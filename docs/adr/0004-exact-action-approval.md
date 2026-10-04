# ADR-004 — Approval binds to an exact persisted action

Status: Accepted

## Context
Approval must authorize the same operation that subsequently executes.

## Decision
Persist an immutable action payload and action identity. Approval references that
exact action; the model does not regenerate arguments after approval.
Task 007 materializes a deterministic PendingAction, fingerprints canonical
executable JSON with SHA-256, and derives its stable action ID from the incident,
fingerprint, and a fixed namespace. The action is checkpointed before interrupt.
Approval contains only approve/reject and that action ID. The executor consumes
the persisted action after verifying its fingerprint and matching approval record.

## Alternatives considered
Approve a broad intent; ask the model to recreate arguments after approval.

## Why
Binding approval to a fixed payload prevents the executed operation from drifting
from the reviewed proposal.

## Consequences
The interrupt node is side-effect-free and safe to re-enter. A fresh runtime may
resume approval with reads and writes, without a reasoner: execution uses writes
and subsequent verification uses reads. Rejection requires none. MCP annotations are
metadata, not the approval mechanism. Fingerprints detect internal payload drift,
not database-admin tampering. Execution-record guards plus synthetic rollback
idempotency reduce replay risk without claiming distributed exactly-once execution.
