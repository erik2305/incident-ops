"""Known-service HTTP reads, lifecycle, and safe upstream tool errors."""

import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

Service = Literal["checkout", "inventory"]
Endpoint = Literal["health", "metrics", "logs", "deployments"]
EvidenceLimit = Annotated[int, Field(ge=1, le=100, strict=True)]


def configured_url(name: str) -> str:
    """Read essential runtime configuration, never tool-controlled URLs."""
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def validate_base_url(value: str) -> str:
    """Require an HTTP origin so evidence paths stay fixed by the application."""
    try:
        parts = urlsplit(value)
        valid = (
            value == value.strip()
            and parts.scheme in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and parts.path in {"", "/"}
            and not parts.query
            and not parts.fragment
            and (parts.port is None or parts.port > 0)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Service base URL must be an HTTP(S) origin")
    return value.rstrip("/")


def records_have_strings(value: Any, fields: tuple[str, ...]) -> bool:
    return isinstance(value, list) and all(
        isinstance(record, dict)
        and all(isinstance(record.get(field), str) for field in fields)
        for record in value
    )


def valid_evidence(endpoint: Endpoint, data: Any) -> bool:
    """Check the existing raw contracts without rewriting evidence or log text."""
    if endpoint == "logs":
        return records_have_strings(data, ("timestamp", "level", "service", "message"))
    if not isinstance(data, dict):
        return False
    if endpoint == "health":
        return all(isinstance(data.get(field), str) for field in ("service", "status"))
    if endpoint == "metrics":
        return all(
            type(data.get(field)) is int and data[field] >= 0
            for field in ("requests_total", "requests_5xx")
        )
    return isinstance(data.get("active_version"), str) and records_have_strings(
        data.get("deployment_history"), ("version", "timestamp")
    )


class HTTPBackend:
    """One lifespan-owned HTTP client; no retained operational evidence."""

    def __init__(self, client: httpx.AsyncClient, urls: dict[Service, str]):
        self.client = client
        self.urls = urls

    async def read(self, service: Service, endpoint: Endpoint) -> Any:
        if service not in self.urls:
            raise ToolError("Unsupported service")
        if endpoint not in {"health", "metrics", "logs", "deployments"}:
            raise ToolError("Unsupported evidence endpoint")
        url = f"{self.urls[service]}/__ops/{endpoint}"
        try:
            response = await self.client.get(url)
            response.raise_for_status()
        except httpx.TimeoutException:
            raise ToolError(f"{service} {endpoint} read timed out") from None
        except httpx.RequestError:
            raise ToolError(f"{service} {endpoint} is unavailable") from None
        except httpx.HTTPStatusError as error:
            raise ToolError(
                f"{service} {endpoint} returned HTTP {error.response.status_code}"
            ) from None
        try:
            data = response.json()
        except ValueError:
            raise ToolError(f"{service} {endpoint} returned invalid JSON") from None
        if not valid_evidence(endpoint, data):
            raise ToolError(f"{service} {endpoint} returned unexpected JSON")
        return data


def backend_lifespan(checkout_base_url: str, inventory_base_url: str | None = None):
    """Validate at construction and reuse an async client across all tool calls."""
    urls: dict[Service, str] = {"checkout": validate_base_url(checkout_base_url)}
    if inventory_base_url is not None:
        urls["inventory"] = validate_base_url(inventory_base_url)

    @asynccontextmanager
    async def lifespan(_server):
        async with httpx.AsyncClient(
            timeout=3, trust_env=False, follow_redirects=False
        ) as client:
            yield HTTPBackend(client, urls)

    return lifespan
