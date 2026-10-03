"""Real fixed behavior probes across v2 failure and v1 rollback recovery."""

import asyncio

import httpx
import pytest
from mcp import Client

pytestmark = pytest.mark.integration


@pytest.fixture
def probe_environment(rollback_environment):
    env = rollback_environment
    with (
        httpx.Client(base_url=env["checkout"], trust_env=False, timeout=10) as checkout,
        httpx.Client(
            base_url=env["inventory"], trust_env=False, timeout=10
        ) as inventory,
    ):
        try:
            for client in (checkout, inventory):
                client.post("/__control/reset").raise_for_status()
            yield env, checkout, inventory
        finally:
            for client in (checkout, inventory):
                client.post("/__control/reset").raise_for_status()


def business_evidence(client):
    return [client.get(f"/__ops/{endpoint}").json() for endpoint in ("metrics", "logs")]


def test_real_probe_observes_v2_failure_then_v1_recovery_without_traffic(
    probe_environment,
):
    env, checkout, inventory = probe_environment
    checkout.post("/__control/deploy", json={"version": "v2"}).raise_for_status()
    order = {"sku": "SKU-001", "quantity": 1}
    assert checkout.post("/checkout", json=order).status_code == 500

    async def verify():
        async with (
            Client(env["observability"], cache=None) as obs,
            Client(env["operations"], cache=None) as ops,
        ):
            for ok, status in ((False, 500), (True, 200)):
                before = [business_evidence(client) for client in (checkout, inventory)]
                deployments = checkout.get("/__ops/deployments").json()
                for _ in range(2):
                    result = await obs.call_tool("probe_checkout", {})
                    assert not result.is_error
                    assert result.structured_content == {
                        "service": "checkout",
                        "ok": ok,
                        "observed_status": status,
                    }
                assert [
                    business_evidence(client) for client in (checkout, inventory)
                ] == before
                assert checkout.get("/__ops/deployments").json() == deployments
                if not ok:
                    result = await ops.call_tool(
                        "rollback_deployment",
                        {"service": "checkout", "target_version": "v1"},
                    )
                    assert (
                        not result.is_error
                        and result.structured_content["applied"] is True
                    )

    asyncio.run(verify())
    assert checkout.get("/__ops/deployments").json()["active_version"] == "v1"
    assert checkout.post("/checkout", json=order).status_code == 200


def test_probe_rejects_all_caller_controlled_arguments(probe_environment):
    env, checkout, inventory = probe_environment
    before = [business_evidence(client) for client in (checkout, inventory)]

    async def verify():
        async with Client(env["observability"], cache=None) as client:
            for arguments in (
                {"service": "inventory"},
                {"sku": "SKU-002"},
                {"quantity": 2},
                {"url": "http://127.0.0.1:9"},
                {"path": "/checkout"},
                {"method": "POST"},
                {"payload": {"sku": "SKU-001"}},
            ):
                result = await client.call_tool("probe_checkout", arguments)
                assert result.is_error and result.structured_content is None

    asyncio.run(verify())
    assert [business_evidence(client) for client in (checkout, inventory)] == before
