from collections.abc import Callable

import pytest
from pydantic import ValidationError

from app.candidates.schemas import CandidateProfile
from app.jobs.schemas import ConfirmedScorecard, CriterionKind, ScorecardCriterion
from app.matching.engine import MatchingEngine
from app.matching.explanations import format_explanation
from app.matching.schemas import EvidenceState, ScoreBreakdown


def _work_eligibility() -> ScorecardCriterion:
    return ScorecardCriterion(
        key="work_eligibility",
        label="Authorized to work in the United States",
        kind=CriterionKind.MUST_HAVE,
        source_text="Authorized to work in the United States",
    )


def test_fixed_weights_total_one_hundred_for_fully_supported_evidence(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    scorecard = scorecard_factory(
        criteria=[
            ScorecardCriterion(
                key="payments",
                label="Payments platform experience",
                kind=CriterionKind.MUST_HAVE,
            ),
            _work_eligibility(),
        ]
    )
    candidate = candidate_factory(
        current_title="Sr. Product Manager",
        skills=("Payment Processing",),
        location="NYC",
        seniority="Sr",
        work_eligibility="US work authorized",
    )

    result = engine.evaluate(scorecard, candidate)

    assert result.breakdown == ScoreBreakdown(
        role_and_skills=35,
        scope_seniority_years=25,
        industry=20,
        location_and_eligibility=10,
        recency_and_trajectory=10,
    )
    assert result.total == 100
    assert result.total == sum(result.breakdown.model_dump().values())
    assert result.scoring_version == "matching-v2"


@pytest.mark.parametrize(
    ("seniority", "years", "state"),
    [
        (["early_career"], 3.0, EvidenceState.SUPPORTED),
        (["mid_level"], 3.0, EvidenceState.SUPPORTED),
        (["mid_level"], 10.0, EvidenceState.FAILED),
        (["early_career", "senior"], 7.0, EvidenceState.FAILED),
        (["early_career", "senior"], 12.0, EvidenceState.SUPPORTED),
    ],
)
def test_seniority_presets_match_numeric_years(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
    seniority: list[str],
    years: float,
    state: EvidenceState,
) -> None:
    result = engine.evaluate(
        scorecard_factory(
            seniority=seniority,
            minimum_years=None,
            maximum_years=None,
        ),
        candidate_factory(
            years_experience=years,
            seniority="unrelated-provider-label",
        ),
    )

    evaluation = next(
        item for item in result.criteria if item.key == "component.years_experience"
    )

    assert evaluation.state is state


def test_custom_open_range_overrides_presets(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    result = engine.evaluate(
        scorecard_factory(
            seniority=["early_career"],
            minimum_years=5,
            maximum_years=None,
        ),
        candidate_factory(years_experience=6.0),
    )

    evaluation = next(
        item for item in result.criteria if item.key == "component.years_experience"
    )

    assert evaluation.state is EvidenceState.SUPPORTED
    assert not any(item.key == "component.seniority" for item in result.criteria)


def test_title_or_provider_label_cannot_replace_missing_years(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    result = engine.evaluate(
        scorecard_factory(
            seniority=["senior"],
            minimum_years=None,
            maximum_years=None,
        ),
        candidate_factory(
            years_experience=None,
            seniority="senior",
            current_title="Senior Director",
        ),
    )

    evaluation = next(
        item for item in result.criteria if item.key == "component.years_experience"
    )

    assert evaluation.state is EvidenceState.UNKNOWN
    assert evaluation.points == 0


def test_no_requirement_produces_no_scope_criterion(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    result = engine.evaluate(
        scorecard_factory(
            seniority=[],
            minimum_years=None,
            maximum_years=None,
        ),
        candidate_factory(years_experience=None),
    )

    assert not any(
        item.key == "component.years_experience" for item in result.criteria
    )
    assert not any(
        item.key == "component.scope_seniority_years" for item in result.criteria
    )
    assert result.breakdown.scope_seniority_years == 0


@pytest.mark.parametrize(
    ("industry_codes", "expected", "state"),
    [
        (("financial_services.banking",), 20, EvidenceState.SUPPORTED),
        (("technology.fintech",), 12, EvidenceState.SUPPORTED),
        (("retail",), 0, EvidenceState.FAILED),
        ((), 0, EvidenceState.UNKNOWN),
    ],
)
def test_industry_exact_adjacency_unrelated_and_unknown_scoring(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
    industry_codes: tuple[str, ...],
    expected: int,
    state: EvidenceState,
) -> None:
    result = engine.evaluate(
        scorecard_factory(), candidate_factory(industry_codes=industry_codes)
    )

    industry = next(
        item for item in result.criteria if item.key == "component.industry"
    )
    assert result.breakdown.industry == expected
    assert industry.state is state
    assert industry.points == expected


def test_unapproved_taxonomy_adjacency_receives_zero(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    scorecard = scorecard_factory(suggested_adjacent_industries=[])

    result = engine.evaluate(
        scorecard, candidate_factory(industry_codes=("technology.fintech",))
    )

    assert result.breakdown.industry == 0


def test_recruiter_criterion_cannot_be_hidden_by_a_component_key_collision(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    scorecard = scorecard_factory(
        criteria=[
            ScorecardCriterion(
                key="industry",
                label="Industry experience",
                kind=CriterionKind.MUST_HAVE,
            )
        ]
    )

    result = engine.evaluate(
        scorecard,
        candidate_factory(
            skills=("retail merchandising",),
            industry_codes=("financial_services.banking",),
        ),
    )

    recruiter_criterion = next(
        item for item in result.criteria if item.key == "industry"
    )
    industry_component = next(
        item for item in result.criteria if item.key == "component.industry"
    )
    assert recruiter_criterion.state is EvidenceState.FAILED
    assert industry_component.state is EvidenceState.SUPPORTED
    assert result.failed_must_haves == ("industry",)
    assert result.classification == "near_match"


def test_unknown_evidence_is_zero_and_score_is_not_renormalized(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    scorecard = scorecard_factory(criteria=[_work_eligibility()])
    candidate = candidate_factory(work_eligibility=None)

    result = engine.evaluate(scorecard, candidate)

    assert result.breakdown.location_and_eligibility == 5
    assert "work_eligibility" in result.unknown_keys
    assert result.total == sum(result.breakdown.model_dump().values())
    assert result.total < 100


def test_explanation_only_partitions_stored_evaluation_summaries(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    scorecard = scorecard_factory(
        criteria=[
            ScorecardCriterion(
                key="payments",
                label="Payments platform experience",
                kind=CriterionKind.MUST_HAVE,
            ),
            _work_eligibility(),
        ]
    )
    result = engine.evaluate(
        scorecard,
        candidate_factory(skills=("payments",), work_eligibility=None),
    )

    explanation = format_explanation(result)

    assert explanation.supported == tuple(
        item.summary
        for item in result.criteria
        if item.state is EvidenceState.SUPPORTED
    )
    assert explanation.failed == tuple(
        item.summary for item in result.criteria if item.state is EvidenceState.FAILED
    )
    assert explanation.unknown == tuple(
        item.summary for item in result.criteria if item.state is EvidenceState.UNKNOWN
    )


def test_score_breakdown_rejects_values_outside_frozen_weights() -> None:
    with pytest.raises(ValidationError):
        ScoreBreakdown(
            role_and_skills=36,
            scope_seniority_years=25,
            industry=20,
            location_and_eligibility=10,
            recency_and_trajectory=10,
        )
