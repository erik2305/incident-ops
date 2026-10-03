"""Approval binds exact persisted arguments; rejection and corruption never write."""

import asyncio
from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime
from langgraph.types import Command

from incidentops.domain.actions import ActionIntegrityError
from incidentops.domain.capabilities import WriteCapabilityError
from incidentops.graph import build_graph
from incidentops.graph.nodes import approval_gate, execute_action, prepare_action
from incidentops.graph.runtime import IncidentRuntimeContext


class FakeWrites:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def rollback_deployment(self, *, service, target_version):
        self.calls.append({"service": service, "target_version": target_version})
        if self.fail:
            raise WriteCapabilityError("rollback_deployment(checkout): failed")
        return {
            "service": service,
            "previous_version": "v2",
            "active_version": target_version,
            "target_version": target_version,
            "applied": True,
        }


@pytest.fixture
def paused(read_capabilities, reasoner, monkeypatch):
    async def deployments(service, *, limit):
        read_capabilities.called("get_recent_deployments", service, limit)
        return {
            "active_version": "v2",
            "deployment_history": [
                {"version": "v2", "timestamp": "2026-10-03"},
                {"version": "v1", "timestamp": "2026-10-02"},
            ],
        }

    monkeypatch.setattr(read_capabilities, "get_recent_deployments", deployments)
    reasoner.assessments = [
        {
            "decision": "propose_remediation",
            "root_cause": "bad_deployment",
            "confidence": "high",
            "summary": "Rollback the bad deployment.",
            "evidence_requests": [],
            "proposed_remediation": {
                "action": "rollback_deployment",
                "service": "checkout",
                "target_version": "v1",
            },
        }
    ]
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "INC-HITL"}}
    context = IncidentRuntimeContext(
        read_capabilities=read_capabilities, reasoner=reasoner
    )
    writes = FakeWrites()
    result = asyncio.run(
        graph.ainvoke(
            {
                "incident_id": "INC-HITL",
                "user_report": "Errors",
                "target_service": "checkout",
            },
            config=config,
            context=context,
        )
    )
    return graph, config, result, writes


def resume(graph, config, decision, action_id, context=None, **extra):
    return asyncio.run(
        graph.ainvoke(
            Command(resume={"decision": decision, "action_id": action_id, **extra}),
            config=config,
            context=context,
        )
    )


def test_interrupt_exposes_exact_checkpointed_action_and_response_schema(paused):
    graph, config, result, writes = paused
    snapshot = graph.get_state(config)
    state = snapshot.values
    pending = state["pending_action"]
    assert state["status"] == "awaiting_approval"
    assert snapshot.next == ("approval_gate",)
    assert "approval_record" not in state and "execution_record" not in state
    assert writes.calls == []
    interrupted = result["__interrupt__"][0]
    assert (
        interrupted.value
        == snapshot.interrupts[0].value
        == {
            "kind": "approval_required",
            "incident_id": "INC-HITL",
            "action_id": pending["action_id"],
            "fingerprint": pending["fingerprint"],
            "action": state["proposal"],
        }
    )
    assert set(interrupted.response_schema["properties"]) == {"decision", "action_id"}
    assert interrupted.response_schema["additionalProperties"] is False


def test_approve_resumes_with_only_write_authority(paused, read_capabilities, reasoner):
    graph, config, _, writes = paused
    pending = graph.get_state(config).values["pending_action"]
    before = (len(read_capabilities.calls), len(reasoner.calls))
    result = resume(
        graph,
        config,
        "approve",
        pending["action_id"],
        IncidentRuntimeContext(write_capabilities=writes),
    )
    assert writes.calls == [
        {"service": pending["service"], "target_version": pending["target_version"]}
    ]
    assert (len(read_capabilities.calls), len(reasoner.calls)) == before
    assert result["status"] == "action_executed"
    assert result["execution_record"]["action_id"] == pending["action_id"]
    assert result["execution_record"]["fingerprint"] == pending["fingerprint"]
    assert graph.get_state(config).next == ()


