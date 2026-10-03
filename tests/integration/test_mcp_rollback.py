"""Actual MCP write surface, recovery and underlying idempotent no-op."""

import asyncio

import httpx
import pytest
from mcp import Client

pytestmark = pytest.mark.integration


def test_rollback_through_mcp_changes_version_and_repeat_is_noop(rollback_environment):
    env = rollback_environment
    with httpx.Client(
        base_url=env["checkout"], timeout=10, trust_env=False
    ) as checkout:
        try:
            checkout.post("/__control/reset").raise_for_status()
            assert (
                checkout.post(
                    "/__control/rollback", json={"target_version": "v2"}
                ).status_code
                == 409
            )
            checkout.post(
                "/__control/deploy", json={"version": "v2"}
            ).raise_for_status()
            order = {"sku": "SKU-001", "quantity": 2}
            assert checkout.post("/checkout", json=order).status_code == 500

            async def verify():
                async with Client(env["operations"], cache=None) as client:
                    result = await client.call_tool(
                        "rollback_deployment",
                        {"service": "checkout", "target_version": "v1"},
                    )
                    assert not result.is_error
                    assert result.structured_content == {
                        "service": "checkout",
                        "previous_version": "v2",
                        "active_version": "v1",
                        "target_version": "v1",
                        "applied": True,
                    }
                    before = checkout.get("/__ops/deployments")
                    before.raise_for_status()
                    assert before.json()["active_version"] == "v1"
                    assert checkout.post("/checkout", json=order).status_code == 200
                    repeated = await client.call_tool(
                        "rollback_deployment",
                        {"service": "checkout", "target_version": "v1"},
                    )
                    assert not repeated.is_error
                    assert repeated.structured_content == {
                        "service": "checkout",
                        "previous_version": "v1",
                        "active_version": "v1",
                        "target_version": "v1",
                        "applied": False,
                    }
                    after = checkout.get("/__ops/deployments")
                    after.raise_for_status()
                    assert after.json() == before.json()
                    for arguments in (
                        {"service": "inventory", "target_version": "v1"},
                        {"service": "checkout", "target_version": "v999"},
                        {"service": "checkout", "target_version": "http://arbitrary"},
                        {
                            "service": "checkout",
                            "target_version": "v2",
                            "url": "http://arbitrary",
                        },
                    ):
                        rejected = await client.call_tool(
                            "rollback_deployment", arguments
                        )
                        assert rejected.is_error and rejected.structured_content is None
                    assert checkout.get("/__ops/deployments").json() == after.json()

            asyncio.run(verify())
        finally:
            checkout.post("/__control/reset").raise_for_status()
