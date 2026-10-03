"""API-owned request, state, result, and SSE payload contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from incidentops.domain.models import Service

Status = Literal[
    "investigating",
    "action_proposed",
    "awaiting_approval",
    "action_executed",
    "resolved",
    "escalated",
]
EventName = Literal[
    "incident_started", "progress", "approval_required", "completed", "error"
]


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StartIncident(APIModel):
    user_report: str = Field(min_length=1)
    target_service: Service

    @field_validator("user_report")
    @classmethod
    def nonblank_report(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("user_report must not be blank")
        return value


class ApprovalRequest(APIModel):
    decision: Literal["approve", "reject"]
    action_id: str = Field(min_length=1)


class IncidentView(APIModel):
    incident_id: str
    user_report: str
    target_service: Service
    status: Status
    evidence_round: int | None = None
    evidence: list[dict[str, JsonValue]] | None = None
    assessment_history: list[dict[str, JsonValue]] | None = None
    proposal: dict[str, JsonValue] | None = None
    pending_action: dict[str, JsonValue] | None = None
    approval_record: dict[str, JsonValue] | None = None
    execution_record: dict[str, JsonValue] | None = None
    verification_result: dict[str, JsonValue] | None = None
    escalation_reason: str | None = None


class IncidentResult(APIModel):
    incident_id: str
    status: Literal["resolved", "escalated"]
    root_cause: Literal["bad_deployment", "downstream_failure", "unknown"] | None = None
    approved_action: dict[str, JsonValue] | None = None
    execution_record: dict[str, JsonValue] | None = None
    verification_result: dict[str, JsonValue] | None = None
    escalation_reason: str | None = None


class StartedPayload(APIModel):
    incident_id: str
    status: Literal["investigating"] = "investigating"


class ProgressPayload(APIModel):
    incident_id: str
    node: str
    status: Status


class ApprovalRequiredPayload(APIModel):
    kind: Literal["approval_required"]
    incident_id: str
    action_id: str
    fingerprint: str
    action: dict[str, JsonValue]


class CompletedPayload(APIModel):
    incident_id: str
    status: Literal["resolved", "escalated"]
    escalation_reason: str | None = None


class ErrorPayload(APIModel):
    incident_id: str
    code: Literal["execution_failed"] = "execution_failed"
    message: str = "Incident execution failed; inspect the durable state."
