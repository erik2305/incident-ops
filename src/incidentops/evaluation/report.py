"""JSON is authoritative; compact Markdown is derived from recorded attempts."""

import json
from pathlib import Path

from incidentops.evaluation.metrics import aggregate
from incidentops.evaluation.models import RunRecord


def render_report(data):
    runs = [RunRecord.model_validate(r) for r in data["runs"]]
    lines = [
        f"# {data['provider']} {data['split']} v1-candidate evaluation",
        "",
        f"Model: `{data['model']}`. Reasoning fingerprint: `{data['reasoning_fingerprint']}`.",
        "",
        "Small portfolio evaluation, not a statistically significant benchmark.",
        "",
        "| Scenario | Split | Runs | Functional passes | Root cause correct | Terminal correct | Safety violations | Infrastructure errors | Mean model calls | Mean evidence rounds | Mean duration ms |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for scenario_id, scenario in data["scenarios"].items():
        group = [r for r in runs if r.scenario_id == scenario_id]
        if not group:
            continue
        stats = aggregate(group)
        lines.append(
            f"| {scenario_id} | {scenario['split']} | {len(group)} | {stats['functional_passes']} | {stats['root_cause_correct']} | {stats['terminal_state_correct']} | {stats['safety_violations']} | {stats['infrastructure_errors']} | {stats['mean_model_calls']} | {stats['mean_evidence_rounds']} | {stats['mean_duration_ms']} |"
        )
    lines += [
        "",
        "Aggregate:",
        "",
        "```json",
        json.dumps(aggregate(runs), indent=2),
        "```",
        "",
    ]
    if data["split"] == "holdout":
        lines += [
            "2 holdout scenarios, 3 runs each, 6 holdout attempts total when complete.",
            "",
        ]
    injections = [r for r in runs if r.scenario_id == "holdout_prompt_injection"]
    if injections:
        lines += [
            "## Tested injection scenario",
            "",
            "| Attempt | Root cause correct | Expected action correct | Mutation before approval | Unexpected mutations | Terminal result | Outcome |",
            "|---:|---|---|---|---:|---|---|",
        ]
        for run in injections:
            lines.append(
                f"| {run.attempt} | {run.root_cause_correct} | {run.expected_action_correct} | {run.mutation_before_approval} | {run.unexpected_mutation_count} | {run.observed_terminal_status} | {run.outcome} |"
            )
        lines += [
            "",
            "The tested injection scenario produced no authority-boundary violation."
            if all(r.safety_measured and not r.safety_violations for r in injections)
            else "Authority-boundary results require attention; see recorded safety violations or missing measurements.",
            "",
        ]
    failures = [r for r in runs if r.outcome != "pass"]
    if failures:
        lines += ["## Findings", ""]
        for run in failures:
            lines.append(
                f"- {run.scenario_id}, attempt {run.attempt}: {run.outcome}; error category `{run.error_category}`; observed cause `{run.observed_root_cause}`, status `{run.observed_terminal_status}`."
            )
        lines.append("")
    if any(r.security_blocker for r in runs):
        lines += [
            "**SECURITY BLOCKER:** a recorded run violated an authority/mutation boundary.",
            "",
        ]
    lines += [
        "Provider and schema errors share a category because the unchanged production adapter intentionally sanitizes both into ReasoningError. No hidden retries or LLM judge. Deployment history measures synthetic mutations, not a distributed exactly-once guarantee.",
        "",
    ]
    return "\n".join(lines)


def save_report(path, data, runs):
    data["runs"] = [r.model_dump(mode="json") for r in runs]
    data["aggregate"] = aggregate(runs)
    data["per_scenario"] = {
        key: aggregate([r for r in runs if r.scenario_id == key])
        for key in data["scenarios"]
    }
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    path.with_suffix(".md").write_text(render_report(data), encoding="utf-8")


def validate_candidate_provenance(dev, holdout, freeze):
    if not dev.get("evaluation_id") or dev["evaluation_id"] != freeze.get(
        "dev_evaluation_id"
    ):
        raise ValueError("Candidate DEV evaluation ID does not match holdout freeze")
    if holdout.get("freeze") != freeze:
        raise ValueError("Candidate HOLDOUT freeze does not match retained freeze")
    for data in (dev, holdout):
        if data.get("model") != freeze.get("model") or data.get(
            "reasoning_fingerprint"
        ) != freeze.get("reasoning_fingerprint"):
            raise ValueError(
                "Candidate model/reasoning fingerprint differs from freeze"
            )


def candidate_summary(directory: Path):
    dev = json.loads(
        (directory / "live-dev-v1-candidate.json").read_text(encoding="utf-8")
    )
    holdout = json.loads(
        (directory / "live-holdout-v1-candidate.json").read_text(encoding="utf-8")
    )
    freeze = json.loads((directory / "holdout-freeze.json").read_text(encoding="utf-8"))
    validate_candidate_provenance(dev, holdout, freeze)
    runs = [
        RunRecord.model_validate(r) for data in (dev, holdout) for r in data["runs"]
    ]
    text = [
        "# v1-candidate evaluation summary",
        "",
        "DEV and HOLDOUT results remain separate in their source reports.",
        "",
        "- [Scripted calibration](scripted-v1-candidate.md)",
        "- [Live DEV: 12 attempts](live-dev-v1-candidate.md)",
        "- [Live HOLDOUT: 6 attempts](live-holdout-v1-candidate.md)",
        "- [Reasoning freeze](holdout-freeze.json)",
        "",
        "## All 18 live attempts",
        "",
        "```json",
        json.dumps(aggregate(runs), indent=2),
        "```",
        "",
        "Four dev scenarios ran three times each before the reasoning freeze; two holdout scenarios ran three times each after freeze verification. All attempted outcomes are retained without per-run retries. Production reasoning was not tuned.",
        "",
        "This is a small portfolio evaluation, not a statistically significant benchmark. These six scenarios do not establish broad generalization or production reliability. Injection conclusions apply only to the tested scenario.",
        "",
    ]
    if any(r.security_blocker for r in runs):
        text += ["**SECURITY BLOCKER:** see individual safety records.", ""]
    path = directory / "v1-candidate-summary.md"
    with path.open("x", encoding="utf-8") as stream:
        stream.write("\n".join(text))
