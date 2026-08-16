from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
from evaluate_matching import (
    EvaluationDataset,
    EvaluationGateError,
    EvaluationMetrics,
    _ndcg_at_k,
    compare_to_baseline,
    evaluate_dataset,
    load_dataset,
    require_recruiter_panel,
)
from pydantic import ValidationError

FIXTURES = Path(__file__).parent / "fixtures"


def test_synthetic_fixture_exercises_real_matching_engine() -> None:
    dataset = load_dataset(
        FIXTURES / "synthetic_jobs.jsonl",
        FIXTURES / "synthetic_judgments.jsonl",
    )

    report = evaluate_dataset(dataset)

    assert report.dataset_kind == "synthetic"
    assert report.job_count == 2
    assert report.judgment_count == 8
    assert report.metrics.hard_gate_precision == 1.0
    assert report.metrics.hard_gate_recall == 1.0
    assert 0.0 <= report.metrics.ndcg_at_20 <= 1.0
    assert 0.0 <= report.metrics.top_20_acceptance_proxy <= 1.0


def test_launch_gate_rejects_synthetic_or_incomplete_panel_data() -> None:
    dataset = load_dataset(
        FIXTURES / "synthetic_jobs.jsonl",
        FIXTURES / "synthetic_judgments.jsonl",
    )

    with pytest.raises(EvaluationGateError, match="recruiter_panel_required"):
        require_recruiter_panel(dataset)


def test_launch_gate_requires_thirty_jobs_both_markets_and_twenty_judgments() -> None:
    dataset = EvaluationDataset.model_validate(
        {
            "jobs": [
                {
                    "job_key": f"job-{index}",
                    "market": "IN" if index < 15 else "US",
                    "source": "recruiter_panel",
                    "deidentified": True,
                    "annotation_version": "recruiter-panel-v1",
                    "panel_reference": "sha256:" + "a" * 64,
                    "scorecard": {},
                }
                for index in range(30)
            ],
            "judgments": [
                {
                    "job_key": f"job-{job_index}",
                    "candidate_key": f"candidate-{candidate_index}",
                    "relevant": True,
                    "hard_gate_eligible": True,
                    "annotator_hash": "hmac-sha256:" + "b" * 64,
                    "candidate": {},
                }
                for job_index in range(30)
                for candidate_index in range(20)
            ],
        }
    )

    require_recruiter_panel(dataset)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("panel_reference", "panel-owner@example.com"),
        ("panel_reference", "https://evidence.example/panel/123"),
        ("panel_reference", "sha256:not-a-digest"),
        ("annotator_hash", "plain-sha256-digest"),
        ("annotator_hash", "hmac-sha256:not-a-digest"),
    ],
)
def test_annotation_provenance_requires_safe_immutable_digests(
    field: str, value: str
) -> None:
    record = {
        "job_key": "job-001",
        "market": "IN",
        "source": "recruiter_panel",
        "deidentified": True,
        "annotation_version": "panel-v1",
        "panel_reference": "sha256:" + "a" * 64,
        "scorecard": {},
    }
    judgment = {
        "job_key": "job-001",
        "candidate_key": "candidate-001",
        "relevant": True,
        "hard_gate_eligible": True,
        "annotator_hash": "hmac-sha256:" + "b" * 64,
        "candidate": {},
    }
    target = judgment if field == "annotator_hash" else record
    target[field] = value

    with pytest.raises(ValidationError):
        EvaluationDataset.model_validate({"jobs": [record], "judgments": [judgment]})


def test_launch_gate_scans_annotation_metadata_for_personal_data() -> None:
    dataset = EvaluationDataset.model_validate(
        {
            "jobs": [
                {
                    "job_key": "job-001",
                    "market": "IN",
                    "source": "recruiter_panel",
                    "deidentified": True,
                    "annotation_version": "owner@example.com",
                    "panel_reference": "sha256:" + "a" * 64,
                    "scorecard": {},
                }
            ],
            "judgments": [],
        }
    )

    with pytest.raises(EvaluationGateError, match="personal_data_detected"):
        require_recruiter_panel(dataset)


def test_ndcg_ideal_ranking_uses_all_judged_candidates() -> None:
    ranked = [
        (100 - index, f"candidate-{index:02d}", index in {19, 20})
        for index in range(21)
    ]

    actual = _ndcg_at_k(ranked, k=20)
    expected = (1 / math.log2(21)) / (1 + 1 / math.log2(3))

    assert actual == pytest.approx(expected)


def test_regression_thresholds_fail_closed() -> None:
    baseline = EvaluationMetrics(
        hard_gate_precision=0.92,
        hard_gate_recall=0.90,
        hard_gate_f1=0.91,
        ndcg_at_20=0.80,
        top_20_acceptance_proxy=0.75,
    )
    current = EvaluationMetrics(
        hard_gate_precision=0.91,
        hard_gate_recall=0.88,
        hard_gate_f1=0.899,
        ndcg_at_20=0.769,
        top_20_acceptance_proxy=0.74,
    )

    regressions = compare_to_baseline(current, baseline)

    assert regressions == [
        "hard_gate_f1_regressed_by_0.0110",
        "ndcg_at_20_regressed_by_0.0310",
    ]


def test_fixture_lines_are_valid_json_without_personal_data() -> None:
    for fixture in FIXTURES.glob("*.jsonl"):
        for line in fixture.read_text().splitlines():
            record = json.loads(line)
            serialized = json.dumps(record).casefold()
            assert "@" not in serialized
            assert "phone" not in serialized
            assert "linkedin" not in serialized


def test_evaluator_cli_runs_from_backend_project() -> None:
    backend = Path(__file__).parents[1] / "backend"
    result = subprocess.run(
        [
            sys.executable,
            "../evaluation/evaluate_matching.py",
            "--jobs",
            "../evaluation/fixtures/synthetic_jobs.jsonl",
            "--judgments",
            "../evaluation/fixtures/synthetic_judgments.jsonl",
        ],
        cwd=backend,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert '"dataset_kind": "synthetic"' in result.stdout
