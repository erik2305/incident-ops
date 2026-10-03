"""The initial, checkpoint-serializable graph state contract."""

from typing import Literal, NotRequired, TypedDict


class IncidentState(TypedDict):
    """Required incident input and the status added by initialization."""

    incident_id: str
    user_report: str
    status: NotRequired[Literal["investigating"]]
