from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT / "backend"))

from app.candidates.schemas import CandidateProfile
from app.jobs.schemas import ConfirmedScorecard
from app.matching.engine import MatchingEngine

Market = Literal["IN", "US"]
DatasetSource = Literal["synthetic", "recruiter_panel"]
Classification = Literal["main", "near_match"]
_EMAIL = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE = re.compile(r"(?<![\w-])\+?(?:\d[\s().-]*){8,15}(?![\w-])")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_DISALLOWED_KEYS = frozenset(
    {"email", "phone", "linkedin_url", "profile_url", "job_description"}
)


class EvaluationGateError(RuntimeError):
    pass


class EvaluationJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_key: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    market: Market
    source: DatasetSource
    deidentified: bool
    annotation_version: str = Field(min_length=3, max_length=64)
    panel_reference: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    scorecard: dict[str, object]


class RecruiterJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_key: str
    candidate_key: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    relevant: bool
    hard_gate_eligible: bool
    expected_classification: Classification | None = None
    annotator_hash: str = Field(pattern=r"^hmac-sha256:[0-9a-f]{64}$")
    candidate: dict[str, object]


class EvaluationDataset(BaseModel):
    model_config = ConfigDict(frozen=True)

    jobs: list[EvaluationJob]
    judgments: list[RecruiterJudgment]

    @property
    def dataset_kind(self) -> DatasetSource:
        if self.jobs and all(job.source == "recruiter_panel" for job in self.jobs):
            return "recruiter_panel"
        return "synthetic"

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:
        keys = [job.job_key for job in self.jobs]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate job_key")
        known = set(keys)
        judgment_keys = [(item.job_key, item.candidate_key) for item in self.judgments]
        if len(judgment_keys) != len(set(judgment_keys)):
            raise ValueError("duplicate job/candidate judgment")
        if any(item.job_key not in known for item in self.judgments):
            raise ValueError("judgment references unknown job")
        return self


class EvaluationMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    hard_gate_precision: float = Field(ge=0, le=1)
    hard_gate_recall: float = Field(ge=0, le=1)
    hard_gate_f1: float = Field(ge=0, le=1)
    ndcg_at_20: float = Field(ge=0, le=1)
    top_20_acceptance_proxy: float = Field(ge=0, le=1)


class EvaluationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    dataset_kind: DatasetSource
    dataset_sha256: str
    scoring_version: str
    job_count: int
    judgment_count: int
    metrics: EvaluationMetrics
    classifications: dict[str, Classification]


class EvaluationBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_kind: DatasetSource
    dataset_sha256: str
    scoring_version: str
    created_at: datetime
    metrics: EvaluationMetrics
    classifications: dict[str, Classification]


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        raise EvaluationGateError(f"dataset_file_missing:{path}")
    records: list[dict[str, object]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise EvaluationGateError(f"invalid_jsonl:{path}:{line_number}") from error
        if not isinstance(value, dict):
            raise EvaluationGateError(
                f"jsonl_record_must_be_object:{path}:{line_number}"
            )
        records.append(value)
    return records


def load_dataset(jobs_path: Path, judgments_path: Path) -> EvaluationDataset:
    return EvaluationDataset.model_validate(
        {
            "jobs": _load_jsonl(jobs_path),
            "judgments": _load_jsonl(judgments_path),
        }
    )


def _contains_personal_data(value: object, *, key: str = "") -> bool:
    if key.casefold() in _DISALLOWED_KEYS and value not in (None, "", [], {}):
        return True
    if isinstance(value, dict):
        return any(
            _contains_personal_data(item, key=str(item_key))
            for item_key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_personal_data(item, key=key) for item in value)
    if isinstance(value, str):
        if _UUID.fullmatch(value):
            return False
        return bool(
            _EMAIL.search(value) or _PHONE.search(value) or "http" in value.casefold()
        )
    return False


def require_recruiter_panel(dataset: EvaluationDataset) -> None:
    counts: dict[str, int] = defaultdict(int)
    for judgment in dataset.judgments:
        counts[judgment.job_key] += 1
    failures: list[str] = []
    if dataset.dataset_kind != "recruiter_panel":
        failures.append("source")
    if len(dataset.jobs) < 30:
        failures.append("job_count")
    if {job.market for job in dataset.jobs} != {"IN", "US"}:
        failures.append("markets")
    if any(counts[job.job_key] < 20 for job in dataset.jobs):
        failures.append("judgments_per_job")
    if any(not job.deidentified for job in dataset.jobs):
        failures.append("deidentification_attestation")
    if any(
        _contains_personal_data(record)
        for record in (
            *(job.model_dump(mode="json") for job in dataset.jobs),
            *(judgment.model_dump(mode="json") for judgment in dataset.judgments),
        )
    ):
        failures.append("personal_data_detected")
    if failures:
        raise EvaluationGateError(
            "recruiter_panel_required:" + ",".join(sorted(set(failures)))
        )


def _dataset_digest(dataset: EvaluationDataset) -> str:
    canonical = json.dumps(
        dataset.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _dcg(values: list[int]) -> float:
    return sum(value / math.log2(index + 2) for index, value in enumerate(values))


def _ndcg_at_k(items: list[tuple[int, str, bool]], *, k: int) -> float:
    ordered = sorted(items, key=lambda item: (-item[0], item[1]))[:k]
    relevance = [int(item[2]) for item in ordered]
    ideal = sorted((int(item[2]) for item in items), reverse=True)[:k]
    return _ratio(_dcg(relevance), _dcg(ideal))


def evaluate_dataset(dataset: EvaluationDataset) -> EvaluationReport:
    if not dataset.jobs or not dataset.judgments:
        raise EvaluationGateError("evaluation_dataset_empty")
    engine = MatchingEngine()
    jobs = {job.job_key: job for job in dataset.jobs}
    ranked: dict[str, list[tuple[int, str, bool]]] = defaultdict(list)
    classifications: dict[str, Classification] = {}
    true_positive = false_positive = false_negative = 0
    scoring_versions: set[str] = set()
    for judgment in dataset.judgments:
        scorecard = ConfirmedScorecard.model_validate(jobs[judgment.job_key].scorecard)
        candidate = CandidateProfile.model_validate(judgment.candidate)
        result = engine.evaluate(scorecard, candidate)
        scoring_versions.add(result.scoring_version)
        predicted_eligible = result.classification == "main"
        if predicted_eligible and judgment.hard_gate_eligible:
            true_positive += 1
        elif predicted_eligible:
            false_positive += 1
        elif judgment.hard_gate_eligible:
            false_negative += 1
        key = f"{judgment.job_key}/{judgment.candidate_key}"
        classifications[key] = result.classification
        if (
            judgment.expected_classification is not None
            and result.classification != judgment.expected_classification
        ):
            raise EvaluationGateError(f"mandatory_classification_changed:{key}")
        ranked[judgment.job_key].append(
            (result.total, judgment.candidate_key, judgment.relevant)
        )
    if len(scoring_versions) != 1:
        raise EvaluationGateError("mixed_scoring_versions")
    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    f1 = _ratio(2 * precision * recall, precision + recall)
    ndcg_values: list[float] = []
    acceptance_values: list[float] = []
    for items in ranked.values():
        ordered = sorted(items, key=lambda item: (-item[0], item[1]))[:20]
        relevance = [int(item[2]) for item in ordered]
        ndcg_values.append(_ndcg_at_k(items, k=20))
        acceptance_values.append(_ratio(sum(relevance), len(relevance)))
    metrics = EvaluationMetrics(
        hard_gate_precision=precision,
        hard_gate_recall=recall,
        hard_gate_f1=f1,
        ndcg_at_20=_ratio(sum(ndcg_values), len(ndcg_values)),
        top_20_acceptance_proxy=_ratio(sum(acceptance_values), len(acceptance_values)),
    )
    return EvaluationReport(
        dataset_kind=dataset.dataset_kind,
        dataset_sha256=_dataset_digest(dataset),
        scoring_version=scoring_versions.pop(),
        job_count=len(dataset.jobs),
        judgment_count=len(dataset.judgments),
        metrics=metrics,
        classifications=classifications,
    )


def compare_to_baseline(
    current: EvaluationMetrics, baseline: EvaluationMetrics
) -> list[str]:
    regressions: list[str] = []
    f1_drop = baseline.hard_gate_f1 - current.hard_gate_f1
    ndcg_drop = baseline.ndcg_at_20 - current.ndcg_at_20
    if f1_drop > 0.01:
        regressions.append(f"hard_gate_f1_regressed_by_{f1_drop:.4f}")
    if ndcg_drop > 0.03:
        regressions.append(f"ndcg_at_20_regressed_by_{ndcg_drop:.4f}")
    return regressions


def compare_report_to_baseline(
    report: EvaluationReport, baseline: EvaluationBaseline
) -> list[str]:
    failures = compare_to_baseline(report.metrics, baseline.metrics)
    if report.dataset_sha256 != baseline.dataset_sha256:
        failures.append("baseline_dataset_provenance_mismatch")
    if report.scoring_version != baseline.scoring_version:
        failures.append("baseline_scoring_version_mismatch")
    for key, expected in baseline.classifications.items():
        if report.classifications.get(key) != expected:
            failures.append(f"mandatory_classification_changed:{key}")
    return failures


def _parse_args(argv: list[str]) -> argparse.Namespace:
    fixtures = Path(__file__).parent / "fixtures"
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=Path, default=fixtures / "jobs.jsonl")
    parser.add_argument("--judgments", type=Path, default=fixtures / "judgments.jsonl")
    parser.add_argument("--baseline", type=Path, default=fixtures / "baseline.json")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--fail-on-regression", action="store_true")
    parser.add_argument("--require-recruiter-panel", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        dataset = load_dataset(args.jobs, args.judgments)
        if args.require_recruiter_panel:
            require_recruiter_panel(dataset)
        report = evaluate_dataset(dataset)
        failures: list[str] = []
        if args.fail_on_regression:
            if not args.baseline.exists():
                failures.append("evaluation_baseline_missing")
            else:
                baseline = EvaluationBaseline.model_validate_json(
                    args.baseline.read_text(encoding="utf-8")
                )
                failures.extend(compare_report_to_baseline(report, baseline))
        rendered = json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True)
        if args.report:
            args.report.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        if failures:
            raise EvaluationGateError(";".join(failures))
    except EvaluationGateError as error:
        print(f"evaluation_gate_failed:{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
