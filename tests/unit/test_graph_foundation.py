"""Behavioral coverage of execution, checkpointing, and input validation."""

import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from incidentops.graph import build_graph
from incidentops.graph.nodes import initialize_incident


@pytest.fixture
def graph():
    return build_graph(checkpointer=InMemorySaver())


@pytest.fixture
def invoke(graph, incident_runtime):
    def run(incident, config):
        return asyncio.run(
            graph.ainvoke(incident, config=config, context=incident_runtime)
        )

    return run


def test_successful_execution_reaches_end(graph, invoke):
    config = {"configurable": {"thread_id": "INC-001"}}
    incident = {
        "incident_id": "INC-001",
        "user_report": "Checkout is returning HTTP 500 responses.",
        "target_service": "checkout",
    }

    result = invoke(incident, config)

    assert all(result[field] == value for field, value in incident.items())
    assert result["status"] == "investigating"
    assert len(result["evidence"]) == 4
    assert graph.get_state(config).next == ()


def test_checkpoint_contains_initialized_state(graph, invoke):
    config = {"configurable": {"thread_id": "INC-001"}}
    incident = {
        "incident_id": "INC-001",
        "user_report": "Checkout is failing.",
        "target_service": "checkout",
    }
    assert graph.get_state(config).values == {}

    result = invoke(incident, config)
    snapshot = graph.get_state(config)

    assert snapshot.values == result
    assert set(snapshot.values) == {
        "incident_id",
        "user_report",
        "target_service",
        "status",
        "evidence",
    }
    assert snapshot.config["configurable"]["thread_id"] == "INC-001"


def test_threads_retain_independent_state(graph, invoke):
    # Native thread IDs are caller-controlled and need not equal incident IDs.
    first_config = {"configurable": {"thread_id": "thread-one"}}
    second_config = {"configurable": {"thread_id": "thread-two"}}
    first = {
        "incident_id": "INC-001",
        "user_report": "Checkout is failing.",
        "target_service": "checkout",
    }
    second = {
        "incident_id": "INC-002",
        "user_report": "Inventory is timing out.",
        "target_service": "inventory",
    }

    first_result = invoke(first, first_config)
    second_result = invoke(second, second_config)

    assert graph.get_state(first_config).values == first_result
    assert graph.get_state(second_config).values == second_result
    assert first_result["user_report"] == first["user_report"]
    assert second_result["user_report"] == second["user_report"]


@pytest.mark.parametrize("field", ["incident_id", "user_report"])
@pytest.mark.parametrize("invalid", ["", " \t\n "])
def test_empty_or_whitespace_input_is_rejected(invoke, field, invalid):
    incident = {
        "incident_id": "INC-001",
        "user_report": "Checkout is failing.",
        "target_service": "checkout",
    }
    incident[field] = invalid

    with pytest.raises(ValueError, match=rf"{field} must be a non-empty string"):
        invoke(incident, {"configurable": {"thread_id": "invalid"}})


@pytest.mark.parametrize("field", ["incident_id", "user_report"])
def test_missing_required_input_is_rejected(invoke, field):
    incident = {
        "incident_id": "INC-001",
        "user_report": "Checkout is failing.",
        "target_service": "checkout",
    }
    del incident[field]

    with pytest.raises(ValueError, match=rf"{field} must be a non-empty string"):
        invoke(incident, {"configurable": {"thread_id": "missing"}})


def test_node_returns_update_without_mutating_or_normalizing_input():
    incident = {
        "incident_id": " INC-001 ",
        "user_report": " Checkout is failing. ",
        "target_service": "checkout",
    }
    original = incident.copy()

    assert initialize_incident(incident) == {"status": "investigating"}
    assert incident == original
