"""Run-scoped dependencies that must never become checkpointed graph state."""

from dataclasses import dataclass

from incidentops.domain.capabilities import ReadCapabilities


@dataclass(frozen=True)
class IncidentRuntimeContext:
    read_capabilities: ReadCapabilities
