"""Incident input and checkpointable evidence; runtime dependencies stay out."""

from typing import Literal, NotRequired, TypedDict

from incidentops.domain.actions import ApprovalRecord, ExecutionRecord, PendingAction
from incidentops.domain.models import EvidenceItem, Service
from incidentops.domain.reasoning import Assessment, RemediationProposal
from incidentops.domain.verification import VerificationResult


class IncidentState(TypedDict):
    """Explicit target input and the values added by the two graph nodes."""

    incident_id: str
    user_report: str
    target_service: Service
    status: NotRequired[
        Literal[
            "investigating",
            "action_proposed",
            "escalated",
            "awaiting_approval",
            "action_executed",
            "resolved",
        ]
    ]
    evidence: NotRequired[list[EvidenceItem]]
    evidence_round: NotRequired[Literal[1, 2]]
    assessment_history: NotRequired[list[Assessment]]
    proposal: NotRequired[RemediationProposal]
    escalation_reason: NotRequired[str]
    pending_action: NotRequired[PendingAction]
    approval_record: NotRequired[ApprovalRecord]
    execution_record: NotRequired[ExecutionRecord]
    verification_result: NotRequired[VerificationResult]
