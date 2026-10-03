"""Build the graph with checkpointing owned by the caller."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from incidentops.graph.nodes import collect_initial_evidence, initialize_incident
from incidentops.graph.runtime import IncidentRuntimeContext
from incidentops.graph.state import IncidentState


def build_graph(*, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    """Compile the incident graph; callers supply configurable.thread_id."""
    builder = StateGraph(IncidentState, context_schema=IncidentRuntimeContext)
    builder.add_node("initialize_incident", initialize_incident)
    builder.add_node("collect_initial_evidence", collect_initial_evidence)
    builder.add_edge(START, "initialize_incident")
    builder.add_edge("initialize_incident", "collect_initial_evidence")
    builder.add_edge("collect_initial_evidence", END)
    return builder.compile(checkpointer=checkpointer)
