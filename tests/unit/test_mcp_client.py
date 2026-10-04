"""Application-side result validation, fixed tool routing, and client lifecycle."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import MCPError

from incidentops.domain.capabilities import ReadCapabilityError, WriteCapabilityError
from incidentops.mcp.client import (
    MCPReadCapabilities,
    MCPWriteCapabilities,
    open_read_capabilities,
    open_write_capabilities,
)


@pytest.mark.parametrize(("ok", "status"), [(True, 200), (False, 500), (False, 502)])
def test_probe_success_uses_empty_arguments_and_observability_only(ok, status):
    data = {"service": "checkout", "ok": ok, "observed_status": status}
    obs = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(is_error=False, structured_content=data)
        )
    )
    ops = SimpleNamespace(call_tool=AsyncMock())
    assert asyncio.run(MCPReadCapabilities(obs, ops).probe_checkout()) == data
    obs.call_tool.assert_awaited_once_with("probe_checkout", {})
    ops.call_tool.assert_not_awaited()


@pytest.mark.parametrize(
    ("is_error", "data"),
    [
        (True, {"service": "checkout", "ok": False, "observed_status": 500}),
        (False, None),
        (False, {}),
        (False, {"service": "inventory", "ok": True, "observed_status": 200}),
        (False, {"service": "checkout", "ok": "false", "observed_status": 500}),
        (False, {"service": "checkout", "ok": False, "observed_status": 200}),
        (False, {"service": "checkout", "ok": True, "observed_status": True}),
        (False, {"service": "checkout", "ok": False, "observed_status": 999}),
        (
            False,
            {
                "service": "checkout",
                "ok": True,
                "observed_status": 200,
                "extra": object(),
            },
        ),
    ],
)
def test_probe_errors_and_malformed_structured_data_are_not_failed_business_probes(
    is_error, data
):
    client = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                is_error=is_error,
                structured_content=data,
                content=[
                    {"text": '{"service":"checkout","ok":true,"observed_status":200}'}
                ],
            )
        )
    )
    with pytest.raises(ReadCapabilityError, match="probe_checkout"):
        asyncio.run(MCPReadCapabilities(client, client).probe_checkout())


@pytest.mark.parametrize(
    ("capability", "service", "limit", "data", "domain"),
    [
        (
            "get_service_health",
            "inventory",
            None,
            {"service": "inventory-api", "status": "healthy"},
            "observability",
        ),
        (
            "get_metrics",
            "checkout",
            None,
            {"requests_total": 2, "requests_5xx": 1},
            "observability",
        ),
        ("query_logs", "checkout", 20, {"logs": []}, "observability"),
        (
            "get_recent_deployments",
            "checkout",
            10,
            {"active_version": "v2", "deployment_history": []},
            "operations",
        ),
    ],
)
def test_structured_success_uses_exact_tool_and_domain(
    capability, service, limit, data, domain
):
    result = SimpleNamespace(is_error=False, structured_content=data)
    observability = SimpleNamespace(call_tool=AsyncMock(return_value=result))
    operations = SimpleNamespace(call_tool=AsyncMock(return_value=result))
    adapter = MCPReadCapabilities(observability, operations)
    kwargs = {} if limit is None else {"limit": limit}

    assert asyncio.run(getattr(adapter, capability)(service, **kwargs)) == data
    selected = observability if domain == "observability" else operations
    other = operations if domain == "observability" else observability
    selected.call_tool.assert_awaited_once_with(
        capability, {"service": service, **kwargs}
    )
    other.call_tool.assert_not_awaited()


@pytest.mark.parametrize(
    ("is_error", "data"),
    [
        (True, {"requests_total": 99, "requests_5xx": 0}),
        (False, None),
        (False, {}),
        (False, {"logs": []}),
    ],
)
def test_failed_or_unstructured_result_is_not_evidence(is_error, data):
    client = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                is_error=is_error,
                structured_content=data,
                content=[{"text": '{"requests_total": 99, "requests_5xx": 0}'}],
            )
        )
    )
    adapter = MCPReadCapabilities(client, client)
    with pytest.raises(ReadCapabilityError, match="get_metrics\\(checkout\\)"):
        asyncio.run(adapter.get_metrics("checkout"))


def test_protocol_failure_is_normalized_without_framework_details():
    client = SimpleNamespace(
        call_tool=AsyncMock(
            side_effect=MCPError(code=-32603, message="secret transport internals")
        )
    )
    adapter = MCPReadCapabilities(client, client)
    with pytest.raises(ReadCapabilityError, match="get_metrics\\(checkout\\)") as error:
        asyncio.run(adapter.get_metrics("checkout"))
    assert "secret transport internals" not in str(error.value)


def test_non_json_success_is_rejected():
    client = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                is_error=False,
                structured_content={
                    "requests_total": 1,
                    "requests_5xx": 0,
                    "runtime_object": object(),
                },
            )
        )
    )
    with pytest.raises(ReadCapabilityError, match="structured result"):
        asyncio.run(MCPReadCapabilities(client, client).get_metrics("checkout"))


@pytest.fixture
def connected_clients(monkeypatch):
    instances = []

    class FakeClient:
        def __init__(self, url, **_kwargs):
            self.url = url
            self.connected = False
            self.calls = []
            self.fail_open = False
            instances.append(self)

        async def __aenter__(self):
            if self.fail_open:
                raise MCPError(code=-32603, message="connection internals")
            self.connected = True
            return self

        async def __aexit__(self, *_args):
            self.connected = False

        async def call_tool(self, name, arguments):
            assert self.connected
            self.calls.append((name, arguments))
            data = (
                {"service": "checkout-api", "status": "healthy"}
                if name == "get_service_health"
                else {"requests_total": 1, "requests_5xx": 0}
            )
            return SimpleNamespace(is_error=False, structured_content=data)

    monkeypatch.setattr("incidentops.mcp.client.Client", FakeClient)
    return instances, FakeClient


def test_one_connected_client_per_domain_is_reused_and_closed(connected_clients):
    instances, _ = connected_clients

    async def verify():
        async with open_read_capabilities(
            "http://observability/mcp", "http://operations/mcp"
        ) as capabilities:
            assert len(instances) == 2
            assert all(client.connected for client in instances)
            await capabilities.get_service_health("checkout")
            await capabilities.get_metrics("checkout")
        assert all(not client.connected for client in instances)
        assert len(instances[0].calls) == 2
        assert instances[1].calls == []

    asyncio.run(verify())


def test_second_connection_failure_closes_first(monkeypatch, connected_clients):
    instances, fake_client = connected_clients

    def factory(url, **kwargs):
        client = fake_client(url, **kwargs)
        client.fail_open = "operations" in url
        return client

    monkeypatch.setattr("incidentops.mcp.client.Client", factory)

    async def verify():
        with pytest.raises(ReadCapabilityError, match="operations connection failed"):
            async with open_read_capabilities(
                "http://observability/mcp", "http://operations/mcp"
            ):
                pytest.fail("Connection failure incorrectly yielded capabilities")
        assert all(not client.connected for client in instances)

    asyncio.run(verify())


def test_caller_exception_still_closes_both_clients(connected_clients):
    instances, _ = connected_clients

    async def verify():
        with pytest.raises(ValueError, match="caller failure"):
            async with open_read_capabilities(
                "http://observability/mcp", "http://operations/mcp"
            ):
                raise ValueError("caller failure")
        assert all(not client.connected for client in instances)

    asyncio.run(verify())


def rollback_result():
    return {
        "service": "checkout",
        "previous_version": "v2",
        "active_version": "v1",
        "target_version": "v1",
        "applied": True,
    }


def test_write_adapter_uses_only_exact_rollback_arguments():
    data = rollback_result()
    client = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(is_error=False, structured_content=data)
        )
    )
    assert (
        asyncio.run(
            MCPWriteCapabilities(client).rollback_deployment(
                service="checkout", target_version="v1"
            )
        )
        == data
    )
    client.call_tool.assert_awaited_once_with(
        "rollback_deployment", {"service": "checkout", "target_version": "v1"}
    )


@pytest.mark.parametrize(
    ("is_error", "data"),
    [
        (True, rollback_result()),
        (False, None),
        (False, {}),
        (False, {**rollback_result(), "active_version": "v2"}),
        (False, {**rollback_result(), "applied": "true"}),
        (False, {**rollback_result(), "runtime_object": object()}),
    ],
)
def test_write_error_or_invalid_structured_result_never_succeeds(is_error, data):
    client = SimpleNamespace(
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                is_error=is_error,
                structured_content=data,
                content=[{"text": "success"}],
            )
        )
    )
    with pytest.raises(WriteCapabilityError):
        asyncio.run(
            MCPWriteCapabilities(client).rollback_deployment(
                service="checkout", target_version="v1"
            )
        )


def test_write_protocol_failure_is_normalized():
    client = SimpleNamespace(
        call_tool=AsyncMock(
            side_effect=MCPError(code=-32603, message="secret transport details")
        )
    )
    with pytest.raises(WriteCapabilityError) as error:
        asyncio.run(
            MCPWriteCapabilities(client).rollback_deployment(
                service="checkout", target_version="v1"
            )
        )
    assert "secret" not in str(error.value)


def test_write_context_connects_only_operations_and_closes_on_caller_failure(
    connected_clients,
):
    instances, _ = connected_clients

    async def verify():
        with pytest.raises(ValueError, match="caller failure"):
            async with open_write_capabilities("http://operations/mcp"):
                assert len(instances) == 1 and instances[0].connected
                raise ValueError("caller failure")
        assert not instances[0].connected

    asyncio.run(verify())
