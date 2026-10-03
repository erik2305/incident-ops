"""Behavioral coverage of execution, checkpointing, and input validation."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from incidentops.graph import build_graph
from incidentops.graph.nodes import initialize_incident


@pytest.fixture
def graph():
    return build_graph(checkpointer=InMemorySaver())


def test_successful_execution_reaches_end(graph):
    config = {"configurable": {"thread_id": "INC-001"}}
    incident = {
        "incident_id": "INC-001",
        "user_report": "Checkout is returning HTTP 500 responses.",
    }

    result = graph.invoke(incident, config=config)

    assert result == {**incident, "status": "investigating"}
    assert graph.get_state(config).next == ()


def test_checkpoint_contains_initialized_state(graph):
    config = {"configurable": {"thread_id": "INC-001"}}
    incident = {"incident_id": "INC-001", "user_report": "Checkout is failing."}
    assert graph.get_state(config).values == {}

    graph.invoke(incident, config=config)
    snapshot = graph.get_state(config)

    assert snapshot.values == {**incident, "status": "investigating"}
    assert snapshot.config["configurable"]["thread_id"] == "INC-001"


def test_threads_retain_independent_state(graph):
    # Native thread IDs are caller-controlled and need not equal incident IDs.
    first_config = {"configurable": {"thread_id": "thread-one"}}
    second_config = {"configurable": {"thread_id": "thread-two"}}
    first = {"incident_id": "INC-001", "user_report": "Checkout is failing."}
    second = {"incident_id": "INC-002", "user_report": "Search is timing out."}

    graph.invoke(first, config=first_config)
    graph.invoke(second, config=second_config)

    assert graph.get_state(first_config).values == {**first, "status": "investigating"}
    assert graph.get_state(second_config).values == {
        **second,
        "status": "investigating",
    }


@pytest.mark.parametrize("field", ["incident_id", "user_report"])
@pytest.mark.parametrize("invalid", ["", " \t\n "])
def test_empty_or_whitespace_input_is_rejected(graph, field, invalid):
    incident = {"incident_id": "INC-001", "user_report": "Checkout is failing."}
    incident[field] = invalid

    with pytest.raises(ValueError, match=rf"{field} must be a non-empty string"):
        graph.invoke(incident, config={"configurable": {"thread_id": "invalid"}})


@pytest.mark.parametrize("field", ["incident_id", "user_report"])
def test_missing_required_input_is_rejected(graph, field):
    incident = {"incident_id": "INC-001", "user_report": "Checkout is failing."}
    del incident[field]

    with pytest.raises(ValueError, match=rf"{field} must be a non-empty string"):
        graph.invoke(incident, config={"configurable": {"thread_id": "missing"}})


def test_node_returns_update_without_mutating_or_normalizing_input():
    incident = {"incident_id": " INC-001 ", "user_report": " Checkout is failing. "}
    original = incident.copy()

    assert initialize_incident(incident) == {"status": "investigating"}
    assert incident == original
