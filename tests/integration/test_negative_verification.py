"""Real rollback success while downstream failure prevents recovery, across runtimes."""

import asyncio
from uuid import uuid4

import httpx
import pytest
from langgraph.types import Command

from incidentops.graph import build_graph
from incidentops.graph.runtime import IncidentRuntimeContext
from incidentops.mcp.client import open_read_capabilities, open_write_capabilities
from incidentops.persistence import open_checkpointer, setup_checkpoints

pytestmark = pytest.mark.integration


def run_async(coroutine):
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coroutine)


class DeliberateRollbackReasoner:
    """Propose the observed v1 deliberately; this test is not reasoning-quality evaluation."""

    def __init__(self):
        self.calls = 0

    async def assess(self, **kwargs):
        self.calls += 1
        return {
            "decision": "propose_remediation",
            "root_cause": "bad_deployment",
            "confidence": "high",
            "summary": "Deliberately test rollback recovery verification.",
            "evidence_requests": [],
            "proposed_remediation": {
                "action": "rollback_deployment",
                "service": "checkout",
                "target_version": "v1",
            },
        }


def test_real_rollback_succeeds_but_downstream_prevents_recovery(rollback_environment):
    env = rollback_environment
    incident = {
        "incident_id": "INC-NEGATIVE-" + uuid4().hex,
        "user_report": "Checkout downstream failure after deployment.",
        "target_service": "checkout",
    }
    config = {"configurable": {"thread_id": incident["incident_id"]}}
    reasoner = DeliberateRollbackReasoner()
    writes = []

    async def runtime_a():
        await setup_checkpoints(env["database"])
        async with (
            open_checkpointer(env["database"]) as saver,
            open_read_capabilities(env["observability"], env["operations"]) as reads,
        ):
            graph = build_graph(checkpointer=saver)
            await graph.ainvoke(
                incident,
                config=config,
                context=IncidentRuntimeContext(
                    read_capabilities=reads, reasoner=reasoner
                ),
            )
            snapshot = await graph.aget_state(config)
            assert snapshot.next == ("approval_gate",) and snapshot.interrupts
            assert snapshot.values["pending_action"]["target_version"] == "v1"
            assert snapshot.values["status"] == "awaiting_approval"
            return snapshot.values

    async def runtime_b(pending):
        async with (
            open_checkpointer(env["database"]) as saver,
            open_read_capabilities(env["observability"], env["operations"]) as reads,
            open_write_capabilities(env["operations"]) as real_writes,
        ):

            class CountWrites:
                async def rollback_deployment(self, *, service, target_version):
                    writes.append(
                        {"service": service, "target_version": target_version}
                    )
                    return await real_writes.rollback_deployment(
                        service=service, target_version=target_version
                    )

            graph = build_graph(checkpointer=saver)
            result = await graph.ainvoke(
                Command(
                    resume={"decision": "approve", "action_id": pending["action_id"]}
                ),
                config=config,
                context=IncidentRuntimeContext(
                    read_capabilities=reads, write_capabilities=CountWrites()
                ),
            )
            assert (await graph.aget_state(config)).next == ()
            assert await graph.ainvoke(None, config=config) == result
            return result

    async def runtime_c():
        async with open_checkpointer(env["database"]) as saver:
            snapshot = await build_graph(checkpointer=saver).aget_state(config)
            assert not snapshot.next and not snapshot.interrupts
            return snapshot.values

    with (
        httpx.Client(base_url=env["checkout"], trust_env=False, timeout=10) as checkout,
        httpx.Client(
            base_url=env["inventory"], trust_env=False, timeout=10
        ) as inventory,
    ):
        try:
            for raw in (checkout, inventory):
                raw.post("/__control/reset").raise_for_status()
            checkout.post(
                "/__control/deploy", json={"version": "v2"}
            ).raise_for_status()
            inventory.post(
                "/__control/fault", json={"mode": "unavailable"}
            ).raise_for_status()
            order = {"sku": "SKU-001", "quantity": 1}
            assert checkout.post("/checkout", json=order).status_code == 502
            paused = run_async(runtime_a())
            assert not writes and reasoner.calls == 1
            before = checkout.get("/__ops/deployments").json()
            assert before["active_version"] == "v2"
            pending = paused["pending_action"]
            finished = run_async(runtime_b(pending))
            assert writes == [{"service": "checkout", "target_version": "v1"}]
            assert reasoner.calls == 1
            assert finished["status"] == "escalated"
            assert finished["escalation_reason"] == "verification_failed"
            assert finished["execution_record"]["action_id"] == pending["action_id"]
            assert finished["execution_record"]["result"]["applied"] is True
            assert finished["verification_result"] == {
                "action_id": pending["action_id"],
                "target_version": "v1",
                "deployment_matches_target": True,
                "probe_ok": False,
                "probe_status": 502,
                "recovered": False,
            }
            after = checkout.get("/__ops/deployments").json()
            assert after["active_version"] == "v1"
            assert after["deployment_history"][:-1] == before["deployment_history"]
            assert (
                len(after["deployment_history"])
                == len(before["deployment_history"]) + 1
            )
            assert checkout.post("/checkout", json=order).status_code == 502
            assert inventory.get("/__ops/health").json()["status"] == "healthy"
            assert inventory.get("/inventory/SKU-001").status_code == 503
            assert run_async(runtime_c()) == finished
        finally:
            for raw in (checkout, inventory):
                raw.post("/__control/reset").raise_for_status()

            async def cleanup():
                async with open_checkpointer(env["database"]) as saver:
                    await saver.adelete_thread(incident["incident_id"])

            run_async(cleanup())
