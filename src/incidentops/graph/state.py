"""Incident input and checkpointable evidence; runtime dependencies stay out."""

from typing import Literal, NotRequired, TypedDict

from incidentops.domain.models import EvidenceItem, Service


class IncidentState(TypedDict):
    """Explicit target input and the values added by the two graph nodes."""

    incident_id: str
    user_report: str
    target_service: Service
    status: NotRequired[Literal["investigating"]]
    evidence: NotRequired[list[EvidenceItem]]
