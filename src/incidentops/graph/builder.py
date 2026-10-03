"""Build the graph with checkpointing owned by the caller."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from incidentops.graph.nodes import initialize_incident
from incidentops.graph.state import IncidentState


def build_graph(*, checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    """Compile the incident graph; callers supply configurable.thread_id."""
    builder = StateGraph(IncidentState)
    builder.add_node("initialize_incident", initialize_incident)
    builder.add_edge(START, "initialize_incident")
    builder.add_edge("initialize_incident", END)
    return builder.compile(checkpointer=checkpointer)
