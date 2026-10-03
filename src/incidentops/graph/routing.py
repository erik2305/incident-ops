"""Deterministic routes for the fixed, one-extra-round reasoning cycle."""

from typing import Literal

from incidentops.graph.state import IncidentState


def route_assessment(
    state: IncidentState,
) -> Literal["end", "collect", "limit", "prepare"]:
    if state["assessment_history"][-1]["decision"] == "propose_remediation":
        return "prepare"
    if state["assessment_history"][-1]["decision"] != "need_more_evidence":
        return "end"
    return "collect" if state["evidence_round"] < 2 else "limit"


def route_approval(state: IncidentState) -> Literal["approve", "reject"]:
    return state["approval_record"]["decision"]


def route_verification(state: IncidentState) -> Literal["resolved", "failed"]:
    recovered = state["verification_result"]["recovered"]
    if type(recovered) is not bool:
        raise ValueError("Verification recovered must be a boolean")
    return "resolved" if recovered else "failed"
