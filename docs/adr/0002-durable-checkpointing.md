# ADR-002 — Durable checkpoints will use PostgreSQL

Status: Accepted

## Context
Production and demo incident state must eventually survive process restarts.

## Decision
Use a PostgreSQL-backed LangGraph checkpointer for production/demo. In-memory
checkpointing is acceptable only for unit tests and early development.

## Alternatives considered
In-memory-only state; SQLite persistence; custom persistence.

## Why
PostgreSQL provides the selected durable storage boundary while retaining native
LangGraph checkpoint APIs.

## Consequences
Graph construction receives its checkpointer from the caller. Task 001 tests use
an in-memory saver; PostgreSQL integration and lifecycle management are deferred.
The eventual native `thread_id` equals `incident_id`.
