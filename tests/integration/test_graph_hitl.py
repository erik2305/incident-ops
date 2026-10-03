"""Pause A, approve in a fresh write-only B, inspect durable execution in C."""

import asyncio
import json
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


class ProposalReasoner:
    def __init__(self):
        self.calls = 0

    async def assess(self, **kwargs):
        self.calls += 1
        assert kwargs["evidence_round"] == 1 and len(kwargs["evidence"]) == 4
        return {
            "decision": "propose_remediation",
            "root_cause": "bad_deployment",
            "confidence": "high",
            "summary": "The v2 checkout calculation is failing; propose the observed v1.",
            "evidence_requests": [],
            "proposed_remediation": {
                "action": "rollback_deployment",
                "service": "checkout",
                "target_version": "v1",
            },
        }


async def runtime_a(env, incident, config):
    await setup_checkpoints(env["database"])
    async with open_checkpointer(env["database"]) as saver:
        graph = build_graph(checkpointer=saver)
        async with open_read_capabilities(
            env["observability"], env["operations"]
        ) as reads:
            reasoner = ProposalReasoner()
            result = await graph.ainvoke(
                incident,
                config=config,
                context=IncidentRuntimeContext(
                    read_capabilities=reads, reasoner=reasoner
                ),
            )
            snapshot = await graph.aget_state(config)
            assert snapshot.next == ("approval_gate",) and reasoner.calls == 1
            assert len(snapshot.interrupts) == 1
            assert result["__interrupt__"][0].value == snapshot.interrupts[0].value
            return snapshot.values, snapshot.interrupts[0].value


async def runtime_b(env, config, pending):
    async with open_checkpointer(env["database"]) as saver:
        graph = build_graph(checkpointer=saver)
        before = await graph.aget_state(config)
        assert before.values["pending_action"] == pending
        assert before.interrupts[0].value["action_id"] == pending["action_id"]
        async with open_write_capabilities(env["operations"]) as writes:
            result = await graph.ainvoke(
                Command(
                    resume={"decision": "approve", "action_id": pending["action_id"]}
                ),
                config=config,
                context=IncidentRuntimeContext(write_capabilities=writes),
            )
            assert (await graph.aget_state(config)).next == ()
            return result


async def runtime_c(database, config):
    async with open_checkpointer(database) as saver:
        graph = build_graph(checkpointer=saver)
        snapshot = await graph.aget_state(config)
        assert snapshot.next == () and not snapshot.interrupts
        return snapshot.values


def test_pending_approval_survives_closed_runtime_and_write_only_resume(
    rollback_environment,
):
    env = rollback_environment
    incident = {
        "incident_id": f"INC-HITL-{uuid4().hex}",
        "user_report": "Checkout returns HTTP 500 after deployment.",
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
            paused, request = run_async(runtime_a(env, incident, config))
            assert paused["status"] == "awaiting_approval"
            assert "approval_record" not in paused and "execution_record" not in paused
            pending = paused["pending_action"]
            assert request == {
                "kind": "approval_required",
                "incident_id": incident["incident_id"],
                "action_id": pending["action_id"],
                "fingerprint": pending["fingerprint"],
                "action": paused["proposal"],
            }
            assert checkout.get("/__ops/deployments").json()["active_version"] == "v2"
            assert checkout.post("/checkout", json=order).status_code == 500
            executed = run_async(runtime_b(env, config, pending))
            assert (
                executed["status"] == "action_executed"
                and executed["pending_action"] == pending
            )
            assert executed["approval_record"] == {
                "decision": "approve",
                "action_id": pending["action_id"],
                "fingerprint": pending["fingerprint"],
            }
            assert executed["execution_record"]["action_id"] == pending["action_id"]
            assert executed["execution_record"]["fingerprint"] == pending["fingerprint"]
            assert executed["execution_record"]["result"]["applied"] is True
            assert checkout.get("/__ops/deployments").json()["active_version"] == "v1"
            assert checkout.post("/checkout", json=order).status_code == 200
            restored = run_async(runtime_c(env["database"], config))
            assert restored == executed
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
                "approval_record",
                "execution_record",
            }
            serialized = json.dumps(restored, allow_nan=False)
            assert (
                env["operations"] not in serialized
                and env["observability"] not in serialized
            )
        finally:
            for client in (checkout, inventory):
                client.post("/__control/reset").raise_for_status()

            async def cleanup():
                async with open_checkpointer(env["database"]) as saver:
                    await saver.adelete_thread(incident["incident_id"])

            run_async(cleanup())
