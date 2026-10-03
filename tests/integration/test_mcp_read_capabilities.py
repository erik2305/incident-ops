"""Official MCP clients over real Streamable HTTP and synthetic service HTTP."""

import asyncio
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx
import pytest
from mcp import Client

pytestmark = pytest.mark.integration
ORDER = {"sku": "SKU-001", "quantity": 2}


@pytest.fixture
def endpoints():
    observability = os.environ.get("INCIDENTOPS_TEST_OBSERVABILITY_MCP_URL")
    operations = os.environ.get("INCIDENTOPS_TEST_OPERATIONS_MCP_URL")
    if not observability and not operations:
        pytest.skip("Set observability and operations MCP test URLs")
    urls = {
        "observability": observability,
        "operations": operations,
        "checkout": os.environ.get("INCIDENTOPS_TEST_CHECKOUT_URL"),
        "inventory": os.environ.get("INCIDENTOPS_TEST_INVENTORY_URL"),
    }
    if not all(urls.values()):
        pytest.fail("Configure both MCP URLs and both synthetic service test URLs")
    return urls


@pytest.fixture(autouse=True)
def raw_services(endpoints):
    # Raw controls only prepare/reset the environment; evidence reads use MCP.
    with (
        httpx.Client(
            base_url=endpoints["checkout"], timeout=10, trust_env=False
        ) as checkout,
        httpx.Client(
            base_url=endpoints["inventory"], timeout=10, trust_env=False
        ) as inventory,
    ):
        try:
            for service in (checkout, inventory):
                service.post("/__control/reset").raise_for_status()
            yield checkout, inventory
        finally:
            for service in (checkout, inventory):
                service.post("/__control/reset").raise_for_status()


async def evidence(client, name, arguments):
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


def test_exact_read_only_tool_sets(endpoints):
    async def verify():
        expected = {
            "observability": {"get_service_health", "get_metrics", "query_logs"},
            "operations": {"get_recent_deployments"},
        }
        for domain, names in expected.items():
            async with Client(endpoints[domain], cache=None) as client:
                tools = (await client.list_tools()).tools
                assert {tool.name for tool in tools} == names
                for tool in tools:
                    assert tool.annotations.read_only_hint is True
                    assert tool.annotations.open_world_hint is False
                    assert tool.output_schema is not None
                    properties = tool.input_schema["properties"]
                    assert set(properties) <= {"service", "limit"}
                    service_schema = properties["service"]
                    if domain == "observability":
                        assert set(service_schema["enum"]) == {"checkout", "inventory"}
                    else:
                        assert service_schema["const"] == "checkout"
                    if "limit" in properties:
                        assert properties["limit"]["minimum"] == 1
                        assert properties["limit"]["maximum"] == 100

    asyncio.run(verify())


def test_baseline_evidence_through_mcp(endpoints, raw_services):
    checkout, _ = raw_services
    assert checkout.post("/checkout", json=ORDER).status_code == 200

    async def verify():
        async with Client(endpoints["observability"], cache=None) as client:
            for service in ("checkout", "inventory"):
                assert await evidence(
                    client, "get_service_health", {"service": service}
                ) == {"service": f"{service}-api", "status": "healthy"}
                assert await evidence(client, "get_metrics", {"service": service}) == {
                    "requests_total": 1,
                    "requests_5xx": 0,
                }
            logs = await evidence(
                client, "query_logs", {"service": "checkout", "limit": 1}
            )
            assert len(logs["logs"]) == 1
            assert logs["logs"][0]["event"] == "checkout_completed"
            inventory_logs = await evidence(
                client, "query_logs", {"service": "inventory"}
            )
            assert inventory_logs["logs"][0]["event"] == "inventory_read"
        async with Client(endpoints["operations"], cache=None) as client:
            deployments = await evidence(client, "get_recent_deployments", {})
            assert deployments["active_version"] == "v1"
            assert [
                entry["version"] for entry in deployments["deployment_history"]
            ] == ["v1"]

    asyncio.run(verify())


