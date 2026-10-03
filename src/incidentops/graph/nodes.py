"""Incident initialization and bounded, atomic read-only evidence collection."""

from typing import Literal

from langgraph.runtime import Runtime

from incidentops.domain.models import EvidenceItem
from incidentops.domain.reasoning import (
    ReasoningContractError,
    ReasoningError,
    validate_assessment,
)
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
