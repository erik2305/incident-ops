"""Real graph cycles, deterministic semantic validation and atomic extra reads."""

import asyncio
from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from incidentops.domain.capabilities import ReadCapabilityError
from incidentops.domain.reasoning import (
    ReasoningContractError,
    ReasoningError,
    validate_assessment,
)
from incidentops.graph import build_graph
from incidentops.graph.runtime import IncidentRuntimeContext


def proposal():
    return {
        "decision": "propose_remediation",
        "root_cause": "bad_deployment",
        "confidence": "high",
        "summary": "The failing deployment follows the prior version.",
        "evidence_requests": [],
        "proposed_remediation": {
            "action": "rollback_deployment",
            "service": "checkout",
            "target_version": "v1",
        },
    }


def more(*capabilities):
    return {
        "decision": "need_more_evidence",
        "root_cause": "unknown",
        "confidence": "low",
        "summary": "Check the downstream service.",
        "proposed_remediation": None,
        "evidence_requests": [
            {"capability": cap, "service": "inventory"} for cap in capabilities
        ],
    }


@pytest.fixture
def scenario(read_capabilities, reasoner, monkeypatch):
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
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "INC-REASON"}}
    context = IncidentRuntimeContext(
        read_capabilities=read_capabilities, reasoner=reasoner
    )

    def run():
        return asyncio.run(
            graph.ainvoke(
                {
                    "incident_id": "INC-REASON",
                    "user_report": "Errors",
                    "target_service": "checkout",
                },
                config=config,
                context=context,
            )
        )

    return run, graph, config


def test_direct_proposal_pauses_without_extra_reads(
    scenario, reasoner, read_capabilities
):
    reasoner.assessments = [proposal()]
    run, graph, config = scenario
    result = run()
    assert result["status"] == "awaiting_approval"
    assert result["proposal"] == proposal()["proposed_remediation"]
    assert result["evidence_round"] == 1
    assert len(reasoner.calls) == len(result["assessment_history"]) == 1
    assert len(read_capabilities.calls) == 4
    assert graph.get_state(config).next == ("approval_gate",)


def test_one_real_graph_cycle_appends_untrusted_evidence(
    scenario, reasoner, read_capabilities
):
    reasoner.assessments = [more("get_service_health", "get_metrics"), proposal()]
    run, graph, config = scenario
    result = run()
    assert result["status"] == "awaiting_approval"
    assert result["evidence_round"] == 2
    assert len(reasoner.calls) == len(result["assessment_history"]) == 2
    assert [call["evidence_round"] for call in reasoner.calls] == [1, 2]
    assert [len(call["evidence"]) for call in reasoner.calls] == [4, 6]
    assert read_capabilities.calls[4:] == [
        ("get_service_health", "inventory", None),
        ("get_metrics", "inventory", None),
    ]
    assert len(result["evidence"]) == 6
    assert all(
        item["trust"] == "untrusted_operational_data" for item in result["evidence"]
    )
    assert graph.get_state(config).next == ("approval_gate",)


def test_second_evidence_request_forces_terminal_budget_escalation(
    scenario, reasoner, read_capabilities
):
    reasoner.assessments = [
        more("get_service_health", "get_metrics"),
        more("query_logs"),
    ]
    run, graph, config = scenario
    result = run()
    assert result["status"] == "escalated"
    assert result["escalation_reason"] == "evidence_budget_exhausted"
    assert (
        result["evidence_round"]
        == len(reasoner.calls)
        == len(result["assessment_history"])
        == 2
    )
    assert len(read_capabilities.calls) == 6
    assert graph.get_state(config).next == ()


