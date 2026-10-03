"""Real PostgreSQL round trips across independently closed saver lifecycles."""

import asyncio
import os
from uuid import uuid4

import pytest

from incidentops.graph import build_graph
from incidentops.graph.runtime import IncidentRuntimeContext
from incidentops.persistence import open_checkpointer, setup_checkpoints

pytestmark = pytest.mark.integration


def run_async(coroutine):
    # Psycopg requires a selector event loop on Windows, not the default proactor.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coroutine)


@pytest.fixture
def database_url():
    url = os.environ.get("INCIDENTOPS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set INCIDENTOPS_TEST_DATABASE_URL to run PostgreSQL tests")
    # Repeated setup deliberately uses LangGraph's own migration behavior.
    run_async(setup_checkpoints(url))
    return url


@pytest.fixture
def thread_ids(database_url):
    ids = []
    yield ids

    async def cleanup():
        async with open_checkpointer(database_url) as checkpointer:
            for thread_id in ids:
                await checkpointer.adelete_thread(thread_id)

    run_async(cleanup())


async def write_incidents(database_url, incidents, read_capabilities):
    # No graph, saver, or connection escapes this function's lifecycle A.
    async with open_checkpointer(database_url) as checkpointer:
        graph = build_graph(checkpointer=checkpointer)
        written = []
        for incident in incidents:
            config = {"configurable": {"thread_id": incident["incident_id"]}}
            result = await graph.ainvoke(
                incident,
                config=config,
                context=IncidentRuntimeContext(read_capabilities=read_capabilities),
            )
            assert all(result[field] == value for field, value in incident.items())
            assert result["status"] == "investigating"
            assert len(result["evidence"]) == 4
            written.append(result)
            assert (await graph.aget_state(config)).next == ()
        return written


async def read_incidents(database_url, ids):
    # Lifecycle B builds a fresh graph and reads using only native thread IDs.
    async with open_checkpointer(database_url) as checkpointer:
        graph = build_graph(checkpointer=checkpointer)
        restored = []
        for thread_id in ids:
            snapshot = await graph.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
            assert snapshot.next == ()
            restored.append(snapshot.values)
        return restored


def new_incident(thread_ids, prefix, report):
    thread_id = f"{prefix}-{uuid4().hex}"
    thread_ids.append(thread_id)
    return {
        "incident_id": thread_id,
        "user_report": report,
        "target_service": "checkout",
    }


def test_state_survives_closed_runtime(database_url, thread_ids, read_capabilities):
    incident = new_incident(
        thread_ids, "INC-PERSIST-001", "Checkout is returning HTTP 500 responses."
    )

    written = run_async(write_incidents(database_url, [incident], read_capabilities))
    # The writer's event loop and connection are both closed before reopening.
    restored = run_async(read_incidents(database_url, thread_ids))

    assert restored == written


def test_durable_threads_are_isolated(database_url, thread_ids, read_capabilities):
    first = new_incident(thread_ids, "INC-PERSIST-A", "Checkout is failing.")
    second = new_incident(thread_ids, "INC-PERSIST-B", "Search is timing out.")

    written = run_async(
        write_incidents(database_url, [first, second], read_capabilities)
    )
    restored = run_async(read_incidents(database_url, thread_ids))

    assert restored == written


def test_strict_serializer_restores_builtin_state(
    database_url, thread_ids, read_capabilities
):
    incident = new_incident(
        thread_ids,
        "INC-STRICT",
        '  Checkout: HTTP 500 — café, 日本語, 🚨\n{"retry": false}  ',
    )

    written = run_async(write_incidents(database_url, [incident], read_capabilities))
    restored = run_async(read_incidents(database_url, thread_ids))

    assert restored == written
