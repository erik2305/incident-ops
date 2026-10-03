"""Real ASGI routes over the unchanged graph with controlled run factories."""

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from incidentops.api.app import create_app
from incidentops.api.runtime import APIConfig, RuntimeFactories


@pytest.fixture
def api_case(read_capabilities, reasoner, monkeypatch):
    config = APIConfig(
        database_url="database-secret",
        observability_mcp_url="http://obs-secret/mcp",
        operations_mcp_url="http://ops-secret/mcp",
        openrouter_api_key="key-secret",
        openrouter_model="test-model",
    )
    created, closed, writes = [], [], []
    active = {"version": "v2"}
    saver = InMemorySaver()

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
            "summary": "Rollback the observed bad deployment.",
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
            writes.append({"service": service, "target_version": target_version})
            previous = active["version"]
            active["version"] = target_version
            return {
                "service": service,
                "previous_version": previous,
                "active_version": target_version,
                "target_version": target_version,
                "applied": previous != target_version,
            }

    def factory(name, dependency):
        @asynccontextmanager
        async def opened(*args, **kwargs):
            created.append(name)
            try:
                yield dependency
            finally:
                closed.append(name)

        return opened

    factories = RuntimeFactories(
        reads=factory("read", read_capabilities),
        writes=factory("write", Writes()),
        reasoner=factory("reasoner", reasoner),
    )
    app = create_app(
        config, factories=factories, checkpointer_factory=factory("saver", saver)
    )
    return app, created, closed, writes, reasoner, read_capabilities


def start(client):
    with client.stream(
        "POST",
        "/incidents",
        json={"user_report": "Checkout errors", "target_service": "checkout"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = list(response.iter_lines())
    assert lines[0] == "event: incident_started"
    assert "event: approval_required" in lines
    assert "event: completed" not in lines
    # Assert native SSE wire fields, without implementing an SSE parser/replay layer.
    payloads = [json.loads(line[6:]) for line in lines if line.startswith("data: ")]
    return payloads[0]["incident_id"], payloads[-1], lines


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_api_runtime_authority_and_terminal_results(api_case, decision):
    app, created, closed, writes, reasoner, reads = api_case
    with TestClient(app) as client:
        incident_id, approval, lines = start(client)
        for line in lines:
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                if "node" in payload:
                    assert set(payload) == {"incident_id", "node", "status"}
        UUID(incident_id)
        assert created == ["saver", "read", "reasoner"]
        assert closed == ["reasoner", "read"] and not writes
        state = client.get(f"/incidents/{incident_id}").json()
        assert state["status"] == "awaiting_approval"
        assert state["pending_action"]["action_id"] == approval["action_id"]
        assert approval["fingerprint"] == state["pending_action"]["fingerprint"]
        assert approval["action"] == state["proposal"]
        assert "execution_record" not in state and "verification_result" not in state
        assert client.get(f"/incidents/{incident_id}/result").status_code == 409
        before_reads, before_reasoning = len(reads.calls), len(reasoner.calls)
        with client.stream(
            "POST",
            f"/incidents/{incident_id}/approval",
            json={"decision": decision, "action_id": approval["action_id"]},
        ) as response:
            assert response.status_code == 200
            resumed = list(response.iter_lines())
        assert resumed[-3] == "event: completed"
        assert len(reasoner.calls) == before_reasoning
        result = client.get(f"/incidents/{incident_id}/result").json()
        if decision == "approve":
            assert created == ["saver", "read", "reasoner", "read", "write"]
            assert closed == ["reasoner", "read", "write", "read"]
            assert writes == [{"service": "checkout", "target_version": "v1"}]
            assert result["status"] == "resolved"
            assert result["verification_result"]["recovered"] is True
            assert len(reads.calls) == before_reads + 2
            for node in ("execute_action", "verify_recovery", "finalize_resolved"):
                assert any(
                    f'"node":"{node}"' in line.replace(" ", "") for line in resumed
                )
        else:
            assert created == ["saver", "read", "reasoner"] and not writes
            assert len(reads.calls) == before_reads
            assert (
                result["status"] == "escalated"
                and result["escalation_reason"] == "action_rejected"
            )
        assert (
            client.post(
                f"/incidents/{incident_id}/approval",
                json={"decision": decision, "action_id": approval["action_id"]},
            ).status_code
            == 409
        )
        assert not app.state.active_incidents
        assert all(
            secret not in "\n".join(lines + resumed)
            for secret in (
                "database-secret",
                "obs-secret",
                "ops-secret",
                "key-secret",
                "test-model",
            )
        )
    assert closed[-1] == "saver"


def test_api_preflight_unknown_and_wrong_action(api_case):
    app, created, _, writes, _, _ = api_case
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "healthy"}
        assert created == ["saver"]
        for path in ("/incidents/unknown", "/incidents/unknown/result"):
            assert client.get(path).status_code == 404
        assert (
            client.post(
                "/incidents/unknown/approval",
                json={"decision": "approve", "action_id": "unknown"},
            ).status_code
            == 404
        )
        incident_id, _, _ = start(client)
        before = list(created)
        response = client.post(
            f"/incidents/{incident_id}/approval",
            json={"decision": "approve", "action_id": "wrong"},
        )
        assert response.status_code == 409
        assert response.headers["content-type"].startswith("application/json")
        assert created == before and not writes and not app.state.active_incidents


@pytest.mark.parametrize(
    "extra", ["service", "target_version", "action", "fingerprint", "payload"]
)
def test_api_forbids_replacement_approval_arguments(api_case, extra):
    app, _, _, writes, _, _ = api_case
    with TestClient(app) as client:
        response = client.post(
            "/incidents/unknown/approval",
            json={"decision": "approve", "action_id": "unknown", extra: "injected"},
        )
        assert response.status_code == 422 and not writes


@pytest.mark.parametrize(
    "body",
    [
        {"user_report": " ", "target_service": "checkout"},
        {"user_report": "Errors", "target_service": "http://arbitrary"},
        {
            "user_report": "Errors",
            "target_service": "checkout",
            "incident_id": "chosen",
        },
    ],
)
def test_api_validates_start_before_stream(api_case, body):
    app, created, _, _, _, _ = api_case
    with TestClient(app) as client:
        assert client.post("/incidents", json=body).status_code == 422
        assert created == ["saver"] and not app.state.active_incidents


def test_api_runtime_failure_emits_safe_error_without_escalation(api_case, monkeypatch):
    app, _, closed, _, reasoner, _ = api_case

    async def fail(**kwargs):
        raise RuntimeError("database-secret obs-secret key-secret Traceback internal")

    monkeypatch.setattr(reasoner, "assess", fail)
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/incidents",
            json={"user_report": "Errors", "target_service": "checkout"},
        ) as response:
            lines = list(response.iter_lines())
        assert lines[0] == "event: incident_started" and lines[-3] == "event: error"
        payloads = [json.loads(line[6:]) for line in lines if line.startswith("data: ")]
        incident_id = payloads[0]["incident_id"]
        assert set(payloads[-1]) == {"incident_id", "code", "message"}
        assert all(
            secret not in "\n".join(lines)
            for secret in (
                "database-secret",
                "obs-secret",
                "key-secret",
                "Traceback",
                "RuntimeError",
            )
        )
        state = client.get(f"/incidents/{incident_id}").json()
        assert state["status"] == "investigating" and "escalation_reason" not in state
        assert closed == ["reasoner", "read"] and not app.state.active_incidents


