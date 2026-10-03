"""Narrow HTTP adapter checks for configuration and unexpected upstream data."""

import asyncio

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from incidentops.mcp.backend import HTTPBackend, backend_lifespan


@pytest.mark.parametrize(
    "url",
    ["", "ftp://checkout", "http://", "http://checkout/path", "http://checkout:bad"],
)
def test_invalid_essential_configuration_fails_before_startup(url):
    with pytest.raises(ValueError, match="HTTP"):
        backend_lifespan(url)


@pytest.mark.parametrize(
    ("endpoint", "response"),
    [
        ("health", httpx.Response(200, content=b"not JSON")),
        ("health", httpx.Response(200, json={})),
        (
            "metrics",
            httpx.Response(200, json={"requests_total": True, "requests_5xx": 0}),
        ),
        ("logs", httpx.Response(200, json=[{"message": "incomplete record"}])),
        ("deployments", httpx.Response(200, json={"active_version": "v1"})),
        ("metrics", httpx.Response(503, json={"detail": "temporarily unavailable"})),
        (
            "metrics",
            httpx.Response(302, headers={"Location": "http://unconfigured.invalid"}),
        ),
    ],
)
def test_invalid_upstream_response_is_a_tool_error(endpoint, response):
    async def verify():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: response), follow_redirects=False
        ) as client:
            backend = HTTPBackend(client, {"checkout": "http://checkout"})
            with pytest.raises(ToolError):
                await backend.read("checkout", endpoint)

    asyncio.run(verify())


def test_timeout_is_a_tool_error():
    def timeout(request):
        raise httpx.ReadTimeout("internal exception details", request=request)

    async def verify():
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            backend = HTTPBackend(client, {"checkout": "http://checkout"})
            with pytest.raises(ToolError, match="timed out") as error:
                await backend.read("checkout", "metrics")
            assert "internal exception details" not in str(error.value)

    asyncio.run(verify())


def test_log_records_and_additional_fields_are_preserved():
    records = [
        {
            "timestamp": "2026-10-03T00:00:00+00:00",
            "level": "INFO",
            "service": "checkout-api",
            "message": 'Opaque log data: <tag> {"text": "unchanged"}',
            "event": "checkout_completed",
            "request_id": "request-1",
        }
    ]

    async def verify():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=records))
        ) as client:
            backend = HTTPBackend(client, {"checkout": "http://checkout"})
            assert await backend.read("checkout", "logs") == records

    asyncio.run(verify())
