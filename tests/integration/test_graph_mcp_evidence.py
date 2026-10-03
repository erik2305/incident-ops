"""Live service evidence crosses MCP, LangGraph, and strict PostgreSQL storage."""

import asyncio
import json
import os
from uuid import uuid4

import httpx
import pytest

from incidentops.graph import build_graph
from incidentops.graph.runtime import IncidentRuntimeContext
from incidentops.mcp.client import open_read_capabilities
from incidentops.persistence import open_checkpointer, setup_checkpoints

pytestmark = pytest.mark.integration


def run_async(coroutine):
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coroutine)


@pytest.fixture
def environment():
    names = {
        "database": "INCIDENTOPS_TEST_DATABASE_URL",
        "checkout": "INCIDENTOPS_TEST_CHECKOUT_URL",
        "inventory": "INCIDENTOPS_TEST_INVENTORY_URL",
        "observability": "INCIDENTOPS_TEST_OBSERVABILITY_MCP_URL",
        "operations": "INCIDENTOPS_TEST_OPERATIONS_MCP_URL",
    }
    values = {name: os.environ.get(variable) for name, variable in names.items()}
    if not all(values.values()):
        pytest.skip("Configure database, synthetic service, and MCP test URLs")
    return values


async def runtime_a(environment, incident, config):
    await setup_checkpoints(environment["database"])
    async with open_checkpointer(environment["database"]) as checkpointer:
        graph = build_graph(checkpointer=checkpointer)
        async with open_read_capabilities(
            environment["observability"], environment["operations"]
        ) as capabilities:
            result = await graph.ainvoke(
                incident,
                config=config,
                context=IncidentRuntimeContext(read_capabilities=capabilities),
            )
            assert (await graph.aget_state(config)).next == ()
            return result
    # Returning exits both MCP clients and the PostgreSQL connection before B.


async def runtime_b(database_url, config):
    # No MCP capabilities/context is constructed merely to inspect a checkpoint.
    async with open_checkpointer(database_url) as checkpointer:
        graph = build_graph(checkpointer=checkpointer)
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ()
        return snapshot.values


def test_live_mcp_evidence_survives_closed_runtime(environment):
    incident = {
        "incident_id": f"INC-GRAPH-MCP-{uuid4().hex}",
        "user_report": "Checkout is returning HTTP 500 responses after a deployment.",
        "target_service": "checkout",
    }
    config = {"configurable": {"thread_id": incident["incident_id"]}}
    with (
        httpx.Client(
            base_url=environment["checkout"], timeout=10, trust_env=False
        ) as checkout,
        httpx.Client(
            base_url=environment["inventory"], timeout=10, trust_env=False
        ) as inventory,
    ):
        try:
            for service in (checkout, inventory):
                service.post("/__control/reset").raise_for_status()
            checkout.post(
                "/__control/deploy", json={"version": "v2"}
            ).raise_for_status()
            assert (
                checkout.post(
                    "/checkout", json={"sku": "SKU-001", "quantity": 2}
                ).status_code
                == 500
            )

            written = run_async(runtime_a(environment, incident, config))
            assert written["status"] == "investigating"
            evidence = {item["capability"]: item for item in written["evidence"]}
            assert set(evidence) == {
                "get_service_health",
                "get_metrics",
                "query_logs",
                "get_recent_deployments",
            }
            assert evidence["get_service_health"]["data"] == {
                "service": "checkout-api",
                "status": "healthy",
            }
            assert evidence["get_metrics"]["data"] == {
                "requests_total": 1,
                "requests_5xx": 1,
            }
            assert any(
                log["event"] == "checkout_error"
                and log["level"] == "ERROR"
                and log["version"] == "v2"
                for log in evidence["query_logs"]["data"]["logs"]
            )
            deployments = evidence["get_recent_deployments"]["data"]
            assert deployments["active_version"] == "v2"
            assert deployments["deployment_history"][0]["version"] == "v2"
            assert all(
                item["trust"] == "untrusted_operational_data"
                for item in written["evidence"]
            )

            restored = run_async(runtime_b(environment["database"], config))
            assert restored == written
            assert set(restored) == {
                "incident_id",
                "user_report",
                "target_service",
                "status",
                "evidence",
            }
            serialized = json.dumps(restored, allow_nan=False)
            assert environment["observability"] not in serialized
            assert environment["operations"] not in serialized
        finally:
            for service in (checkout, inventory):
                service.post("/__control/reset").raise_for_status()

            async def cleanup():
                async with open_checkpointer(environment["database"]) as checkpointer:
                    await checkpointer.adelete_thread(incident["incident_id"])

            run_async(cleanup())