def test_environment_config_requires_explicit_settings_without_import_clients(
    monkeypatch,
):
    for name in (
        "INCIDENTOPS_DATABASE_URL",
        "INCIDENTOPS_OBSERVABILITY_MCP_URL",
        "INCIDENTOPS_OPERATIONS_MCP_URL",
        "INCIDENTOPS_OPENROUTER_MODEL",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="Missing API configuration"):
        APIConfig.from_environment()


def test_start_can_stream_terminal_escalation_without_approval(api_case):
    app, created, _, writes, reasoner, _ = api_case
    reasoner.assessments = [
        {
            "decision": "escalate",
            "root_cause": "unknown",
            "confidence": "low",
            "summary": "The incident requires an operator.",
            "evidence_requests": [],
            "proposed_remediation": None,
        }
    ]
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/incidents",
            json={"user_report": "Errors", "target_service": "checkout"},
        ) as response:
            lines = list(response.iter_lines())
        assert lines[0] == "event: incident_started" and lines[-3] == "event: completed"
        assert "event: approval_required" not in lines
        payload = json.loads(lines[-2][6:])
        assert (
            payload["status"] == "escalated"
            and payload["escalation_reason"] == "model_escalation"
        )
        assert (
            client.get(f"/incidents/{payload['incident_id']}/result").status_code == 200
        )
        assert created == ["saver", "read", "reasoner"] and not writes


def test_approval_verification_error_stream_retains_execution_without_false_terminal(
    api_case,
):
    app, _, closed, writes, reasoner, reads = api_case
    with TestClient(app) as client:
        incident_id, approval, _ = start(client)
        before_reasoning = len(reasoner.calls)
        reads.fail_capability = "probe_checkout"
        with client.stream(
            "POST",
            f"/incidents/{incident_id}/approval",
            json={"decision": "approve", "action_id": approval["action_id"]},
        ) as response:
            lines = list(response.iter_lines())
        assert response.status_code == 200 and lines[-3] == "event: error"
        assert "event: completed" not in lines
        state = client.get(f"/incidents/{incident_id}").json()
        assert state["status"] == "action_executed" and "execution_record" in state
        assert "verification_result" not in state and "escalation_reason" not in state
        assert client.get(f"/incidents/{incident_id}/result").status_code == 409
        assert len(writes) == 1 and len(reasoner.calls) == before_reasoning
        assert closed == ["reasoner", "read", "write", "read"]


def test_live_stream_disconnect_cancels_run_closes_dependencies_and_releases_guard(
    api_case, api_server, monkeypatch
):
    app, _, closed, writes, reasoner, _ = api_case

    async def exercise():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def blocked(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(reasoner, "assess", blocked)
        async with (
            api_server(app) as url,
            httpx.AsyncClient(base_url=url, trust_env=False, timeout=5) as client,
        ):
            async with client.stream(
                "POST",
                "/incidents",
                json={"user_report": "Errors", "target_service": "checkout"},
            ) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                assert await anext(lines) == "event: incident_started"
                first = json.loads((await anext(lines))[6:])
                incident_id = first["incident_id"]
                # The first event arrives even though reasoning cannot complete.
                await asyncio.wait_for(entered.wait(), timeout=5)
                overlap = await client.post(
                    f"/incidents/{incident_id}/approval",
                    json={"decision": "approve", "action_id": "unknown"},
                )
                assert overlap.status_code == 409
                state = (await client.get(f"/incidents/{incident_id}")).json()
                assert (
                    state["status"] == "investigating" and state["evidence_round"] == 1
                )
            await asyncio.wait_for(cancelled.wait(), timeout=5)
            async with asyncio.timeout(5):
                while app.state.active_incidents:
                    await asyncio.sleep(0.01)
            assert closed == ["reasoner", "read"] and not writes
            state = (await client.get(f"/incidents/{incident_id}")).json()
            assert (
                "assessment_history" not in state and state["status"] == "investigating"
            )

    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(exercise())
