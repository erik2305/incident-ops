"""Fake reads keep graph and checkpoint regression tests infrastructure-specific."""

import pytest

from incidentops.domain.capabilities import ReadCapabilityError
from incidentops.graph.runtime import IncidentRuntimeContext


class FakeReadCapabilities:
    def __init__(self):
        self.calls = []
        self.fail_capability = None
        self.log_message = "Checkout completed"

    def called(self, capability, service, limit=None):
        self.calls.append((capability, service, limit))
        if capability == self.fail_capability:
            raise ReadCapabilityError(f"{capability}({service}): fake read failed")

    async def get_service_health(self, service):
        self.called("get_service_health", service)
        return {"service": f"{service}-api", "status": "healthy"}

    async def get_metrics(self, service):
        self.called("get_metrics", service)
        return {"requests_total": 3, "requests_5xx": 1}

    async def query_logs(self, service, *, limit=20):
        self.called("query_logs", service, limit)
        return {
            "logs": [
                {
                    "timestamp": "2026-10-03T00:00:00+00:00",
                    "level": "INFO",
                    "service": f"{service}-api",
                    "message": self.log_message,
                }
            ]
        }

    async def get_recent_deployments(self, service, *, limit=20):
        self.called("get_recent_deployments", service, limit)
        return {
            "active_version": "v1",
            "deployment_history": [
                {"version": "v1", "timestamp": "2026-10-03T00:00:00+00:00"}
            ],
        }


@pytest.fixture
def read_capabilities():
    return FakeReadCapabilities()


@pytest.fixture
def incident_runtime(read_capabilities):
    return IncidentRuntimeContext(read_capabilities=read_capabilities)