def test_bad_deployment_live_evidence_through_mcp(endpoints, raw_services):
    checkout, _ = raw_services

    async def verify():
        async with (
            Client(endpoints["observability"], cache=None) as observability,
            Client(endpoints["operations"], cache=None) as operations,
        ):
            before = await evidence(
                observability, "get_metrics", {"service": "checkout"}
            )
            assert before == {"requests_total": 0, "requests_5xx": 0}
            checkout.post(
                "/__control/deploy", json={"version": "v2"}
            ).raise_for_status()
            for _ in range(2):
                assert checkout.post("/checkout", json=ORDER).status_code == 500
            for service in ("checkout", "inventory"):
                assert (
                    await evidence(
                        observability, "get_service_health", {"service": service}
                    )
                )["status"] == "healthy"
            assert await evidence(
                observability, "get_metrics", {"service": "checkout"}
            ) == {"requests_total": 2, "requests_5xx": 2}
            assert await evidence(
                observability, "get_metrics", {"service": "inventory"}
            ) == {"requests_total": 2, "requests_5xx": 0}
            logs = (
                await evidence(
                    observability, "query_logs", {"service": "checkout", "limit": 100}
                )
            )["logs"]
            errors = [log for log in logs if log["event"] == "checkout_error"]
            assert len(errors) == 2
            assert all(
                log["level"] == "ERROR" and log["version"] == "v2" for log in errors
            )
            deployed = await evidence(
                operations, "get_recent_deployments", {"limit": 1}
            )
            assert deployed["active_version"] == "v2"
            assert len(deployed["deployment_history"]) == 1
            assert deployed["deployment_history"][0]["version"] == "v2"
            # Same connected MCP clients must read fresh values after recovery.
            checkout.post(
                "/__control/deploy", json={"version": "v1"}
            ).raise_for_status()
            assert checkout.post("/checkout", json=ORDER).status_code == 200
            assert await evidence(
                observability, "get_metrics", {"service": "checkout"}
            ) == {"requests_total": 3, "requests_5xx": 2}
            versions = await evidence(
                operations, "get_recent_deployments", {"limit": 2}
            )
            assert [entry["version"] for entry in versions["deployment_history"]] == [
                "v1",
                "v2",
            ]

    asyncio.run(verify())


def test_tool_inputs_reject_urls_unknown_services_and_unbounded_limits(endpoints):
    async def verify():
        async with Client(endpoints["observability"], cache=None) as client:
            for name in ("get_service_health", "get_metrics", "query_logs"):
                for invalid in (
                    "unknown",
                    "http://127.0.0.1:9/arbitrary",
                    "../checkout",
                ):
                    result = await client.call_tool(name, {"service": invalid})
                    assert result.is_error
                    assert result.structured_content is None
            for limit in (0, 101, -1):
                assert (
                    await client.call_tool(
                        "query_logs", {"service": "checkout", "limit": limit}
                    )
                ).is_error
        async with Client(endpoints["operations"], cache=None) as client:
            for service in ("inventory", "http://127.0.0.1:9/arbitrary"):
                assert (
                    await client.call_tool(
                        "get_recent_deployments", {"service": service}
                    )
                ).is_error
            for limit in (0, 101):
                assert (
                    await client.call_tool("get_recent_deployments", {"limit": limit})
                ).is_error

    asyncio.run(verify())


@contextmanager
def local_failure_server(unreachable_url, tmp_path):
    # A separate local test process with controlled bad configuration; no Compose
    # services are stopped or changed to exercise upstream failure semantics.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    env = {
        **os.environ,
        "CHECKOUT_BASE_URL": unreachable_url,
        "INVENTORY_BASE_URL": unreachable_url,
        "MCP_HOST": "127.0.0.1",
        "MCP_PORT": str(port),
    }
    log_path = tmp_path / "failure-server.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "incidentops.mcp.observability_server"],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                if process.poll() is not None:
                    pytest.fail(
                        f"Local MCP process exited: {log_path.read_text(encoding='utf-8')}"
                    )
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        pytest.fail("Local MCP process did not become ready")
                    time.sleep(0.05)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_upstream_failure_is_a_failed_mcp_tool_call(tmp_path):
    # Binding without listening reserves a guaranteed unreachable local port.
    with socket.socket() as unreachable:
        unreachable.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{unreachable.getsockname()[1]}"
        with local_failure_server(url, tmp_path) as mcp_url:

            async def verify():
                async with Client(
                    mcp_url, cache=None, read_timeout_seconds=10
                ) as client:
                    result = await client.call_tool(
                        "get_metrics", {"service": "checkout"}
                    )
                    assert result.is_error is True
                    assert result.structured_content is None
                    assert result.content
                    text = " ".join(
                        block.text for block in result.content if block.type == "text"
                    )
                    assert "Traceback" not in text
                    assert 'File "' not in text

            asyncio.run(verify())
