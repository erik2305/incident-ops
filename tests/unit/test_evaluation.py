"""Evaluator boundaries and independent metrics, using the production graph."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from incidentops.evaluation import runner
from incidentops.evaluation.__main__ import evaluate
from incidentops.evaluation.freeze import (
    reasoning_fingerprint,
    require_freeze,
    write_freeze,
)
from incidentops.evaluation.measurement import ScriptedReasoner
from incidentops.evaluation.metrics import aggregate, grade, history_delta
from incidentops.evaluation.models import RunRecord, Scenario
from incidentops.evaluation.report import render_report, save_report
from incidentops.evaluation.scenarios import KNOWN, load_scenarios

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def scenarios():
    return {s.id: s for s in load_scenarios(ROOT / "scenarios")}


def truth(*versions):
    return {
        "active_version": versions[-1],
        "inventory_health": "healthy",
        "deployment_history": [
            {"version": v, "timestamp": str(i)} for i, v in enumerate(versions)
        ],
    }


def record_for(scenario):
    action = scenario.expected.expected_action
    return RunRecord(
        scenario_id=scenario.id,
        split=scenario.split,
        reasoner_mode="scripted",
        model=None,
        attempt=1,
        incident_id="INC-test",
        expected_root_cause=scenario.expected.root_cause,
        expected_terminal_status=scenario.expected.terminal_status,
        expected_action=action.model_dump() if action else None,
        human_decision=scenario.human_decision,
        expected_mutation_count=scenario.expected.expected_mutation_count,
    )


def test_exact_manifest_set_and_frozen_split(scenarios, tmp_path):
    assert len(scenarios) == 6
    assert sum(s.split == "dev" for s in scenarios.values()) == 4
    assert sum(s.split == "holdout" for s in scenarios.values()) == 2
    for scenario in scenarios.values():
        folder = tmp_path / scenario.split
        folder.mkdir(exist_ok=True)
        (folder / f"{scenario.id}.json").write_text(scenario.model_dump_json())
    bad = next(tmp_path.glob("dev/*.json"))
    value = json.loads(bad.read_text())
    value["split"] = "holdout"
    bad.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="reassigned"):
        load_scenarios(tmp_path)
    bad.unlink()
    with pytest.raises(ValueError, match="six known"):
        load_scenarios(tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("service", "inventory"),
        ("target_version", "v999"),
        ("action", "restart"),
        ("url", "http://evil"),
    ],
)
def test_invalid_expected_actions_rejected(scenarios, field, value):
    data = scenarios["dev_bad_deployment_approve"].model_dump()
    data["expected"]["expected_action"][field] = value
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)


def test_invalid_human_decision_rejected(scenarios):
    data = scenarios["dev_bad_deployment_approve"].model_dump()
    data["human_decision"] = "force"
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)


def test_mutation_count_excludes_setup_and_separates_safety(scenarios):
    scenario = scenarios["dev_bad_deployment_approve"]
    record = record_for(scenario)
    record.baseline, record.at_interrupt = truth("v1", "v2"), truth("v1", "v2")
    record.final_environment = truth("v1", "v2", "v1")
    record.active_version_after = "v1"
    record.observed_pending_action = {
        **record.expected_action,
        "action_id": "act",
        "fingerprint": "fp",
    }
    record.observed_root_cause = "bad_deployment"
    record.observed_terminal_status = "resolved"
    record.terminal_persisted = True
    grade(scenario, record)
    assert record.observed_mutation_count == 1 and record.functional_pass
    assert not record.safety_violations
    # The same final outcome can hide an earlier unauthorized mutation.
    record.at_interrupt = record.final_environment
    grade(scenario, record)
    assert record.functional_pass and record.security_blocker
    assert aggregate([record])["clean_passes"] == 0
    assert record.mutation_before_approval


@pytest.mark.parametrize(
    "scenario_id", ["dev_bad_deployment_reject", "dev_downstream_failure"]
)
def test_reject_and_no_approval_mutations_are_safety_violations(scenarios, scenario_id):
    scenario = scenarios[scenario_id]
    record = record_for(scenario)
    record.baseline, record.final_environment = (
        truth("v1", "v2"),
        truth("v1", "v2", "v1"),
    )
    grade(scenario, record)
    assert record.unexpected_mutation_count == 1 and record.security_blocker


def test_duplicates_and_missing_baselines_are_not_hidden(scenarios):
    record = record_for(scenarios["dev_bad_deployment_approve"])
    record.baseline = record.at_interrupt = truth("v1", "v2")
    record.final_environment = truth("v1", "v2", "v1", "v2", "v1")
    grade(scenarios[record.scenario_id], record)
    assert record.observed_mutation_count == 3
    assert record.duplicate_mutation_count == 1
    assert record.unexpected_mutation_count == 1
    with pytest.raises(ValueError):
        history_delta(truth("v1", "v2"), truth("v1"))
    record = record_for(scenarios["dev_downstream_failure"])
    grade(scenarios[record.scenario_id], record)
    assert not record.safety_measured and not record.functional_pass
    assert aggregate([record])["unmeasured_safety_runs"] == 1


def test_human_response_only_uses_surfaced_identity(scenarios):
    action = {
        "action": "rollback_deployment",
        "service": "checkout",
        "target_version": "v1",
    }
    snapshot = SimpleNamespace(
        interrupts=[SimpleNamespace(value={"action_id": "surfaced", "action": action})],
        values={"pending_action": {**action, "action_id": "surfaced"}},
    )
    for decision in ("approve", "reject"):
        scenario = scenarios[f"dev_bad_deployment_{decision}"]
        assert runner.human_response(scenario, snapshot) == {
            "decision": decision,
            "action_id": "surfaced",
        }
    assert runner.human_response(scenarios["dev_downstream_failure"], snapshot) is None
    snapshot.interrupts[0].value["action_id"] = "different"
    with pytest.raises(ValueError):
        runner.human_response(scenarios["dev_bad_deployment_approve"], snapshot)


@pytest.mark.parametrize("change", ["prompt", "schema", "model", "configuration"])
def test_reasoning_fingerprint_relevant_changes(change):
    values = {
        "model": "model",
        "prompt": "trusted",
        "schema": {"type": "object"},
        "configuration": {"seed": 0},
    }
    baseline = reasoning_fingerprint(**values)
    assert reasoning_fingerprint(**values) == baseline
    values[change] = (
        {"changed": True} if change in ("schema", "configuration") else "changed"
    )
    assert reasoning_fingerprint(**values) != baseline


def dev_result():
    return {
        "provider": "live",
        "split": "dev",
        "complete": True,
        "evaluation_id": "dev",
        "runs": [
            {"scenario_id": key, "attempt": attempt}
            for key, (split, _) in KNOWN.items()
            if split == "dev"
            for attempt in range(1, 4)
        ],
    }


def test_freeze_requires_dev_and_cannot_be_overwritten(tmp_path):
    path = tmp_path / "freeze.json"
    result = dev_result()
    result["complete"] = False
    with pytest.raises(ValueError):
        write_freeze(path, "model", "fingerprint", result)
    result["complete"] = True
    write_freeze(path, "model", "fingerprint", result)
    assert require_freeze(path, "model", "fingerprint")["dev_evaluation_id"] == "dev"
    with pytest.raises(FileExistsError):
        write_freeze(path, "model", "fingerprint", result)
    with pytest.raises(ValueError):
        require_freeze(path, "changed", "fingerprint")
    with pytest.raises(ValueError):
        require_freeze(path, "model", "changed")


@pytest.mark.parametrize("mismatch", [False, True])
def test_live_holdout_refuses_before_environment_creation(
    tmp_path, monkeypatch, mismatch
):
    monkeypatch.chdir(ROOT)
    path = tmp_path / "freeze.json"
    if mismatch:
        write_freeze(path, "model", "wrong", dev_result())
    args = SimpleNamespace(
        provider="live",
        split="holdout",
        require_freeze=path,
        runs=3,
        write_freeze=None,
        output=tmp_path,
    )
    config = runner.EvaluationConfig(
        "private-db", "checkout", "inventory", "obs", "ops", "model", "SECRET"
    )
    with pytest.raises(ValueError):
        asyncio.run(evaluate(args, config))
    assert not list(tmp_path.glob("live-*.json"))


def test_report_is_derived_and_does_not_serialize_configuration(scenarios, tmp_path):
    scenario = scenarios["dev_downstream_failure"]
    record = record_for(scenario)
    data = {
        "provider": "scripted",
        "split": "all",
        "model": "scripted",
        "reasoning_fingerprint": "hash",
        "scenarios": {scenario.id: scenario.model_dump()},
    }
    path = tmp_path / "results.json"
    save_report(path, data, [record])
    stored = json.loads(path.read_text())
    assert stored["aggregate"]["attempts"] == 1
    assert path.with_suffix(".md").read_text() == render_report(stored)
    assert "api_key" not in path.read_text() and "database" not in path.read_text()


def test_scripted_reads_mcp_log_envelope():
    evidence = [
        {
            "capability": "query_logs",
            "service": "checkout",
            "data": {"logs": [{"event": "checkout_error", "status_code": 500}]},
        }
    ]
    result = asyncio.run(
        ScriptedReasoner().assess(
            user_report="report",
            target_service="checkout",
            evidence=evidence,
            evidence_round=1,
        )
    )
    assert result["root_cause"] == "bad_deployment"


def test_truth_separation_and_unexpected_interrupt_never_acquire_writes(
    scenarios, read_capabilities, monkeypatch
):
    saver = InMemorySaver()
    calls = []

    @asynccontextmanager
    async def opened_saver(*args):
        yield saver

    @asynccontextmanager
    async def opened_reads(*args):
        yield read_capabilities

    async def setup(*args):
        return None

    async def deployments(*args, **kwargs):
        return {
            "active_version": "v2",
            "deployment_history": [
                {"version": "v2", "timestamp": "today"},
                {"version": "v1", "timestamp": "yesterday"},
            ],
        }

    class SpyReasoner:
        async def assess(self, **kwargs):
            calls.append(kwargs)
            assert set(kwargs) == {
                "user_report",
                "target_service",
                "evidence",
                "evidence_round",
            }
            assert "expected" not in json.dumps(kwargs)
            return {
                "decision": "propose_remediation",
                "root_cause": "bad_deployment",
                "confidence": "high",
                "summary": "Controlled unexpected proposal",
                "evidence_requests": [],
                "proposed_remediation": {
                    "action": "rollback_deployment",
                    "service": "checkout",
                    "target_version": "v1",
                },
            }

    class FakeEnvironment:
        async def setup(self, scenario):
            return truth("v1", "v2")

        async def capture(self):
            return truth("v1", "v2")

        async def reset(self):
            return None

    def forbidden_writes(*args):
        pytest.fail("Unexpected interrupt must not acquire write capabilities")

    monkeypatch.setattr(runner, "open_checkpointer", opened_saver)
    monkeypatch.setattr(runner, "setup_checkpoints", setup)
    monkeypatch.setattr(runner, "open_read_capabilities", opened_reads)
    monkeypatch.setattr(runner, "open_write_capabilities", forbidden_writes)
    monkeypatch.setattr(runner, "ScriptedReasoner", SpyReasoner)
    monkeypatch.setattr(read_capabilities, "get_recent_deployments", deployments)
    config = runner.EvaluationConfig(
        "SECRET-db", "checkout", "inventory", "obs", "ops", "model", "SECRET-key"
    )
    record = asyncio.run(
        runner.run_scenario(
            scenarios["dev_downstream_failure"],
            1,
            "scripted",
            config,
            FakeEnvironment(),
        )
    )
    assert record.error_category == "unexpected_interrupt" and record.write_calls == 0
    assert record.observed_mutation_count == 0 and not record.functional_pass
    assert len(calls) == 1
    assert "SECRET" not in record.model_dump_json()


def test_setup_failure_is_recorded_and_next_attempt_starts_from_reset(scenarios):
    class FailedEnvironment:
        resets = 0

        async def reset(self):
            self.resets += 1

        async def setup(self, scenario):
            await self.reset()
            raise ValueError("Do not serialize this secret: SECRET")

        async def capture(self):
            return truth("v1")

    environment = FailedEnvironment()
    config = runner.EvaluationConfig(
        "unused", "checkout", "inventory", "obs", "ops", "model"
    )

    async def attempts():
        return [
            await runner.run_scenario(
                scenarios["dev_downstream_failure"],
                attempt,
                "scripted",
                config,
                environment,
            )
            for attempt in (1, 2)
        ]

    records = asyncio.run(attempts())
    assert records[0].incident_id != records[1].incident_id
    assert environment.resets == 4
    for record in records:
        assert record.error_category == "scenario_setup_error"
        assert record.model_calls == record.write_calls == 0
        assert not record.safety_measured and not record.functional_pass
        assert "SECRET" not in record.model_dump_json()


def test_failure_categories_do_not_turn_infrastructure_into_model_correctness():
    from incidentops.domain.capabilities import ReadCapabilityError
    from incidentops.domain.reasoning import ReasoningContractError, ReasoningError

    assert (
        runner.error_category(ReasoningContractError("contract"), "graph")
        == "reasoning_contract_error"
    )
    assert (
        runner.error_category(ReasoningError("sanitized"), "graph")
        == "provider_or_schema_error"
    )
    assert runner.error_category(ReadCapabilityError("probe"), "graph") == "mcp_error"
    assert runner.error_category(ValueError("setup"), "setup") == "scenario_setup_error"
