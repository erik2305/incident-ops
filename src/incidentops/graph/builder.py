"""Build the graph with checkpointing owned by the caller."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from incidentops.graph.nodes import (
    assess_evidence,
    collect_initial_evidence,
    collect_requested_evidence,
    escalate_evidence_limit,
    initialize_incident,
)
from incidentops.graph.routing import route_assessment
from incidentops.graph.runtime import IncidentRuntimeContext
from incidentops.graph.state import IncidentState


def build_graph(*, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    """Compile the incident graph; callers supply configurable.thread_id."""
    builder = StateGraph(IncidentState, context_schema=IncidentRuntimeContext)
    builder.add_node("initialize_incident", initialize_incident)
    builder.add_node("collect_initial_evidence", collect_initial_evidence)
    builder.add_node("assess_evidence", assess_evidence)
    builder.add_node("collect_requested_evidence", collect_requested_evidence)
    builder.add_node("escalate_evidence_limit", escalate_evidence_limit)
    builder.add_edge(START, "initialize_incident")
    builder.add_edge("initialize_incident", "collect_initial_evidence")
    builder.add_edge("collect_initial_evidence", "assess_evidence")
    builder.add_conditional_edges(
        "assess_evidence",
        route_assessment,
        {
            "end": END,
            "collect": "collect_requested_evidence",
            "limit": "escalate_evidence_limit",
        },
    )
    builder.add_edge("collect_requested_evidence", "assess_evidence")
    builder.add_edge("escalate_evidence_limit", END)
    return builder.compile(checkpointer=checkpointer)
