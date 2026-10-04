"""Read-only release consistency checks for the authoritative retained candidate."""

import json
from pathlib import Path

from incidentops.evaluation.freeze import reasoning_fingerprint
from incidentops.evaluation.metrics import aggregate, grade
from incidentops.evaluation.models import RunRecord
from incidentops.evaluation.report import render_report, validate_candidate_provenance
from incidentops.evaluation.scenarios import load_scenarios


def check_candidate(
    directory=Path("results/evaluation"), scenarios_root=Path("scenarios")
):
    scenarios = {s.id: s for s in load_scenarios(scenarios_root)}
    documents = {
        name: json.loads(
            (directory / f"{name}-v1-candidate.json").read_text(encoding="utf-8")
        )
        for name in ("scripted", "live-dev", "live-holdout")
    }
    freeze = json.loads((directory / "holdout-freeze.json").read_text(encoding="utf-8"))
    dev, holdout = documents["live-dev"], documents["live-holdout"]
    validate_candidate_provenance(dev, holdout, freeze)
    if reasoning_fingerprint(freeze["model"]) != freeze["reasoning_fingerprint"]:
        raise ValueError("Current reasoning surface differs from retained freeze")
    identifiers, incidents = set(), set()
    for name, data in documents.items():
        split = (
            "all" if name == "scripted" else "dev" if name == "live-dev" else "holdout"
        )
        repetitions = 1 if name == "scripted" else 3
        wanted = {
            key: s.model_dump(mode="json")
            for key, s in scenarios.items()
            if split == "all" or s.split == split
        }
        if (
            not data["complete"]
            or data["split"] != split
            or data["runs_per_scenario"] != repetitions
            or data["provider"] != ("scripted" if name == "scripted" else "live")
            or data["scenarios"] != wanted
            or not data["evaluation_id"]
            or data["evaluation_id"] in identifiers
            or data["reasoning_fingerprint"] != freeze["reasoning_fingerprint"]
        ):
            raise ValueError(f"Invalid candidate metadata/scenarios: {name}")
        identifiers.add(data["evaluation_id"])
        records = [RunRecord.model_validate(r) for r in data["runs"]]
        pairs = {(r.scenario_id, r.attempt) for r in records}
        expected_pairs = {
            (key, attempt) for key in wanted for attempt in range(1, repetitions + 1)
        }
        if pairs != expected_pairs or len(records) != len(expected_pairs):
            raise ValueError(f"Invalid candidate attempt counts: {name}")
        for record in records:
            scenario = scenarios[record.scenario_id]
            if (
                record.incident_id in incidents
                or not record.incident_id
                or record.split != scenario.split
                or record.reasoner_mode != data["provider"]
                or record.model != (None if name == "scripted" else freeze["model"])
                or record.expected_root_cause != scenario.expected.root_cause
                or record.expected_terminal_status != scenario.expected.terminal_status
                or record.expected_action
                != (
                    scenario.expected.expected_action.model_dump()
                    if scenario.expected.expected_action
                    else None
                )
                or record.human_decision != scenario.human_decision
                or record.expected_mutation_count
                != scenario.expected.expected_mutation_count
                or record.write_calls != scenario.expected.expected_mutation_count
            ):
                raise ValueError(
                    f"Candidate run identity/expectation/telemetry mismatch: {name}"
                )
            incidents.add(record.incident_id)
            original = record.model_dump()
            grade(scenario, record)
            if record.model_dump() != original or not record.safety_measured:
                raise ValueError(f"Candidate deterministic grading mismatch: {name}")
        if data["aggregate"] != aggregate(records) or data["per_scenario"] != {
            key: aggregate([r for r in records if r.scenario_id == key])
            for key in wanted
        }:
            raise ValueError(f"Candidate aggregate mismatch: {name}")
        if (directory / f"{name}-v1-candidate.md").read_text(
            encoding="utf-8"
        ) != render_report(data):
            raise ValueError(f"Derivative Markdown differs from JSON: {name}")
    for split in ("dev", "holdout"):
        if set(freeze[f"{split}_scenario_ids"]) != {
            key for key, s in scenarios.items() if s.split == split
        }:
            raise ValueError("Freeze scenario membership mismatch")
    print(
        "Candidate integrity: 6 scripted + 12 DEV + 6 HOLDOUT; unique IDs, freeze linkage, grading and aggregates verified"
    )


if __name__ == "__main__":
    check_candidate()
