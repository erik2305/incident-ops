"""Structured conclusions and deterministic application-side validation."""

from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, ValidationError

from incidentops.domain.models import Capability, EvidenceItem, Service

Decision = Literal["need_more_evidence", "propose_remediation", "escalate"]
RootCause = Literal["bad_deployment", "downstream_failure", "unknown"]
Confidence = Literal["low", "medium", "high"]


class ReasoningError(RuntimeError):
    """Provider or structured-output processing failed; never an escalation."""


class ReasoningContractError(ReasoningError):
    """A structured decision violates the application's read/proposal contract."""


class EvidenceRequest(TypedDict):
    capability: Capability
    service: Service


class RemediationProposal(TypedDict):
    action: Literal["rollback_deployment"]
    service: Literal["checkout"]
    target_version: str


class Assessment(TypedDict):
    decision: Decision
    root_cause: RootCause
    confidence: Confidence
    summary: str
    evidence_requests: list[EvidenceRequest]
    proposed_remediation: RemediationProposal | None


class EvidenceRequestSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    capability: Capability
    service: Service


class RemediationProposalSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["rollback_deployment"]
    service: Literal["checkout"]
    target_version: str


class AssessmentSchema(BaseModel):
    """All fields required, with a nullable proposal, for native strict JSON schema."""

    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Decision
    root_cause: RootCause
    confidence: Confidence
    summary: str
    evidence_requests: list[EvidenceRequestSchema]
    proposed_remediation: RemediationProposalSchema | None


class IncidentReasoner(Protocol):
    async def assess(
        self,
        *,
        user_report: str,
        target_service: Service,
        evidence: list[EvidenceItem],
        evidence_round: int,
    ) -> Assessment: ...


def validate_assessment(value: object, evidence: list[EvidenceItem]) -> Assessment:
    """Validate even fake/custom reasoners; return only checkpoint-safe built-ins."""
    try:
        assessment = AssessmentSchema.model_validate(value).model_dump(mode="json")
    except ValidationError:
        raise ReasoningContractError("Invalid structured assessment") from None
    if not assessment["summary"].strip() or len(assessment["summary"]) > 600:
        raise ReasoningContractError("Assessment summary must be concise and non-empty")
    requests = assessment["evidence_requests"]
    proposal = assessment["proposed_remediation"]
    decision = assessment["decision"]
    if decision == "need_more_evidence":
        if proposal is not None or not 1 <= len(requests) <= 3:
            raise ReasoningContractError(
                "Evidence decision requires 1–3 reads and no proposal"
            )
        observed = {(item["capability"], item["service"]) for item in evidence}
        seen = set()
        for request in requests:
            pair = (request["capability"], request["service"])
            if pair == ("get_recent_deployments", "inventory"):
                raise ReasoningContractError(
                    "Inventory deployment reads are unsupported"
                )
            if pair in observed or pair in seen:
                raise ReasoningContractError(
                    "Evidence requests must be novel and unique"
                )
            seen.add(pair)
    elif decision == "propose_remediation":
        if requests or proposal is None or assessment["root_cause"] != "bad_deployment":
            raise ReasoningContractError(
                "Rollback decision requires bad_deployment and only a proposal"
            )
        deployments = [
            item["data"]
            for item in evidence
            if item["capability"] == "get_recent_deployments"
            and item["service"] == "checkout"
        ]
        target = proposal["target_version"]
        if not deployments:
            raise ReasoningContractError(
                "Rollback requires observed checkout deployments"
            )
        for data in deployments:
            history = data.get("deployment_history", [])
            versions = {
                item.get("version") for item in history if isinstance(item, dict)
            }
            latest = history[0].get("version") if history else None
            if target not in versions or target in (data.get("active_version"), latest):
                raise ReasoningContractError(
                    "Rollback target must be observed and not active/latest"
                )
    elif requests or proposal is not None:
        raise ReasoningContractError("Escalation cannot contain reads or a proposal")
    return assessment
