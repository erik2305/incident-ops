"""Causal evidence from real HTTP requests to separate Compose containers."""

import os
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

pytestmark = pytest.mark.integration
ORDER = {"sku": "SKU-001", "quantity": 2}


def get_json(client, path):
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def reset_services(checkout, inventory):
    for client in (checkout, inventory):
        client.post("/__control/reset").raise_for_status()


@pytest.fixture
def services():
    checkout_url = os.environ.get("INCIDENTOPS_TEST_CHECKOUT_URL")
    inventory_url = os.environ.get("INCIDENTOPS_TEST_INVENTORY_URL")
    if not checkout_url and not inventory_url:
        pytest.skip("Set synthetic checkout and inventory test URLs")
    if not checkout_url or not inventory_url:
        pytest.fail("Configure both synthetic service test URLs")
    with (
        httpx.Client(base_url=checkout_url, timeout=15, trust_env=False) as checkout,
        httpx.Client(base_url=inventory_url, timeout=15, trust_env=False) as inventory,
    ):
        reset_services(checkout, inventory)
        try:
            yield checkout, inventory
        finally:
            reset_services(checkout, inventory)


def assert_baseline(checkout, inventory):
    for client, name in ((checkout, "checkout-api"), (inventory, "inventory-api")):
        assert get_json(client, "/__ops/health") == {
            "service": name,
            "status": "healthy",
        }
        assert get_json(client, "/__ops/metrics") == {
            "requests_total": 0,
            "requests_5xx": 0,
        }
        assert get_json(client, "/__ops/logs") == []
    deployments = get_json(checkout, "/__ops/deployments")
    assert deployments["active_version"] == "v1"
    assert len(deployments["deployment_history"]) == 1
    assert deployments["deployment_history"][0]["version"] == "v1"
    assert deployments["deployment_history"][0]["timestamp"]


