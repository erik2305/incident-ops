"""Run-scoped dependencies that must never become checkpointed graph state."""

from dataclasses import dataclass

from incidentops.domain.capabilities import ReadCapabilities
from incidentops.domain.reasoning import IncidentReasoner


@dataclass(frozen=True)
class IncidentRuntimeContext:
    read_capabilities: ReadCapabilities
    reasoner: IncidentReasoner
