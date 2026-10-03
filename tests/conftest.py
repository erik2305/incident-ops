"""Fake reads keep graph and checkpoint regression tests infrastructure-specific."""

from pathlib import Path

import pytest
from dotenv import load_dotenv

from incidentops.domain.capabilities import ReadCapabilityError
from incidentops.graph.runtime import IncidentRuntimeContext

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def escalation_assessment():
    return {
        "decision": "escalate",
        "root_cause": "unknown",
        "confidence": "low",
        "summary": "Fake reasoner terminates the infrastructure regression.",
        "evidence_requests": [],
        "proposed_remediation": None,
    }


class FakeReasoner:
    def __init__(self, assessments=None):
        self.assessments = assessments or [escalation_assessment()]
        self.calls = []

    async def assess(self, **kwargs):
        self.calls.append(kwargs)
        return self.assessments[min(len(self.calls) - 1, len(self.assessments) - 1)]


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
def reasoner():
    return FakeReasoner()


@pytest.fixture
def incident_runtime(read_capabilities, reasoner):
    return IncidentRuntimeContext(
        read_capabilities=read_capabilities, reasoner=reasoner
    )