def structured_keys(value):
    if isinstance(value, dict):
        return set(value).union(*(structured_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(structured_keys(item) for item in value))
    return set()


def test_inventory_fault_observed_as_checkout_downstream_failure_and_reset(services):
    checkout, inventory = services
    assert checkout.post("/checkout", json=ORDER).status_code == 200
    checkout.post("/__control/deploy", json={"version": "v2"}).raise_for_status()
    assert checkout.post("/checkout", json=ORDER).status_code == 500
    inventory.post("/__control/fault", json={"mode": "unavailable"}).raise_for_status()
    assert inventory.get("/__ops/health").json()["status"] == "healthy"
    assert inventory.get("/inventory/SKU-001").status_code == 503
    assert checkout.post("/checkout", json=ORDER).status_code == 502
    before = [
        get_json(client, f"/__ops/{endpoint}")
        for client in (checkout, inventory)
        for endpoint in ("metrics", "logs")
    ]
    probe = checkout.get("/__ops/probe")
    assert probe.status_code == 200
    assert probe.json() == {"service": "checkout", "ok": False, "observed_status": 502}
    assert [
        get_json(client, f"/__ops/{endpoint}")
        for client in (checkout, inventory)
        for endpoint in ("metrics", "logs")
    ] == before
    facts = [
        get_json(client, f"/__ops/{endpoint}")
        for client in (checkout, inventory)
        for endpoint in ("health", "metrics", "logs")
    ]
    assert not structured_keys(facts) & {
        "fault_mode",
        "root_cause",
        "expected_action",
        "inventory_is_intentionally_broken",
    }
    reset_services(checkout, inventory)
    assert_baseline(checkout, inventory)
    assert inventory.get("/__ops/probe-stock").status_code == 200
    assert checkout.post("/checkout", json=ORDER).status_code == 200


def test_bad_deployment_failure_evidence_and_recovery(services):
    checkout, inventory = services
    assert_baseline(checkout, inventory)
    stock = get_json(inventory, "/inventory/SKU-001")
    assert stock["available"] == 25
    baseline = checkout.post("/checkout", json=ORDER)
    assert baseline.status_code == 200
    assert baseline.json()["total_cents"] == 3998
    assert get_json(checkout, "/__ops/metrics") == {
        "requests_total": 1,
        "requests_5xx": 0,
    }
    inventory_before = get_json(inventory, "/__ops/metrics")["requests_total"]

    checkout.post("/__control/deploy", json={"version": "v2"}).raise_for_status()
    for _ in range(2):
        assert checkout.post("/checkout", json=ORDER).status_code == 500

    assert get_json(checkout, "/__ops/health")["status"] == "healthy"
    assert get_json(inventory, "/__ops/health")["status"] == "healthy"
    assert get_json(checkout, "/__ops/metrics") == {
        "requests_total": 3,
        "requests_5xx": 2,
    }
    # Even defective checkout requests actually reached inventory over HTTP.
    assert get_json(inventory, "/__ops/metrics") == {
        "requests_total": inventory_before + 2,
        "requests_5xx": 0,
    }
    errors = [
        log
        for log in get_json(checkout, "/__ops/logs")
        if log["event"] == "checkout_error"
    ]
    assert len(errors) == 2
    for log in errors:
        assert log["level"] == "ERROR"
        assert log["version"] == "v2"
        assert log["error_type"] == "KeyError"
        assert log["status_code"] == 500
        assert {"timestamp", "service", "message"} <= log.keys()
    deployments = get_json(checkout, "/__ops/deployments")
    assert deployments["active_version"] == "v2"
    assert [entry["version"] for entry in deployments["deployment_history"]] == [
        "v1",
        "v2",
    ]
    assert get_json(inventory, "/inventory/SKU-001")["available"] == 25

    checkout.post("/__control/deploy", json={"version": "v1"}).raise_for_status()
    recovered = checkout.post("/checkout", json=ORDER)
    assert recovered.status_code == 200
    assert recovered.json()["total_cents"] == 3998
    assert get_json(checkout, "/__ops/metrics") == {
        "requests_total": 4,
        "requests_5xx": 2,
    }
    logs = get_json(checkout, "/__ops/logs")
    assert all(error in logs for error in errors)
    assert logs[-1]["event"] == "checkout_completed"
    assert logs[-1]["version"] == "v1"
    assert [
        entry["version"]
        for entry in get_json(checkout, "/__ops/deployments")["deployment_history"]
    ] == ["v1", "v2", "v1"]

    forbidden = {"root_cause", "expected_action", "correct_diagnosis", "bad_deployment"}
    for client in (checkout, inventory):
        for path in ("/__ops/health", "/__ops/metrics", "/__ops/logs"):
            assert forbidden.isdisjoint(structured_keys(get_json(client, path)))
    assert forbidden.isdisjoint(structured_keys(deployments))
    reset_services(checkout, inventory)
    assert_baseline(checkout, inventory)


def test_reset_restores_runtime_baseline(services):
    checkout, inventory = services
    checkout.post("/__control/deploy", json={"version": "v2"}).raise_for_status()
    assert checkout.post("/checkout", json=ORDER).status_code == 500
    assert get_json(checkout, "/__ops/logs")
    assert get_json(inventory, "/__ops/logs")

    reset_services(checkout, inventory)
    assert_baseline(checkout, inventory)
    assert checkout.post("/checkout", json=ORDER).status_code == 200
    # Unsupported deployment input cannot change the active implementation.
    assert checkout.post("/__control/deploy", json={"version": "v3"}).status_code == 422
    assert get_json(checkout, "/__ops/deployments")["active_version"] == "v1"


def test_concurrent_business_counters_and_bounded_logs(services):
    checkout, inventory = services

    def submit(_):
        return checkout.post("/checkout", json=ORDER).status_code

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert list(executor.map(submit, range(60))) == [200] * 60
    for client in (checkout, inventory):
        assert get_json(client, "/__ops/metrics") == {
            "requests_total": 60,
            "requests_5xx": 0,
        }
    assert len(get_json(checkout, "/__ops/logs")) == 100
    assert len(get_json(inventory, "/__ops/logs")) == 60
