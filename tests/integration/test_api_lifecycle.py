"""Host HTTP/SSE → graph/PostgreSQL/MCP → durable approval and recovery."""

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest

from incidentops.api.app import create_app
from incidentops.api.runtime import APIConfig, RuntimeFactories
from incidentops.graph import build_graph
from incidentops.mcp.client import open_read_capabilities, open_write_capabilities
from incidentops.persistence import open_checkpointer, setup_checkpoints

pytestmark = pytest.mark.integration


def run_async(coroutine):
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coroutine)


async def consume(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # Consume live native wire fields, rather than a buffered final body.
    lines = [line async for line in response.aiter_lines()]
    names = [line[7:] for line in lines if line.startswith("event: ")]
    payloads = [json.loads(line[6:]) for line in lines if line.startswith("data: ")]
    assert len(names) == len(payloads)
    assert not any(line.startswith(("id:", "retry:")) for line in lines)
    return names, payloads


def create_test_app(env, created, closed):
    def track(name, factory):
        @asynccontextmanager
        async def opened(*args, **kwargs):
            created.append(name)
            async with factory(*args, **kwargs) as dependency:
                try:
                    yield dependency
                finally:
                    closed.append(name)

        return opened

    class Reasoner:
        async def assess(self, **kwargs):
            assert kwargs["evidence_round"] == 1 and len(kwargs["evidence"]) == 4
            return {
                "decision": "propose_remediation",
                "root_cause": "bad_deployment",
                "confidence": "high",
                "summary": "Checkout v2 has a calculation defect.",
                "evidence_requests": [],
                "proposed_remediation": {
                    "action": "rollback_deployment",
                    "service": "checkout",
                    "target_version": "v1",
                },
            }

    @asynccontextmanager
    async def reasoner(**kwargs):
        yield Reasoner()

    config = APIConfig(
        database_url=env["database"],
        observability_mcp_url=env["observability"],
        operations_mcp_url=env["operations"],
        openrouter_api_key="fake-api-test-key",
        openrouter_model="fake-api-test-model",
    )
    return create_app(
        config,
        factories=RuntimeFactories(
            reads=track("read", open_read_capabilities),
            writes=track("write", open_write_capabilities),
            reasoner=track("reasoner", reasoner),
        ),
    )


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_api_durable_start_inspect_later_resume_and_result(
    rollback_environment, api_server, decision
):
    env = rollback_environment
    created, closed, ids = [], [], []

    async def exercise():
        await setup_checkpoints(env["database"])
        async with (
            httpx.AsyncClient(
                base_url=env["checkout"], timeout=10, trust_env=False
            ) as checkout,
            httpx.AsyncClient(
                base_url=env["inventory"], timeout=10, trust_env=False
            ) as inventory,
        ):
            try:
                for raw in (checkout, inventory):
                    (await raw.post("/__control/reset")).raise_for_status()
                (
                    await checkout.post("/__control/deploy", json={"version": "v2"})
                ).raise_for_status()
                order = {"sku": "SKU-001", "quantity": 1}
                assert (await checkout.post("/checkout", json=order)).status_code == 500
                # App A's saver/server close completely at the durable interrupt.
                async with (
                    api_server(create_test_app(env, created, closed)) as url,
                    httpx.AsyncClient(
                        base_url=url, timeout=15, trust_env=False
                    ) as client,
                ):
                    unknown = "unknown-" + uuid4().hex
                    for path in (
                        f"/incidents/{unknown}",
                        f"/incidents/{unknown}/result",
                    ):
                        assert (await client.get(path)).status_code == 404
                    assert (
                        await client.post(
                            f"/incidents/{unknown}/approval",
                            json={"decision": "approve", "action_id": "missing"},
                        )
                    ).status_code == 404
                    async with client.stream(
                        "POST",
                        "/incidents",
                        json={
                            "user_report": "Checkout HTTP 500 after deployment.",
                            "target_service": "checkout",
                        },
                    ) as response:
                        names, payloads = await consume(response)
                    assert (
                        names[0] == "incident_started"
                        and names[-1] == "approval_required"
                    )
                    assert set(names) == {
                        "incident_started",
                        "progress",
                        "approval_required",
                    }
                    incident_id = payloads[0]["incident_id"]
                    ids.append(incident_id)
                    approval = payloads[-1]
                    state = (await client.get(f"/incidents/{incident_id}")).json()
                    assert state["status"] == "awaiting_approval"
                    pending = state["pending_action"]
                    assert approval == {
                        "kind": "approval_required",
                        "incident_id": incident_id,
                        "action_id": pending["action_id"],
                        "fingerprint": pending["fingerprint"],
                        "action": state["proposal"],
                    }
                    assert (
                        await client.get(f"/incidents/{incident_id}/result")
                    ).status_code == 409
                    mismatch = await client.post(
                        f"/incidents/{incident_id}/approval",
                        json={"decision": "approve", "action_id": "wrong"},
                    )
                    assert mismatch.status_code == 409
                    assert mismatch.headers["content-type"].startswith(
                        "application/json"
                    )
                    assert created == ["read", "reasoner"] and closed == [
                        "reasoner",
                        "read",
                    ]
                assert (await checkout.get("/__ops/deployments")).json()[
                    "active_version"
                ] == "v2"
                assert (await checkout.post("/checkout", json=order)).status_code == 500
                # App B is entirely fresh and resumes the same PostgreSQL thread.
                async with (
                    api_server(create_test_app(env, created, closed)) as url,
                    httpx.AsyncClient(
                        base_url=url, timeout=15, trust_env=False
                    ) as client,
                ):
                    assert (
                        await client.get(f"/incidents/{incident_id}")
                    ).json() == state
                    async with client.stream(
                        "POST",
                        f"/incidents/{incident_id}/approval",
                        json={
                            "decision": decision,
                            "action_id": approval["action_id"],
                        },
                    ) as response:
                        names, payloads = await consume(response)
                    assert names[-1] == "completed" and set(names) == {
                        "progress",
                        "completed",
                    }
                    terminal = "resolved" if decision == "approve" else "escalated"
                    assert payloads[-1]["status"] == terminal
                    recovered = (await client.get(f"/incidents/{incident_id}")).json()
                    result = (
                        await client.get(f"/incidents/{incident_id}/result")
                    ).json()
                    assert result["status"] == recovered["status"] == terminal
                    assert result["root_cause"] == "bad_deployment"
                    if decision == "approve":
                        nodes = {p["node"] for p in payloads[:-1]}
                        assert {
                            "execute_action",
                            "verify_recovery",
                            "finalize_resolved",
                        } <= nodes
                        assert recovered["verification_result"]["recovered"] is True
                        assert (
                            recovered["approval_record"]["action_id"]
                            == pending["action_id"]
                        )
                        assert (
                            recovered["execution_record"]["action_id"]
                            == pending["action_id"]
                        )
                        assert result["approved_action"] == pending
                        assert created == ["read", "reasoner", "read", "write"]
                        assert closed == ["reasoner", "read", "write", "read"]
                        assert (await checkout.get("/__ops/deployments")).json()[
                            "active_version"
                        ] == "v1"
                        assert (
                            await checkout.post("/checkout", json=order)
                        ).status_code == 200
                    else:
                        assert result["escalation_reason"] == "action_rejected"
                        assert (
                            "execution_record" not in recovered
                            and "verification_result" not in recovered
                        )
                        assert created == ["read", "reasoner"]
                        assert (await checkout.get("/__ops/deployments")).json()[
                            "active_version"
                        ] == "v2"
                    assert (
                        await client.post(
                            f"/incidents/{incident_id}/approval",
                            json={
                                "decision": decision,
                                "action_id": pending["action_id"],
                            },
                        )
                    ).status_code == 409
                async with open_checkpointer(env["database"]) as saver:
                    snapshot = await build_graph(checkpointer=saver).aget_state(
                        {"configurable": {"thread_id": incident_id}}
                    )
                    assert snapshot.values["status"] == terminal and snapshot.next == ()
                assert not any("fake-api-test-key" in json.dumps(p) for p in payloads)
            finally:
                for raw in (checkout, inventory):
                    (await raw.post("/__control/reset")).raise_for_status()
                async with open_checkpointer(env["database"]) as saver:
                    for incident_id in ids:
                        await saver.adelete_thread(incident_id)

    run_async(exercise())
