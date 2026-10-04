"""Live OpenRouter proposal, non-mutation, and fresh PostgreSQL recovery."""

import asyncio
import json
import os
from uuid import uuid4

import httpx
import pytest

from incidentops.graph import build_graph
from incidentops.graph.runtime import IncidentRuntimeContext
from incidentops.llm.openrouter import open_openrouter_reasoner
from incidentops.mcp.client import open_read_capabilities
from incidentops.persistence import open_checkpointer, setup_checkpoints

pytestmark = [pytest.mark.integration, pytest.mark.live_llm]


def run_async(coroutine):
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coroutine)


@pytest.fixture
def live_environment():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        pytest.skip("OPENROUTER_API_KEY is absent")
    names = {
        "database": "INCIDENTOPS_TEST_DATABASE_URL",
        "checkout": "INCIDENTOPS_TEST_CHECKOUT_URL",
        "inventory": "INCIDENTOPS_TEST_INVENTORY_URL",
        "observability": "INCIDENTOPS_TEST_OBSERVABILITY_MCP_URL",
        "operations": "INCIDENTOPS_TEST_OPERATIONS_MCP_URL",
        "model": "INCIDENTOPS_TEST_LLM_MODEL",
    }
    values = {name: os.environ.get(variable) for name, variable in names.items()}
    if not all(values.values()):
        pytest.fail(
            "Live reasoning requires explicit model, database, synthetic and MCP URLs"
        )
    # Never print configuration or the key, including in assertion diagnostics.
    return values


async def write_runtime(environment, incident, config):
    await setup_checkpoints(environment["database"])
    async with open_checkpointer(environment["database"]) as saver:
        graph = build_graph(checkpointer=saver)
        async with (
            open_read_capabilities(
                environment["observability"], environment["operations"]
            ) as capabilities,
            open_openrouter_reasoner(
                api_key=os.environ["OPENROUTER_API_KEY"], model=environment["model"]
            ) as reasoner,
        ):
            result = await graph.ainvoke(
                incident,
                config=config,
                context=IncidentRuntimeContext(
                    read_capabilities=capabilities, reasoner=reasoner
                ),
            )
            snapshot = await graph.aget_state(config)
            assert snapshot.next == ("approval_gate",)
            assert len(snapshot.interrupts) == 1
            request = snapshot.interrupts[0].value
            assert (
                request["action_id"] == snapshot.values["pending_action"]["action_id"]
            )
            assert (
                request["fingerprint"]
                == snapshot.values["pending_action"]["fingerprint"]
            )
            assert request["action"] == snapshot.values["proposal"]
            assert result["__interrupt__"][0].value == request
            return snapshot.values


async def read_runtime(database, config):
    async with open_checkpointer(database) as saver:
        graph = build_graph(checkpointer=saver)
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("approval_gate",)
        assert (
            snapshot.interrupts[0].value["action_id"]
            == snapshot.values["pending_action"]["action_id"]
        )
        return snapshot.values


def test_live_reasoning_proposes_without_execution_and_persists(live_environment):
    env = live_environment
    incident = {
        "incident_id": f"INC-LLM-{uuid4().hex}",
        "user_report": "Checkout is returning HTTP 500 responses after a deployment.",
        "target_service": "checkout",
    }
    config = {"configurable": {"thread_id": incident["incident_id"]}}
    with (
        httpx.Client(base_url=env["checkout"], timeout=10, trust_env=False) as checkout,
        httpx.Client(
            base_url=env["inventory"], timeout=10, trust_env=False
        ) as inventory,
    ):
        try:
            for client in (checkout, inventory):
                client.post("/__control/reset").raise_for_status()
            checkout.post(
                "/__control/deploy", json={"version": "v2"}
            ).raise_for_status()
            order = {"sku": "SKU-001", "quantity": 2}
            assert checkout.post("/checkout", json=order).status_code == 500
            written = run_async(write_runtime(env, incident, config))
            assert written["status"] == "awaiting_approval"
            assert (
                "approval_record" not in written and "execution_record" not in written
            )
            assert written["assessment_history"][-1]["root_cause"] == "bad_deployment"
            assert written["proposal"] == {
                "action": "rollback_deployment",
                "service": "checkout",
                "target_version": "v1",
            }
            assert 1 <= written["evidence_round"] <= 2
            assert len(written["assessment_history"]) == written["evidence_round"]
            assert all(
                item["trust"] == "untrusted_operational_data"
                for item in written["evidence"]
            )
            # Raw read only for the independent post-proposal non-mutation assertion.
            deployment = checkout.get("/__ops/deployments")
            deployment.raise_for_status()
            assert deployment.json()["active_version"] == "v2"
            assert checkout.post("/checkout", json=order).status_code == 500
            # A has closed the model SDK, both MCP clients, saver and event loop.
            restored = run_async(read_runtime(env["database"], config))
            assert restored == written
            assert set(restored) == {
                "incident_id",
                "user_report",
                "target_service",
                "status",
                "evidence",
                "evidence_round",
                "assessment_history",
                "proposal",
                "pending_action",
            }
            serialized = json.dumps(restored, allow_nan=False)
            assert (
                env["observability"] not in serialized
                and env["operations"] not in serialized
            )
            assert os.environ["OPENROUTER_API_KEY"] not in serialized
            print(
                f"Live model={env['model']}; assessments={len(written['assessment_history'])}; evidence_rounds={written['evidence_round']}"
            )
        finally:
            for client in (checkout, inventory):
                client.post("/__control/reset").raise_for_status()

            async def cleanup():
                async with open_checkpointer(env["database"]) as saver:
                    await saver.adelete_thread(incident["incident_id"])

            run_async(cleanup())
