"""Approval binds exact persisted arguments; rejection and corruption never write."""

import asyncio
from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime
from langgraph.types import Command

from incidentops.domain.actions import ActionIntegrityError
from incidentops.domain.capabilities import ReadCapabilityError, WriteCapabilityError
from incidentops.graph import build_graph
from incidentops.graph.nodes import (
    approval_gate,
    execute_action,
    prepare_action,
    verify_recovery,
)
from incidentops.graph.routing import route_verification
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


def test_approve_resumes_with_reads_and_writes_without_reasoner(
    paused, read_capabilities, reasoner, monkeypatch
):
    graph, config, _, writes = paused
    pending = graph.get_state(config).values["pending_action"]
    before = (len(read_capabilities.calls), len(reasoner.calls))

    async def recovered_deployments(service, *, limit):
        read_capabilities.called("get_recent_deployments", service, limit)
        return {
            "active_version": "v1",
            "deployment_history": [{"version": "v1", "timestamp": "2026-10-03"}],
        }

    monkeypatch.setattr(
        read_capabilities, "get_recent_deployments", recovered_deployments
    )
    result = resume(
        graph,
        config,
        "approve",
        pending["action_id"],
        IncidentRuntimeContext(
            write_capabilities=writes, read_capabilities=read_capabilities
        ),
    )
    assert writes.calls == [
        {"service": pending["service"], "target_version": pending["target_version"]}
    ]
    assert len(reasoner.calls) == before[1]
    assert read_capabilities.calls[before[0] :] == [
        ("get_recent_deployments", "checkout", 3),
        ("probe_checkout", "checkout", None),
    ]
    assert result["status"] == "resolved"
    assert result["verification_result"]["recovered"] is True
    assert result["execution_record"]["action_id"] == pending["action_id"]
    assert result["execution_record"]["fingerprint"] == pending["fingerprint"]
    assert graph.get_state(config).next == ()
    assert asyncio.run(graph.ainvoke(None, config=config)) == result
    assert len(writes.calls) == 1


def test_reject_requires_no_runtime_dependencies_and_never_writes(
    paused, read_capabilities, reasoner
):
    graph, config, _, writes = paused
    pending = graph.get_state(config).values["pending_action"]
    before = (len(read_capabilities.calls), len(reasoner.calls))
    result = resume(graph, config, "reject", pending["action_id"])
    assert writes.calls == [] and "execution_record" not in result
    assert "verification_result" not in result
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


@pytest.mark.parametrize(
    "failure", ["missing", "get_recent_deployments", "probe_checkout"]
)
def test_verification_failure_preserves_execution_and_retries_only_reads(
    paused, read_capabilities, reasoner, failure, monkeypatch
):
    graph, config, _, writes = paused
    pending = graph.get_state(config).values["pending_action"]
    before_reasoning = len(reasoner.calls)
    read_capabilities.fail_capability = failure
    context = IncidentRuntimeContext(
        write_capabilities=writes,
        read_capabilities=None if failure == "missing" else read_capabilities,
    )
    with pytest.raises(ReadCapabilityError):
        resume(graph, config, "approve", pending["action_id"], context)
    snapshot = graph.get_state(config)
    assert snapshot.next == ("verify_recovery",)
    assert snapshot.values["status"] == "action_executed"
    assert "execution_record" in snapshot.values
    assert "verification_result" not in snapshot.values
    assert "escalation_reason" not in snapshot.values
    read_capabilities.fail_capability = None

    async def recovered_deployments(service, *, limit):
        read_capabilities.called("get_recent_deployments", service, limit)
        return {
            "active_version": "v1",
            "deployment_history": [{"version": "v1", "timestamp": "now"}],
        }

    monkeypatch.setattr(
        read_capabilities, "get_recent_deployments", recovered_deployments
    )
    result = asyncio.run(
        graph.ainvoke(
            None,
            config=config,
            context=IncidentRuntimeContext(read_capabilities=read_capabilities),
        )
    )
    assert result["status"] == "resolved"
    assert len(writes.calls) == 1 and len(reasoner.calls) == before_reasoning


