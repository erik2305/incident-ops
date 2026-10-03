"""Real PostgreSQL round trips across independently closed saver lifecycles."""

import asyncio
import os
from uuid import uuid4

import pytest

from incidentops.graph import build_graph
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


async def write_incidents(database_url, incidents):
    # No graph, saver, or connection escapes this function's lifecycle A.
    async with open_checkpointer(database_url) as checkpointer:
        graph = build_graph(checkpointer=checkpointer)
        for incident in incidents:
            config = {"configurable": {"thread_id": incident["incident_id"]}}
            result = await graph.ainvoke(incident, config=config)
            assert result == {**incident, "status": "investigating"}
            assert (await graph.aget_state(config)).next == ()


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
    return {"incident_id": thread_id, "user_report": report}


def test_state_survives_closed_runtime(database_url, thread_ids):
    incident = new_incident(
        thread_ids, "INC-PERSIST-001", "Checkout is returning HTTP 500 responses."
    )

    run_async(write_incidents(database_url, [incident]))
    # The writer's event loop and connection are both closed before reopening.
    restored = run_async(read_incidents(database_url, thread_ids))

    assert restored == [{**incident, "status": "investigating"}]


def test_durable_threads_are_isolated(database_url, thread_ids):
    first = new_incident(thread_ids, "INC-PERSIST-A", "Checkout is failing.")
    second = new_incident(thread_ids, "INC-PERSIST-B", "Search is timing out.")

    run_async(write_incidents(database_url, [first, second]))
    restored = run_async(read_incidents(database_url, thread_ids))

    assert restored == [
        {**first, "status": "investigating"},
        {**second, "status": "investigating"},
    ]


def test_strict_serializer_restores_builtin_state(database_url, thread_ids):
    incident = new_incident(
        thread_ids,
        "INC-STRICT",
        '  Checkout: HTTP 500 — café, 日本語, 🚨\n{"retry": false}  ',
    )

    run_async(write_incidents(database_url, [incident]))
    restored = run_async(read_incidents(database_url, thread_ids))

    assert restored == [{**incident, "status": "investigating"}]
