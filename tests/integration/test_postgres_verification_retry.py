"""A fresh PostgreSQL runtime retries pending verification with read authority only."""

import asyncio
import os
from uuid import uuid4

import pytest
from langgraph.types import Command

from incidentops.domain.capabilities import ReadCapabilityError
from incidentops.graph import build_graph
from incidentops.graph.runtime import IncidentRuntimeContext
from incidentops.persistence import open_checkpointer, setup_checkpoints

pytestmark = pytest.mark.integration


def run_async(coroutine):
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coroutine)


def test_fresh_postgres_runtime_retries_verification_without_repeating_write(
    read_capabilities, reasoner, monkeypatch
):
    database = os.environ.get("INCIDENTOPS_TEST_DATABASE_URL")
    if not database:
        pytest.skip("Set INCIDENTOPS_TEST_DATABASE_URL to run PostgreSQL tests")
    incident = {
        "incident_id": "INC-VERIFY-RETRY-" + uuid4().hex,
        "user_report": "Errors after deployment.",
        "target_service": "checkout",
    }
    config = {"configurable": {"thread_id": incident["incident_id"]}}
    active, calls = {"version": "v2"}, []

    async def deployments(service, *, limit):
        read_capabilities.called("get_recent_deployments", service, limit)
        return {
            "active_version": active["version"],
            "deployment_history": [
                {"version": active["version"], "timestamp": "today"},
                {"version": "v1", "timestamp": "yesterday"},
            ],
        }

    monkeypatch.setattr(read_capabilities, "get_recent_deployments", deployments)
    reasoner.assessments = [
        {
            "decision": "propose_remediation",
            "root_cause": "bad_deployment",
            "confidence": "high",
            "summary": "Rollback to the observed v1.",
            "evidence_requests": [],
            "proposed_remediation": {
                "action": "rollback_deployment",
                "service": "checkout",
                "target_version": "v1",
            },
        }
    ]

    class Writes:
        async def rollback_deployment(self, *, service, target_version):
            calls.append((service, target_version))
            active["version"] = target_version
            return {
                "service": service,
                "previous_version": "v2",
                "active_version": target_version,
                "target_version": target_version,
                "applied": True,
            }

    async def runtime_a():
        await setup_checkpoints(database)
        async with open_checkpointer(database) as saver:
            graph = build_graph(checkpointer=saver)
            await graph.ainvoke(
                incident,
                config=config,
                context=IncidentRuntimeContext(
                    read_capabilities=read_capabilities, reasoner=reasoner
                ),
            )
            pending = (await graph.aget_state(config)).values["pending_action"]
            read_capabilities.fail_capability = "probe_checkout"
            with pytest.raises(ReadCapabilityError):
                await graph.ainvoke(
                    Command(
                        resume={
                            "decision": "approve",
                            "action_id": pending["action_id"],
                        }
                    ),
                    config=config,
                    context=IncidentRuntimeContext(
                        read_capabilities=read_capabilities, write_capabilities=Writes()
                    ),
                )
            snapshot = await graph.aget_state(config)
            assert snapshot.next == ("verify_recovery",)
            assert snapshot.values["status"] == "action_executed"
            assert (
                "verification_result" not in snapshot.values
                and "escalation_reason" not in snapshot.values
            )
            return snapshot.values["execution_record"]

    async def runtime_b(execution):
        read_capabilities.fail_capability = None
        async with open_checkpointer(database) as saver:
            graph = build_graph(checkpointer=saver)
            assert (await graph.aget_state(config)).next == ("verify_recovery",)
            result = await graph.ainvoke(
                None,
                config=config,
                context=IncidentRuntimeContext(read_capabilities=read_capabilities),
            )
            assert result["execution_record"] == execution
            assert (
                result["status"] == "resolved"
                and result["verification_result"]["recovered"] is True
            )
            assert not (await graph.aget_state(config)).next

    try:
        execution = run_async(runtime_a())
        run_async(runtime_b(execution))
        assert calls == [("checkout", "v1")] and len(reasoner.calls) == 1
    finally:

        async def cleanup():
            async with open_checkpointer(database) as saver:
                await saver.adelete_thread(incident["incident_id"])

        run_async(cleanup())
