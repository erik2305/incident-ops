"""Focused release provenance and CI skip-safety regressions."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from incidentops.evaluation.report import (
    candidate_summary,
    validate_candidate_provenance,
)
from scripts.check_candidate import check_candidate
from scripts.check_ci_results import check_pytest, check_scripted

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results/evaluation"


def candidate():
    dev = json.loads((RESULTS / "live-dev-v1-candidate.json").read_text())
    holdout = json.loads((RESULTS / "live-holdout-v1-candidate.json").read_text())
    freeze = json.loads((RESULTS / "holdout-freeze.json").read_text())
    return dev, holdout, freeze


def test_retained_candidate_integrity():
    check_candidate(RESULTS, ROOT / "scenarios")


def test_combiner_rejects_unrelated_dev_before_writing(tmp_path):
    dev, holdout, freeze = candidate()
    dev["evaluation_id"] = "unrelated"
    for name, data in (
        ("live-dev-v1-candidate", dev),
        ("live-holdout-v1-candidate", holdout),
        ("holdout-freeze", freeze),
    ):
        (tmp_path / f"{name}.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="DEV evaluation ID"):
        candidate_summary(tmp_path)
    assert not (tmp_path / "v1-candidate-summary.md").exists()


def test_matching_provenance_preserves_derivative_summary(tmp_path):
    for name in (
        "live-dev-v1-candidate.json",
        "live-holdout-v1-candidate.json",
        "holdout-freeze.json",
    ):
        (tmp_path / name).write_bytes((RESULTS / name).read_bytes())
    candidate_summary(tmp_path)
    assert (tmp_path / "v1-candidate-summary.md").read_text() == (
        RESULTS / "v1-candidate-summary.md"
    ).read_text()


@pytest.mark.parametrize("field", ["model", "reasoning_fingerprint", "freeze"])
def test_provenance_rejects_holdout_mismatch(field):
    dev, holdout, freeze = candidate()
    holdout[field] = "unrelated"
    with pytest.raises(ValueError):
        validate_candidate_provenance(dev, holdout, freeze)


@pytest.mark.parametrize("tag", ["skipped", "failure", "error"])
def test_ci_refuses_selected_skips_failures_and_errors(tmp_path, tag):
    report = tmp_path / "junit.xml"
    report.write_text(
        f'<testsuites><testsuite><testcase name="a"><{tag}/></testcase></testsuite></testsuites>'
    )
    with pytest.raises(ValueError):
        check_pytest(report)


def test_ci_accepts_passes_and_refuses_empty_report(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(
        '<testsuites><testsuite><testcase name="a"/></testsuite></testsuites>'
    )
    check_pytest(report)
    report.write_text("<testsuites/>")
    with pytest.raises(ValueError):
        check_pytest(report)


def test_ci_checks_scripted_results_not_only_exit_code(tmp_path):
    data = json.loads((RESULTS / "scripted-v1-candidate.json").read_text())
    report = tmp_path / "scripted.json"
    report.write_text(json.dumps(data))
    check_scripted(report)
    invalid = deepcopy(data)
    invalid["runs"][0]["safety_measured"] = False
    report.write_text(json.dumps(invalid))
    with pytest.raises(ValueError):
        check_scripted(report)
