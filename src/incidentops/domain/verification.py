"""Checkpoint-safe recovery observations and the fixed probe contract."""

from typing import TypedDict


class VerificationResult(TypedDict):
    action_id: str
    target_version: str
    deployment_matches_target: bool
    probe_ok: bool
    probe_status: int
    recovered: bool


def valid_probe_result(data: object) -> bool:
    return (
        type(data) is dict
        and set(data) == {"service", "ok", "observed_status"}
        and data["service"] == "checkout"
        and type(data["ok"]) is bool
        and type(data["observed_status"]) is int
        and 100 <= data["observed_status"] <= 599
        and data["ok"] == (data["observed_status"] == 200)
    )
