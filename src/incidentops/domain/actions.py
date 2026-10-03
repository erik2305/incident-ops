"""Exact-action identity and checkpoint-safe approval/execution contracts."""

import hashlib
import json
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, ValidationError

from incidentops.domain.models import JSONData
from incidentops.domain.reasoning import RemediationProposal, RemediationProposalSchema


class ActionIntegrityError(RuntimeError):
    """Persisted action or approval does not match the exact execution authority."""


class PendingAction(RemediationProposal):
    action_id: str
    fingerprint: str


class ApprovalRecord(TypedDict):
    decision: Literal["approve", "reject"]
    action_id: str
    fingerprint: str


class ExecutionRecord(TypedDict):
    action_id: str
    fingerprint: str
    result: JSONData


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["approve", "reject"]
    action_id: str


class PendingActionSchema(RemediationProposalSchema):
    action_id: str
    fingerprint: str


def action_payload(action: PendingAction | RemediationProposal) -> RemediationProposal:
    return {
        "action": action["action"],
        "service": action["service"],
        "target_version": action["target_version"],
    }


def action_fingerprint(payload: RemediationProposal) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def materialize_action(
    incident_id: str, proposal: RemediationProposal
) -> PendingAction:
    payload = action_payload(proposal)
    fingerprint = action_fingerprint(payload)
    identity = json.dumps(
        ["incidentops-action-v1", incident_id, fingerprint],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return {
        **payload,
        "fingerprint": fingerprint,
        "action_id": "act_" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
    }


def validate_pending_action(incident_id: str, value: object) -> PendingAction:
    try:
        pending = PendingActionSchema.model_validate(value).model_dump(mode="json")
    except ValidationError:
        raise ActionIntegrityError(
            "A valid persisted pending_action is required"
        ) from None
    if not pending["target_version"] or pending != materialize_action(
        incident_id, action_payload(pending)
    ):
        raise ActionIntegrityError("Pending action identity or fingerprint mismatch")
    return pending


def validate_approval(
    value: object, pending: PendingAction, decision: str
) -> ApprovalRecord:
    if not isinstance(value, dict) or set(value) != {
        "decision",
        "action_id",
        "fingerprint",
    }:
        raise ActionIntegrityError("A matching approval_record is required")
    if (
        value["decision"] != decision
        or value["action_id"] != pending["action_id"]
        or value["fingerprint"] != pending["fingerprint"]
    ):
        raise ActionIntegrityError("Approval does not authorize this exact action")
    return dict(value)


def valid_rollback_result(data: object, target_version: str) -> bool:
    """Small exact built-in result contract shared by server, adapter and executor."""
    if type(data) is not dict or set(data) != {
        "service",
        "previous_version",
        "active_version",
        "target_version",
        "applied",
    }:
        return False
    return (
        data["service"] == "checkout"
        and type(data["service"]) is str
        and type(data["previous_version"]) is str
        and data["previous_version"] in ("v1", "v2")
        and type(data["active_version"]) is str
        and data["active_version"] == target_version
        and type(data["target_version"]) is str
        and data["target_version"] == target_version
        and type(data["applied"]) is bool
        and data["applied"] == (data["previous_version"] != target_version)
    )