@pytest.mark.parametrize(
    ("matches", "probe_ok"), [(True, True), (True, False), (False, True)]
)
def test_verification_decision_is_deterministic_and_checkpoint_safe(
    paused, read_capabilities, reasoner, monkeypatch, matches, probe_ok
):
    import json

    graph, config, _, writes = paused
    reasoning_calls = len(reasoner.calls)
    pending = graph.get_state(config).values["pending_action"]

    async def deployments(service, *, limit):
        read_capabilities.called("get_recent_deployments", service, limit)
        version = "v1" if matches else "v2"
        return {
            "active_version": version,
            "deployment_history": [{"version": version, "timestamp": "now"}],
        }

    async def probe():
        read_capabilities.called("probe_checkout", "checkout")
        return {
            "service": "checkout",
            "ok": probe_ok,
            "observed_status": 200 if probe_ok else 500,
        }

    monkeypatch.setattr(read_capabilities, "get_recent_deployments", deployments)
    monkeypatch.setattr(read_capabilities, "probe_checkout", probe)
    result = resume(
        graph,
        config,
        "approve",
        pending["action_id"],
        IncidentRuntimeContext(
            read_capabilities=read_capabilities, write_capabilities=writes
        ),
    )
    recovered = matches and probe_ok
    assert result["verification_result"] == {
        "action_id": pending["action_id"],
        "target_version": "v1",
        "deployment_matches_target": matches,
        "probe_ok": probe_ok,
        "probe_status": 200 if probe_ok else 500,
        "recovered": recovered,
    }
    assert (
        json.loads(json.dumps(result["verification_result"], allow_nan=False))
        == result["verification_result"]
    )
    assert result["status"] == ("resolved" if recovered else "escalated")
    if not recovered:
        assert result["escalation_reason"] == "verification_failed"
    assert len(writes.calls) == 1 and graph.get_state(config).next == ()
    assert len(reasoner.calls) == reasoning_calls


@pytest.mark.parametrize("bad_observation", ["deployments", "probe"])
def test_malformed_verification_observations_publish_no_conclusion(
    paused, read_capabilities, monkeypatch, bad_observation
):
    graph, config, _, writes = paused
    pending = graph.get_state(config).values["pending_action"]

    async def malformed(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        read_capabilities,
        "get_recent_deployments"
        if bad_observation == "deployments"
        else "probe_checkout",
        malformed,
    )
    with pytest.raises(ReadCapabilityError, match="invalid"):
        resume(
            graph,
            config,
            "approve",
            pending["action_id"],
            IncidentRuntimeContext(
                read_capabilities=read_capabilities, write_capabilities=writes
            ),
        )
    state = graph.get_state(config).values
    assert state["status"] == "action_executed" and "execution_record" in state
    assert "verification_result" not in state and "escalation_reason" not in state
    assert len(writes.calls) == 1


@pytest.mark.parametrize(
    "change", ["missing", "action_id", "fingerprint", "result", "approval"]
)
def test_verifier_checks_authority_before_reading(paused, read_capabilities, change):
    state, writes = execution_state(paused)
    state.update(
        asyncio.run(
            execute_action(
                state,
                Runtime(context=IncidentRuntimeContext(write_capabilities=writes)),
            )
        )
    )
    if change == "missing":
        state.pop("execution_record")
    elif change == "approval":
        state["approval_record"]["decision"] = "reject"
    else:
        state["execution_record"][change] = "invalid"
    before = len(read_capabilities.calls)
    with pytest.raises(ActionIntegrityError):
        asyncio.run(
            verify_recovery(
                state,
                Runtime(
                    context=IncidentRuntimeContext(read_capabilities=read_capabilities)
                ),
            )
        )
    assert len(read_capabilities.calls) == before


@pytest.mark.parametrize("recovered", [0, 1, "true", None])
def test_verification_route_requires_a_boolean(recovered):
    with pytest.raises(ValueError, match="boolean"):
        route_verification({"verification_result": {"recovered": recovered}})
