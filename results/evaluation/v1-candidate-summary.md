# v1-candidate evaluation summary

DEV and HOLDOUT results remain separate in their source reports.

- [Scripted calibration](scripted-v1-candidate.md)
- [Live DEV: 12 attempts](live-dev-v1-candidate.md)
- [Live HOLDOUT: 6 attempts](live-holdout-v1-candidate.md)
- [Reasoning freeze](holdout-freeze.json)

## All 18 live attempts

```json
{
  "attempts": 18,
  "functional_passes": 18,
  "clean_passes": 18,
  "functional_pass_percent": 100.0,
  "root_cause_correct": 18,
  "terminal_state_correct": 18,
  "expected_action_correct": 18,
  "safety_violations": 0,
  "runs_with_safety_violations": 0,
  "unmeasured_safety_runs": 0,
  "infrastructure_errors": 0,
  "unexpected_interrupts": 0,
  "mean_model_calls": 1.83,
  "mean_evidence_rounds": 1.83,
  "mean_duration_ms": 8376.5
}
```

Four dev scenarios ran three times each before the reasoning freeze; two holdout scenarios ran three times each after freeze verification. All attempted outcomes are retained without per-run retries. Production reasoning was not tuned.

This is a small portfolio evaluation, not a statistically significant benchmark. These six scenarios do not establish broad generalization or production reliability. Injection conclusions apply only to the tested scenario.
