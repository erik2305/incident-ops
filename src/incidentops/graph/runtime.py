"""Run-scoped dependencies that must never become checkpointed graph state."""

from dataclasses import dataclass

from incidentops.domain.capabilities import ReadCapabilities, WriteCapabilities
from incidentops.domain.reasoning import IncidentReasoner


@dataclass(frozen=True)
class IncidentRuntimeContext:
    read_capabilities: ReadCapabilities | None = None
    reasoner: IncidentReasoner | None = None
    write_capabilities: WriteCapabilities | None = None
