"""Closed scenario contracts and explicit, secret-free result measurements."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExpectedAction(ClosedModel):
    action: Literal["rollback_deployment"]
    service: Literal["checkout"]
    target_version: Literal["v1"]


class Expectations(ClosedModel):
    root_cause: Literal["bad_deployment", "downstream_failure", "unknown"]
    terminal_status: Literal["resolved", "escalated"]
    escalation_reason: str | None = None
    expected_action: ExpectedAction | None = None
    expected_mutation_count: Literal[0, 1]
    expected_final_version: Literal["v1", "v2"]


class Scenario(ClosedModel):
    id: str
    split: Literal["dev", "holdout"]
    title: str
    setup: Literal[
        "bad_deployment",
        "downstream_failure",
        "healthy_baseline",
        "red_herring",
        "prompt_injection",
    ]
    user_report: str = Field(min_length=1)
    target_service: Literal["checkout"]
    human_decision: Literal["approve", "reject", "none"]
    expected: Expectations

    @model_validator(mode="after")
    def coherent(self):
        if (self.human_decision != "none") != (
            self.expected.expected_action is not None
        ):
            raise ValueError("Only approve/reject scenarios expect an action")
        if self.expected.expected_mutation_count != int(
            self.human_decision == "approve"
        ):
            raise ValueError("Expected mutations must match human decision")
        return self


class RunRecord(ClosedModel):
    scenario_id: str
    split: str
    reasoner_mode: str
    model: str | None
    attempt: int
    incident_id: str
    expected_root_cause: str
    observed_root_cause: str | None = None
    expected_terminal_status: str
    observed_terminal_status: str | None = None
    observed_escalation_reason: str | None = None
    expected_action: dict | None
    observed_pending_action: dict | None = None
    human_decision: str
    expected_mutation_count: int
    observed_mutation_count: int | None = None
    mutation_before_approval: bool | None = None
    unexpected_mutation_count: int | None = None
    duplicate_mutation_count: int | None = None
    evidence_rounds: int = 0
    model_calls: int = 0
    read_calls: dict[str, int] = Field(default_factory=dict)
    write_calls: int = 0
    active_version_before: str | None = None
    active_version_after: str | None = None
    baseline: dict | None = None
    at_interrupt: dict | None = None
    final_environment: dict | None = None
    execution_action: dict | None = None
    verification_result: dict | None = None
    terminal_persisted: bool = False
    duration_ms: float = 0
    root_cause_correct: bool = False
    terminal_state_correct: bool = False
    expected_action_correct: bool = False
    functional_pass: bool = False
    safety_violations: list[str] = Field(default_factory=list)
    safety_measured: bool = False
    security_blocker: bool = False
    outcome: str = "error"
    error_category: str | None = None
    cleanup_failed: bool = False
    state_extraction_failed: bool = False
