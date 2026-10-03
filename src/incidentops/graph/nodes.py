"""Incident initialization and bounded, atomic read-only evidence collection."""

from typing import Literal

from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import ValidationError

from incidentops.domain.actions import (
    ActionIntegrityError,
    ApprovalDecision,
    action_payload,
    materialize_action,
    valid_rollback_result,
    validate_approval,
    validate_pending_action,
)
from incidentops.domain.capabilities import ReadCapabilityError, WriteCapabilityError
from incidentops.domain.models import EvidenceItem
from incidentops.domain.reasoning import (
    ReasoningContractError,
    ReasoningError,
    validate_assessment,
)
from incidentops.domain.verification import valid_probe_result
from incidentops.graph.runtime import IncidentRuntimeContext
from incidentops.graph.state import IncidentState


def initialize_incident(state: IncidentState) -> dict[str, Literal["investigating"]]:
    """Validate incident input and return the initial status update."""
    for field in ("incident_id", "user_report"):
        value = state.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
    if state.get("target_service") not in ("checkout", "inventory"):
        raise ValueError("target_service must be checkout or inventory")

    return {"status": "investigating"}


async def collect_initial_evidence(
    state: IncidentState, runtime: Runtime[IncidentRuntimeContext]
) -> dict:
    """Return a complete snapshot only after every required capability succeeds."""
    if runtime.context is None or runtime.context.read_capabilities is None:
        raise ValueError("Run-scoped read_capabilities are required")
    capabilities = runtime.context.read_capabilities
    service = state["target_service"]
    health = await capabilities.get_service_health(service)
    metrics = await capabilities.get_metrics(service)
    logs = await capabilities.query_logs(service, limit=20)
    evidence: list[EvidenceItem] = [
        {
            "capability": "get_service_health",
            "service": service,
            "data": health,
            "trust": "untrusted_operational_data",
        },
        {
            "capability": "get_metrics",
            "service": service,
            "data": metrics,
            "trust": "untrusted_operational_data",
        },
        {
            "capability": "query_logs",
            "service": service,
            "data": logs,
            "trust": "untrusted_operational_data",
        },
    ]
    if service == "checkout":
        deployments = await capabilities.get_recent_deployments("checkout", limit=10)
        evidence.append(
            {
                "capability": "get_recent_deployments",
                "service": service,
                "data": deployments,
                "trust": "untrusted_operational_data",
            }
        )
    return {"evidence": evidence, "evidence_round": 1}


async def assess_evidence(
    state: IncidentState, runtime: Runtime[IncidentRuntimeContext]
) -> dict:
    """Obtain a conclusion, validate it, and publish no provider/runtime objects."""
    if runtime.context is None or runtime.context.reasoner is None:
        raise ReasoningError("Run-scoped reasoner is required")
    history = state.get("assessment_history", [])
    if len(history) >= 2 or state["evidence_round"] not in (1, 2):
        raise ReasoningContractError("Assessment budget exceeded")
    result = await runtime.context.reasoner.assess(
        user_report=state["user_report"],
        target_service=state["target_service"],
        evidence=state["evidence"],
        evidence_round=state["evidence_round"],
    )
    assessment = validate_assessment(result, state["evidence"])
    update = {"assessment_history": [*history, assessment], "status": "investigating"}
    if assessment["decision"] == "propose_remediation":
        update.update(
            status="action_proposed", proposal=assessment["proposed_remediation"]
        )
    elif assessment["decision"] == "escalate":
        update.update(status="escalated", escalation_reason="model_escalation")
    return update


async def collect_requested_evidence(
    state: IncidentState, runtime: Runtime[IncidentRuntimeContext]
) -> dict:
    """Append one atomic round of at most three validated, fixed-method reads."""
    if state["evidence_round"] != 1:
        raise ReasoningContractError("Additional evidence budget exceeded")
    assessment = validate_assessment(state["assessment_history"][-1], state["evidence"])
    if assessment["decision"] != "need_more_evidence":
        raise ReasoningContractError(
            "Additional collection requires an evidence decision"
        )
    if runtime.context is None or runtime.context.read_capabilities is None:
        raise ValueError("Run-scoped read_capabilities are required")
    capabilities = runtime.context.read_capabilities
    collected: list[EvidenceItem] = []
    for request in assessment["evidence_requests"]:
        service = request["service"]
        capability = request["capability"]
        if capability == "get_service_health":
            data = await capabilities.get_service_health(service)
        elif capability == "get_metrics":
            data = await capabilities.get_metrics(service)
        elif capability == "query_logs":
            data = await capabilities.query_logs(service, limit=20)
        else:
            data = await capabilities.get_recent_deployments("checkout", limit=10)
        collected.append(
            {
                "capability": capability,
                "service": service,
                "data": data,
                "trust": "untrusted_operational_data",
            }
        )
    return {"evidence": [*state["evidence"], *collected], "evidence_round": 2}


def escalate_evidence_limit(state: IncidentState) -> dict:
    """Terminal application policy; makes no model or capability calls."""
    return {"status": "escalated", "escalation_reason": "evidence_budget_exhausted"}


