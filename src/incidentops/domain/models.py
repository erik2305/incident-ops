"""Small built-in, checkpoint-compatible operational evidence contracts."""

from typing import Literal, TypeAlias, TypedDict

Service: TypeAlias = Literal["checkout", "inventory"]
Capability: TypeAlias = Literal[
    "get_service_health", "get_metrics", "query_logs", "get_recent_deployments"
]
JSONValue: TypeAlias = (
    str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
)
JSONData: TypeAlias = dict[str, JSONValue]


class EvidenceItem(TypedDict):
    """Operational evidence is data, never an instruction to the application."""

    capability: Capability
    service: Service
    data: JSONData
    trust: Literal["untrusted_operational_data"]
