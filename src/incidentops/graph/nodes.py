"""Pure state transitions for the incident graph."""

from typing import Literal

from incidentops.graph.state import IncidentState


def initialize_incident(state: IncidentState) -> dict[str, Literal["investigating"]]:
    """Validate incident input and return the initial status update."""
    for field in ("incident_id", "user_report"):
        value = state.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")

    return {"status": "investigating"}