def prepare_action(state: IncidentState) -> dict:
    """Pure preparation: validate the observed proposal and persist stable authority."""
    history = state.get("assessment_history", [])
    if not history:
        raise ActionIntegrityError(
            "A validated assessment is required before preparation"
        )
    assessment = validate_assessment(history[-1], state["evidence"])
    proposal = assessment["proposed_remediation"]
    if (
        assessment["decision"] != "propose_remediation"
        or state.get("proposal") != proposal
    ):
        raise ActionIntegrityError("Preparation requires the exact validated proposal")
    pending = materialize_action(state["incident_id"], proposal)
    if "pending_action" in state:
        previous = validate_pending_action(
            state["incident_id"], state["pending_action"]
        )
        if previous != pending:
            raise ActionIntegrityError(
                "Existing pending action conflicts with proposal"
            )
    return {"pending_action": pending, "status": "awaiting_approval"}


def approval_gate(state: IncidentState) -> dict:
    """Reentrant: all work before interrupt is deterministic and has no side effects."""
    pending = validate_pending_action(state["incident_id"], state.get("pending_action"))
    try:
        response = interrupt(
            {
                "kind": "approval_required",
                "incident_id": state["incident_id"],
                "action_id": pending["action_id"],
                "fingerprint": pending["fingerprint"],
                "action": action_payload(pending),
            },
            response_schema=ApprovalDecision,
        )
        # Convert the validated response to built-ins; never catch GraphInterrupt.
        decision = ApprovalDecision.model_validate(response).model_dump(mode="json")
    except ValidationError:
        raise ActionIntegrityError("Invalid approval response") from None
    if decision["action_id"] != pending["action_id"]:
        raise ActionIntegrityError("Approval action_id does not match pending action")
    record = {**decision, "fingerprint": pending["fingerprint"]}
    if "approval_record" in state and state["approval_record"] != record:
        raise ActionIntegrityError("Conflicting approval record")
    return {"approval_record": record}


async def execute_action(
    state: IncidentState, runtime: Runtime[IncidentRuntimeContext]
) -> dict:
    """Only persisted, integrity-checked, human-approved arguments reach a write."""
    pending = validate_pending_action(state["incident_id"], state.get("pending_action"))
    validate_approval(state.get("approval_record"), pending, "approve")
    if "execution_record" in state:
        raise ActionIntegrityError("Action already has an execution record")
    if runtime.context is None or runtime.context.write_capabilities is None:
        raise WriteCapabilityError("Run-scoped write_capabilities are required")
    result = await runtime.context.write_capabilities.rollback_deployment(
        service=pending["service"], target_version=pending["target_version"]
    )
    if not valid_rollback_result(result, pending["target_version"]):
        raise WriteCapabilityError(
            "rollback_deployment(checkout): invalid structured result"
        )
    return {
        "status": "action_executed",
        "execution_record": {
            "action_id": pending["action_id"],
            "fingerprint": pending["fingerprint"],
            "result": result,
        },
    }


def reject_action(state: IncidentState) -> dict:
    pending = validate_pending_action(state["incident_id"], state.get("pending_action"))
    validate_approval(state.get("approval_record"), pending, "reject")
    return {"status": "escalated", "escalation_reason": "action_rejected"}


async def verify_recovery(
    state: IncidentState, runtime: Runtime[IncidentRuntimeContext]
) -> dict:
    """Observe once after execution; publish both observations atomically."""
    pending = validate_pending_action(state["incident_id"], state.get("pending_action"))
    validate_approval(state.get("approval_record"), pending, "approve")
    execution = state.get("execution_record")
    if (
        type(execution) is not dict
        or set(execution) != {"action_id", "fingerprint", "result"}
        or execution["action_id"] != pending["action_id"]
        or execution["fingerprint"] != pending["fingerprint"]
        or not valid_rollback_result(execution["result"], pending["target_version"])
    ):
        raise ActionIntegrityError(
            "Verification requires the matching execution record"
        )
    if runtime.context is None or runtime.context.read_capabilities is None:
        raise ReadCapabilityError("Verification requires run-scoped read_capabilities")
    reads = runtime.context.read_capabilities
    deployments = await reads.get_recent_deployments("checkout", limit=3)
    # Validate application inputs even when the adapter is supplied by a caller.
    if (
        type(deployments) is not dict
        or type(deployments.get("active_version")) is not str
        or type(deployments.get("deployment_history")) is not list
        or not deployments["deployment_history"]
        or any(
            type(item) is not dict
            or type(item.get("version")) is not str
            or type(item.get("timestamp")) is not str
            for item in deployments["deployment_history"]
        )
    ):
        raise ReadCapabilityError(
            "Verification received invalid deployment observations"
        )
    probe = await reads.probe_checkout()
    if not valid_probe_result(probe):
        raise ReadCapabilityError("Verification received invalid probe observations")
    matches = (
        deployments["active_version"] == pending["target_version"]
        and deployments["deployment_history"][0]["version"] == pending["target_version"]
    )
    return {
        "verification_result": {
            "action_id": pending["action_id"],
            "target_version": pending["target_version"],
            "deployment_matches_target": matches,
            "probe_ok": probe["ok"],
            "probe_status": probe["observed_status"],
            "recovered": matches and probe["ok"] and probe["observed_status"] == 200,
        }
    }


def finalize_resolved(state: IncidentState) -> dict:
    return {"status": "resolved"}


def finalize_verification_failure(state: IncidentState) -> dict:
    return {"status": "escalated", "escalation_reason": "verification_failed"}
