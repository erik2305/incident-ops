"""Incident input and checkpointable evidence; runtime dependencies stay out."""

from typing import Literal, NotRequired, TypedDict

from incidentops.domain.models import EvidenceItem, Service
from incidentops.domain.reasoning import Assessment, RemediationProposal


class IncidentState(TypedDict):
    """Explicit target input and the values added by the two graph nodes."""

    incident_id: str
    user_report: str
    target_service: Service
    status: NotRequired[Literal["investigating", "action_proposed", "escalated"]]
    evidence: NotRequired[list[EvidenceItem]]
    evidence_round: NotRequired[Literal[1, 2]]
    assessment_history: NotRequired[list[Assessment]]
    proposal: NotRequired[RemediationProposal]
    escalation_reason: NotRequired[str]
