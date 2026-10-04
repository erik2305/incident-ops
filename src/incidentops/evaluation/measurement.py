"""Evaluator-only protocol wrappers; no production state or response changes."""

from collections import Counter


class Counts:
    def __init__(self):
        self.reads = Counter()
        self.writes = 0
        self.models = 0


class CountReads:
    def __init__(self, inner, counts):
        self.inner, self.counts = inner, counts

    async def probe_checkout(self):
        self.counts.reads["probe_checkout"] += 1
        return await self.inner.probe_checkout()

    async def get_service_health(self, service):
        self.counts.reads["get_service_health"] += 1
        return await self.inner.get_service_health(service)

    async def get_metrics(self, service):
        self.counts.reads["get_metrics"] += 1
        return await self.inner.get_metrics(service)

    async def query_logs(self, service, *, limit=20):
        self.counts.reads["query_logs"] += 1
        return await self.inner.query_logs(service, limit=limit)

    async def get_recent_deployments(self, service, *, limit=20):
        self.counts.reads["get_recent_deployments"] += 1
        return await self.inner.get_recent_deployments(service, limit=limit)


class CountWrites:
    def __init__(self, inner, counts):
        self.inner, self.counts = inner, counts
        self.actions = []

    async def rollback_deployment(self, *, service, target_version):
        self.counts.writes += 1
        self.actions.append(
            {
                "action": "rollback_deployment",
                "service": service,
                "target_version": target_version,
            }
        )
        return await self.inner.rollback_deployment(
            service=service, target_version=target_version
        )


class CountReasoner:
    def __init__(self, inner, counts):
        self.inner, self.counts = inner, counts

    async def assess(self, *, user_report, target_service, evidence, evidence_round):
        self.counts.models += 1
        return await self.inner.assess(
            user_report=user_report,
            target_service=target_service,
            evidence=evidence,
            evidence_round=evidence_round,
        )


class ScriptedReasoner:
    """Small harness calibration: infer only the real evidence's fixed outcomes."""

    async def assess(self, *, user_report, target_service, evidence, evidence_round):
        logs = next(
            item["data"]
            for item in evidence
            if item["capability"] == "query_logs" and item["service"] == "checkout"
        )
        logs = logs["logs"]
        # Read operational events, never evaluator labels or expectations.
        downstream = any(log.get("status_code") == 502 for log in logs)
        defect = any(log.get("event") == "checkout_error" for log in logs)
        root = (
            "downstream_failure"
            if downstream
            else "bad_deployment"
            if defect
            else "unknown"
        )
        return {
            "decision": "propose_remediation"
            if root == "bad_deployment"
            else "escalate",
            "root_cause": root,
            "confidence": "high" if root != "unknown" else "low",
            "summary": "Scripted harness conclusion from operational events.",
            "evidence_requests": [],
            "proposed_remediation": {
                "action": "rollback_deployment",
                "service": "checkout",
                "target_version": "v1",
            }
            if root == "bad_deployment"
            else None,
        }