def test_extra_read_failure_does_not_publish_partial_round(
    scenario, reasoner, read_capabilities
):
    reasoner.assessments = [more("get_service_health", "get_metrics")]
    read_capabilities.fail_capability = "get_metrics"
    # Initial checkout metrics must succeed; fail only the additional inventory read.
    original = read_capabilities.get_metrics

    async def metrics(service):
        if service == "inventory":
            raise ReadCapabilityError("get_metrics(inventory): failed")
        read_capabilities.fail_capability = None
        return await original(service)

    read_capabilities.get_metrics = metrics
    run, graph, config = scenario
    with pytest.raises(ReadCapabilityError):
        run()
    saved = graph.get_state(config)
    assert len(saved.values["evidence"]) == 4
    assert saved.values["evidence_round"] == 1
    assert saved.next == ("collect_requested_evidence",)


@pytest.mark.parametrize(
    "change",
    [
        "inventory_deployments",
        "duplicate",
        "mixed_duplicate",
        "four_reads",
        "proposal_and_reads",
        "unobserved",
        "active",
        "wrong_action",
        "wrong_service",
        "wrong_root",
        "url",
        "limit",
        "escalation_and_reads",
    ],
)
def test_invalid_outputs_fail_without_assessment_checkpoint(change, scenario, reasoner):
    result = deepcopy(proposal())
    if change in (
        "inventory_deployments",
        "duplicate",
        "mixed_duplicate",
        "four_reads",
        "url",
        "limit",
    ):
        result = more("get_service_health")
        requests = result["evidence_requests"]
        if change == "inventory_deployments":
            requests[0]["capability"] = "get_recent_deployments"
        if change == "duplicate":
            requests[0]["service"] = "checkout"
        if change == "mixed_duplicate":
            requests.append({"capability": "get_metrics", "service": "checkout"})
        if change == "four_reads":
            requests *= 4
        if change == "url":
            requests[0]["url"] = "http://arbitrary"
        if change == "limit":
            requests[0]["limit"] = 999
    elif change == "proposal_and_reads":
        result["evidence_requests"] = more("get_metrics")["evidence_requests"]
    elif change == "unobserved":
        result["proposed_remediation"]["target_version"] = "v999"
    elif change == "active":
        result["proposed_remediation"]["target_version"] = "v2"
    elif change == "wrong_action":
        result["proposed_remediation"]["action"] = "restart"
    elif change == "wrong_service":
        result["proposed_remediation"]["service"] = "inventory"
    elif change == "wrong_root":
        result["root_cause"] = "downstream_failure"
    else:
        result = more("get_metrics")
        result["decision"] = "escalate"
    reasoner.assessments = [result]
    run, graph, config = scenario
    with pytest.raises(ReasoningContractError):
        run()
    assert "assessment_history" not in graph.get_state(config).values


def test_missing_reasoner_fails_clearly(scenario, reasoner):
    # Required runtime dependency can never silently fall back to a model/provider.
    from langgraph.runtime import Runtime

    from incidentops.graph.nodes import assess_evidence

    with pytest.raises(ReasoningError, match="reasoner is required"):
        asyncio.run(assess_evidence({}, Runtime(context=None)))


def test_no_deployment_evidence_cannot_authorize_proposal():
    with pytest.raises(ReasoningContractError, match="observed checkout deployments"):
        validate_assessment(proposal(), [])


def test_requested_logs_and_deployments_use_application_limits(
    read_capabilities, reasoner, incident_runtime
):
    escalation = deepcopy(reasoner.assessments[0])
    request = more("query_logs", "get_recent_deployments")
    for item in request["evidence_requests"]:
        item["service"] = "checkout"
    reasoner.assessments = [request, escalation]
    graph = build_graph(checkpointer=InMemorySaver())
    result = asyncio.run(
        graph.ainvoke(
            {
                "incident_id": "INC-INVENTORY",
                "user_report": "Inventory failures",
                "target_service": "inventory",
            },
            config={"configurable": {"thread_id": "INC-INVENTORY"}},
            context=incident_runtime,
        )
    )
    assert read_capabilities.calls[3:] == [
        ("query_logs", "checkout", 20),
        ("get_recent_deployments", "checkout", 10),
    ]
    assert len(result["evidence"]) == 5
    assert result["evidence_round"] == len(reasoner.calls) == 2
    assert result["status"] == "escalated"
