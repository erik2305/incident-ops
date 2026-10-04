"""Real synthetic handlers distinguish business observations from probe failures."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from services.checkout.app import app as checkout_app
from services.inventory.app import app as inventory_app


def business_state(client):
    return [client.get(f"/__ops/{endpoint}").json() for endpoint in ("metrics", "logs")]


def test_inventory_fault_normal_unavailable_and_reset_are_observational():
    with TestClient(inventory_app) as client:
        assert client.get("/inventory/SKU-001").status_code == 200
        assert (
            client.post("/__control/fault", json={"mode": "unavailable"}).status_code
            == 200
        )
        assert client.get("/__ops/health").json() == {
            "service": "inventory-api",
            "status": "healthy",
        }
        assert client.get("/inventory/SKU-001").status_code == 503
        before = business_state(client)
        assert client.get("/__ops/probe-stock").status_code == 503
        assert business_state(client) == before
        assert before[0] == {"requests_total": 2, "requests_5xx": 1}
        assert (
            client.post("/__control/fault", json={"mode": "normal"}).status_code == 200
        )
        assert client.get("/__ops/probe-stock").status_code == 200
        client.post("/__control/fault", json={"mode": "unavailable"})
        client.post("/__control/reset").raise_for_status()
        assert business_state(client) == [{"requests_total": 0, "requests_5xx": 0}, []]
        assert client.get("/__ops/probe-stock").status_code == 200


@pytest.mark.parametrize(
    "body",
    [{"mode": "unknown"}, {"mode": True}, {"mode": "unavailable", "url": "arbitrary"}],
)
def test_inventory_fault_control_rejects_nonclosed_inputs(body):
    with TestClient(inventory_app) as client:
        assert client.post("/__control/fault", json=body).status_code == 422
        assert client.get("/__ops/probe-stock").status_code == 200


@pytest.mark.parametrize(
    ("version", "unavailable", "status"),
    [
        ("v1", False, 200),
        ("v2", False, 500),
        ("v1", True, 502),
        ("v2", True, 502),
    ],
)
def test_checkout_shared_calculation_observes_the_actual_downstream_response(
    version, unavailable, status
):
    calls = []

    def inventory(request):
        calls.append(request.url.path)
        return httpx.Response(
            503 if unavailable else 200,
            json={"sku": "SKU-001", "available": 25, "unit_price_cents": 1999},
        )

    with TestClient(checkout_app) as client:
        downstream = httpx.AsyncClient(
            base_url="http://inventory", transport=httpx.MockTransport(inventory)
        )
        checkout_app.state.inventory = downstream
        try:
            client.post("/__control/deploy", json={"version": version})
            assert (
                client.post(
                    "/checkout", json={"sku": "SKU-001", "quantity": 1}
                ).status_code
                == status
            )
            before = business_state(client)
            history = client.get("/__ops/deployments").json()
            probe = client.get("/__ops/probe")
            assert probe.status_code == 200
            assert probe.json() == {
                "service": "checkout",
                "ok": status == 200,
                "observed_status": status,
            }
            assert calls == ["/inventory/SKU-001", "/__ops/probe-stock"]
            assert business_state(client) == before
            assert client.get("/__ops/deployments").json() == history
        finally:
            asyncio.run(downstream.aclose())
