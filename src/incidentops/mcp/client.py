"""Run-scoped official MCP clients implementing the application's fixed reads."""

import math
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Literal

from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
from httpx2 import HTTPError
from mcp import Client, MCPError

from incidentops.domain.actions import valid_rollback_result
from incidentops.domain.capabilities import ReadCapabilityError, WriteCapabilityError
from incidentops.domain.models import Capability, JSONData, Service
from incidentops.domain.verification import valid_probe_result


def is_json_value(value: object) -> bool:
    """Keep checkpoint data to JSON-like built-ins, including finite numbers."""
    if value is None or type(value) in (str, int, bool):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(is_json_value(item) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and is_json_value(item) for key, item in value.items()
        )
    return False


def records_match(value: object, fields: tuple[str, ...]) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, dict)
        and all(isinstance(item.get(field), str) for field in fields)
        for item in value
    )


def valid_result(
    capability: Capability | Literal["probe_checkout"], data: object
) -> bool:
    """Validate structured success at the consumer boundary, without text parsing."""
    if type(data) is not dict or not is_json_value(data):
        return False
    if capability == "probe_checkout":
        return valid_probe_result(data)
    if capability == "get_service_health":
        return all(isinstance(data.get(field), str) for field in ("service", "status"))
    if capability == "get_metrics":
        return all(
            type(data.get(field)) is int and data[field] >= 0
            for field in ("requests_total", "requests_5xx")
        )
    if capability == "query_logs":
        return records_match(
            data.get("logs"), ("timestamp", "level", "service", "message")
        )
    return isinstance(data.get("active_version"), str) and records_match(
        data.get("deployment_history"), ("version", "timestamp")
    )


class MCPReadCapabilities:
    """Only named reads are exposed to graph code; protocol dispatch is private."""

    def __init__(self, observability: Client, operations: Client):
        self._observability = observability
        self._operations = operations

    async def _read(
        self,
        client: Client,
        capability: Capability | Literal["probe_checkout"],
        service: Service,
        *,
        limit: int | None = None,
    ) -> JSONData:
        arguments = {} if capability == "probe_checkout" else {"service": service}
        if limit is not None:
            arguments["limit"] = limit
        try:
            result = await client.call_tool(capability, arguments)
        except (
            MCPError,
            HTTPError,
            BrokenResourceError,
            ClosedResourceError,
            EndOfStream,
            RuntimeError,
            ExceptionGroup,
        ):
            # Normalize an SDK/protocol/transport failure, never pretend success.
            raise ReadCapabilityError(
                f"{capability}({service}): MCP call failed"
            ) from None
        if result.is_error:
            raise ReadCapabilityError(f"{capability}({service}): MCP tool failed")
        if not valid_result(capability, result.structured_content):
            raise ReadCapabilityError(
                f"{capability}({service}): missing or invalid structured result"
            )
        return result.structured_content

    async def get_service_health(self, service: Service) -> JSONData:
        return await self._read(self._observability, "get_service_health", service)

    async def probe_checkout(self) -> JSONData:
        return await self._read(self._observability, "probe_checkout", "checkout")

    async def get_metrics(self, service: Service) -> JSONData:
        return await self._read(self._observability, "get_metrics", service)

    async def query_logs(self, service: Service, *, limit: int = 20) -> JSONData:
        return await self._read(self._observability, "query_logs", service, limit=limit)

    async def get_recent_deployments(
        self, service: Literal["checkout"], *, limit: int = 20
    ) -> JSONData:
        return await self._read(
            self._operations, "get_recent_deployments", service, limit=limit
        )


@asynccontextmanager
async def open_read_capabilities(observability_url: str, operations_url: str):
    """Open one fresh client per domain for this run and close both on exit."""
    async with AsyncExitStack() as stack:
        clients = []
        for domain, url in (
            ("observability", observability_url),
            ("operations", operations_url),
        ):
            try:
                client = Client(url, cache=None, read_timeout_seconds=10)
                clients.append(await stack.enter_async_context(client))
            except (
                MCPError,
                HTTPError,
                BrokenResourceError,
                ClosedResourceError,
                EndOfStream,
                RuntimeError,
                ValueError,
                ExceptionGroup,
            ):
                raise ReadCapabilityError(f"MCP {domain} connection failed") from None
        yield MCPReadCapabilities(clients[0], clients[1])


class MCPWriteCapabilities:
    """Write authority is separate from reads and exposes only the fixed rollback."""

    def __init__(self, operations: Client):
        self._operations = operations

    async def rollback_deployment(
        self, *, service: Literal["checkout"], target_version: str
    ) -> JSONData:
        if service != "checkout" or target_version not in ("v1", "v2"):
            raise WriteCapabilityError(
                "rollback_deployment(checkout): unsupported arguments"
            )
        try:
            result = await self._operations.call_tool(
                "rollback_deployment",
                {"service": service, "target_version": target_version},
            )
        except (
            MCPError,
            HTTPError,
            BrokenResourceError,
            ClosedResourceError,
            EndOfStream,
            RuntimeError,
            ExceptionGroup,
        ):
            raise WriteCapabilityError(
                "rollback_deployment(checkout): MCP call failed"
            ) from None
        if result.is_error:
            raise WriteCapabilityError("rollback_deployment(checkout): MCP tool failed")
        if not valid_rollback_result(result.structured_content, target_version):
            raise WriteCapabilityError(
                "rollback_deployment(checkout): missing or invalid structured result"
            )
        return result.structured_content


@asynccontextmanager
async def open_write_capabilities(operations_url: str):
    """A fresh operations-only connection, closed when this execution run ends."""
    async with AsyncExitStack() as stack:
        try:
            client = await stack.enter_async_context(
                Client(operations_url, cache=None, read_timeout_seconds=10)
            )
        except (
            MCPError,
            HTTPError,
            BrokenResourceError,
            ClosedResourceError,
            EndOfStream,
            RuntimeError,
            ValueError,
            ExceptionGroup,
        ):
            raise WriteCapabilityError(
                "MCP operations write connection failed"
            ) from None
        yield MCPWriteCapabilities(client)
