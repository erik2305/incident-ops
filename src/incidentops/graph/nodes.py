"""Incident initialization and bounded, atomic read-only evidence collection."""

from typing import Literal

from langgraph.runtime import Runtime

from incidentops.domain.models import EvidenceItem
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
) -> dict[str, list[EvidenceItem]]:
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
    return {"evidence": evidence}