def test_reject_requires_no_runtime_dependencies_and_never_writes(
    paused, read_capabilities, reasoner
):
    graph, config, _, writes = paused
    pending = graph.get_state(config).values["pending_action"]
    before = (len(read_capabilities.calls), len(reasoner.calls))
    result = resume(graph, config, "reject", pending["action_id"])
    assert writes.calls == [] and "execution_record" not in result
    assert (
        result["status"] == "escalated"
        and result["escalation_reason"] == "action_rejected"
    )
    assert (
        result["pending_action"] == pending
        and result["approval_record"]["decision"] == "reject"
    )
    assert (len(read_capabilities.calls), len(reasoner.calls)) == before


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"target_version": "v2"},
        {"service": "inventory"},
        {"fingerprint": "human-supplied"},
    ],
)
def test_wrong_id_or_replacement_arguments_never_write(paused, extra):
    graph, config, _, writes = paused
    pending = graph.get_state(config).values["pending_action"]
    action_id = "different-action" if not extra else pending["action_id"]
    with pytest.raises(ActionIntegrityError):
        resume(
            graph,
            config,
            "approve",
            action_id,
            IncidentRuntimeContext(write_capabilities=writes),
            **extra,
        )
    assert (
        writes.calls == [] and "approval_record" not in graph.get_state(config).values
    )


def execution_state(paused):
    graph, config, _, writes = paused
    state = deepcopy(graph.get_state(config).values)
    pending = state["pending_action"]
    state["approval_record"] = {
        "decision": "approve",
        "action_id": pending["action_id"],
        "fingerprint": pending["fingerprint"],
    }
    return state, writes


@pytest.mark.parametrize("node", ["approval", "execution"])
def test_payload_tampering_fails_before_interrupt_or_write(paused, node):
    state, writes = execution_state(paused)
    state["pending_action"]["target_version"] = "v2"
    with pytest.raises(ActionIntegrityError, match="fingerprint"):
        if node == "approval":
            approval_gate(state)
        else:
            asyncio.run(
                execute_action(
                    state,
                    Runtime(context=IncidentRuntimeContext(write_capabilities=writes)),
                )
            )
    assert writes.calls == []


@pytest.mark.parametrize(
    "change", ["absent", "reject", "action_id", "fingerprint", "executed"]
)
def test_executor_rejects_unapproved_or_replayed_action(paused, change):
    state, writes = execution_state(paused)
    if change == "absent":
        state.pop("approval_record")
    elif change == "reject":
        state["approval_record"]["decision"] = "reject"
    elif change == "executed":
        state["execution_record"] = {
            "action_id": state["pending_action"]["action_id"],
            "fingerprint": state["pending_action"]["fingerprint"],
            "result": {"applied": True},
        }
    else:
        state["approval_record"][change] = "different"
    with pytest.raises(ActionIntegrityError):
        asyncio.run(
            execute_action(
                state,
                Runtime(context=IncidentRuntimeContext(write_capabilities=writes)),
            )
        )
    assert writes.calls == []


def test_executor_ignores_changed_proposal_and_uses_only_pending_action(paused):
    state, writes = execution_state(paused)
    state["proposal"]["target_version"] = "v999"
    update = asyncio.run(
        execute_action(
            state, Runtime(context=IncidentRuntimeContext(write_capabilities=writes))
        )
    )
    assert writes.calls == [{"service": "checkout", "target_version": "v1"}]
    assert update["status"] == "action_executed"


def test_prepare_is_idempotent_and_refuses_existing_conflict(paused):
    state, _ = execution_state(paused)
    expected = prepare_action(state)
    assert expected["pending_action"] == state["pending_action"]
    state["pending_action"]["fingerprint"] = "different"
    with pytest.raises(ActionIntegrityError):
        prepare_action(state)


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_failed_write_keeps_approval_but_no_execution(paused, missing):
    graph, config, _, writes = paused
    writes.fail = True
    pending = graph.get_state(config).values["pending_action"]
    context = (
        IncidentRuntimeContext()
        if missing
        else IncidentRuntimeContext(write_capabilities=writes)
    )
    with pytest.raises(WriteCapabilityError):
        resume(graph, config, "approve", pending["action_id"], context)
    state = graph.get_state(config).values
    assert state["approval_record"]["decision"] == "approve"
    assert "execution_record" not in state and state["status"] != "action_executed"
    assert len(writes.calls) == (0 if missing else 1)
