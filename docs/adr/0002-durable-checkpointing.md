# ADR-002 — Durable checkpoints use PostgreSQL

Status: Accepted

## Context
Demo incident state must survive process restarts.

## Decision
Use the official `AsyncPostgresSaver` for production/demo, with strict msgpack
allowlisting and no pickle fallback. In-memory checkpointing is acceptable only
for unit tests and early development.

## Alternatives considered
In-memory-only state; SQLite persistence; custom persistence.

## Why
PostgreSQL provides durable storage through native LangGraph checkpoint APIs.
The async saver fits the asynchronous IncidentOps API/runtime. Strict
deserialization supports the built-in state values without allowing custom types.

## Consequences
Graph construction receives its checkpointer from the caller. Unit tests use an
in-memory saver; PostgreSQL integration tests use independently managed async
saver lifecycles. Explicit setup uses LangGraph's migrations, without application
checkpoint tables or migration flags.
The API and evaluator use native `thread_id` equal to `incident_id`.
