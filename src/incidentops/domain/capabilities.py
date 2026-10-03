"""The graph's fixed read surface, independent of MCP protocol mechanics."""

from typing import Literal, Protocol

from incidentops.domain.models import JSONData, Service


class ReadCapabilityError(RuntimeError):
    """A read failed; messages identify the operation without external payloads."""


class ReadCapabilities(Protocol):
    async def get_service_health(self, service: Service) -> JSONData: ...

    async def get_metrics(self, service: Service) -> JSONData: ...

    async def query_logs(self, service: Service, *, limit: int = 20) -> JSONData: ...

    async def get_recent_deployments(
        self, service: Literal["checkout"], *, limit: int = 20
    ) -> JSONData: ...


class WriteCapabilityError(RuntimeError):
    """A mutation integration failed; no successful execution may be recorded."""


class WriteCapabilities(Protocol):
    async def rollback_deployment(
        self, *, service: Literal["checkout"], target_version: str
    ) -> JSONData: ...
