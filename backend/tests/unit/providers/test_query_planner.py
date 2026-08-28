from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.jobs.schemas import (
    ConfirmedScorecard,
    CriterionKind,
    ExtractionStatus,
    ScorecardCriterion,
)
from app.providers.query_planner import QueryPlanner


def _scorecard(**overrides: object) -> ConfirmedScorecard:
    values: dict[str, object] = {
        "id": uuid4(),
        "job_id": uuid4(),
        "version": 1,
        "confirmed_at": datetime(2026, 8, 15, tzinfo=UTC),
        "extraction_status": ExtractionStatus.READY,
        "target_titles": ["Product Manager"],
        "criteria": [
            ScorecardCriterion(
                key="payments",
                label="Payments platform experience",
                kind=CriterionKind.MUST_HAVE,
            )
        ],
        "seniority": ["senior"],
        "minimum_years": None,
        "maximum_years": None,
        "locations": ["New York, NY"],
        "industry_code": "financial_services.banking",
        "suggested_adjacent_industries": [],
        "uncertainties": [],
    }
    values.update(overrides)
    return ConfirmedScorecard.model_validate(values)


def test_query_planner_separates_exact_and_adjacent_industries() -> None:
    scorecard = _scorecard(
        target_titles=["Product Manager", "Senior Product Manager"],
        industry_code="financial_services.banking",
        suggested_adjacent_industries=["technology.fintech"],
    )

    queries = QueryPlanner(max_queries=8).compile(scorecard)

    assert 2 <= len(queries) <= 8
    assert any(
        query.industry_codes == ("financial_services.banking",) for query in queries
    )
    assert any(query.industry_codes == ("technology.fintech",) for query in queries)


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("early_career", ("entry", "intern")),
        ("mid_level", ("entry", "senior", "manager")),
        (
            "senior",
            ("senior", "manager", "director", "head", "vp", "c_suite"),
        ),
    ],
)
def test_query_planner_maps_canonical_presets_for_recall(
    level: str, expected: tuple[str, ...]
) -> None:
    queries = QueryPlanner().compile(_scorecard(seniority=[level]))

    assert queries
    assert all(query.seniorities == expected for query in queries)


def test_custom_bounds_omit_inactive_provider_seniority_filters() -> None:
    queries = QueryPlanner().compile(
        _scorecard(seniority=["senior"], minimum_years=5, maximum_years=8)
    )

    assert all(query.seniorities == () for query in queries)


def test_multiple_presets_use_stable_union() -> None:
    queries = QueryPlanner().compile(_scorecard(seniority=["senior", "early_career"]))

    assert all(
        query.seniorities
        == ("entry", "intern", "senior", "manager", "director", "head", "vp", "c_suite")
        for query in queries
    )


def test_query_planner_chunks_titles_and_bounds_queries() -> None:
    scorecard = _scorecard(
        target_titles=[
            "Product Manager",
            "Senior Product Manager",
            "Lead Product Manager",
            "Group Product Manager",
            "VP Product",
            "Head of Product",
            "Director of Product",
        ],
        seniority=["senior"],
        suggested_adjacent_industries=[
            "technology.fintech",
            "consumer",
            "retail",
        ],
    )

    queries = QueryPlanner(max_queries=20).compile(scorecard)

    assert len(queries) == 8
    assert all(1 <= len(query.titles) <= 3 for query in queries)
    assert all(
        query.seniorities == ("senior", "manager", "director", "head", "vp", "c_suite")
        for query in queries
    )


def test_query_planner_keeps_job_relevant_keywords_and_deduplicates_stably() -> None:
    scorecard = _scorecard(
        target_titles=[" Product Manager ", "product manager", "Product Lead"],
        criteria=[
            ScorecardCriterion(
                key="payments",
                label="Payments platform experience",
                kind=CriterionKind.MUST_HAVE,
            ),
            ScorecardCriterion(
                key="analytics",
                label="SQL analytics",
                kind=CriterionKind.PREFERENCE,
            ),
            ScorecardCriterion(
                key="marketing",
                label="Marketing-only background",
                kind=CriterionKind.EXCLUSION,
                source_text="Do not source marketing-only profiles",
            ),
        ],
        locations=[" New York, NY ", "new york, ny", "Remote"],
    )

    first = QueryPlanner(max_queries=8).compile(scorecard)
    second = QueryPlanner(max_queries=8).compile(scorecard)

    assert first == second
    assert first[0].titles == ("Product Manager", "Product Lead")
    assert first[0].person_locations == ("New York, NY", "Remote")
    assert first[0].keywords == (
        "Banking",
        "Payments platform experience",
        "SQL analytics",
    )
    assert len(first[0].query_hash) == 64
    assert first[0].query_hash == second[0].query_hash


def test_query_planner_deduplicates_identical_queries_after_normalization() -> None:
    scorecard = _scorecard(
        target_titles=["Product Manager", " product manager "],
        suggested_adjacent_industries=[
            "financial_services.banking",
            "technology.fintech",
            " TECHNOLOGY.FINTECH ",
        ],
    )

    queries = QueryPlanner(max_queries=8).compile(scorecard)

    assert [query.industry_codes for query in queries] == [
        ("financial_services.banking",),
        ("technology.fintech",),
    ]
