from collections.abc import Callable

from app.candidates.schemas import CandidateProfile
from app.jobs.schemas import ConfirmedScorecard, CriterionKind, ScorecardCriterion
from app.matching.engine import MatchingEngine
from app.matching.schemas import CriterionEvaluation, EvidenceState, MatchResult


def _criterion(result_key: str, result: MatchResult) -> CriterionEvaluation:
    return next(item for item in result.criteria if item.key == result_key)


def test_explicitly_failed_must_have_becomes_near_match(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    scorecard = scorecard_factory()
    candidate = candidate_factory(skills=("retail merchandising",))

    result = engine.evaluate(scorecard, candidate)

    assert result.classification == "near_match"
    assert result.failed_must_haves == ("payments",)
    evaluation = _criterion("payments", result)
    assert evaluation.state is EvidenceState.FAILED
    assert evaluation.evidence == ("retail merchandising",)


def test_unknown_optional_must_have_stays_main_with_visible_uncertainty(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    scorecard = scorecard_factory()
    candidate = candidate_factory(skills=())

    result = engine.evaluate(scorecard, candidate)

    assert result.classification == "main"
    assert result.failed_must_haves == ()
    assert "payments" in result.unknown_keys
    assert _criterion("payments", result).state is EvidenceState.UNKNOWN


def test_unknown_mandatory_must_have_becomes_near_match(
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
                evidence_required=True,
            )
        ]
    )

    result = engine.evaluate(scorecard, candidate_factory(skills=()))

    assert result.classification == "near_match"
    assert result.failed_must_haves == ()
    assert result.unknown_keys == tuple(sorted(result.unknown_keys))
    assert "payments" in result.unknown_keys


def test_work_eligibility_is_never_inferred_from_other_profile_fields(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    scorecard = scorecard_factory(
        criteria=[
            ScorecardCriterion(
                key="work_eligibility",
                label="Authorized to work in the United States",
                kind=CriterionKind.MUST_HAVE,
                evidence_required=True,
                source_text="Authorized to work in the United States",
            )
        ]
    )
    candidate = candidate_factory(
        full_name="US-based candidate",
        location="New York, NY",
        current_company="American Bank",
        work_eligibility=None,
    )

    result = engine.evaluate(scorecard, candidate)

    assert result.classification == "near_match"
    assert _criterion("work_eligibility", result).state is EvidenceState.UNKNOWN
    assert _criterion("work_eligibility", result).evidence == ()


def test_source_text_only_work_eligibility_is_not_evaluated_as_a_skill(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    scorecard = scorecard_factory(
        criteria=[
            ScorecardCriterion(
                key="candidate_requirement",
                label="Candidate requirement",
                kind=CriterionKind.MUST_HAVE,
                evidence_required=True,
                source_text="Must be authorized to work in the United States",
            )
        ]
    )

    result = engine.evaluate(
        scorecard,
        candidate_factory(skills=("retail merchandising",), work_eligibility=None),
    )

    assert result.failed_must_haves == ()
    assert result.classification == "near_match"
    assert _criterion("candidate_requirement", result).state is EvidenceState.UNKNOWN


def test_equivalent_explicit_work_eligibility_phrases_match(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    scorecard = scorecard_factory(
        criteria=[
            ScorecardCriterion(
                key="employment_eligibility",
                label="Employment eligible in the United States",
                kind=CriterionKind.MUST_HAVE,
                source_text="Employment eligible in the United States",
            )
        ]
    )

    result = engine.evaluate(
        scorecard, candidate_factory(work_eligibility="US work authorized")
    )

    assert result.classification == "main"
    assert _criterion("employment_eligibility", result).state is EvidenceState.SUPPORTED


def test_source_text_work_eligibility_matches_explicit_candidate_fact(
    engine: MatchingEngine,
    scorecard_factory: Callable[..., ConfirmedScorecard],
    candidate_factory: Callable[..., CandidateProfile],
) -> None:
    scorecard = scorecard_factory(
        criteria=[
            ScorecardCriterion(
                key="candidate_requirement",
                label="Candidate requirement",
                kind=CriterionKind.MUST_HAVE,
                source_text="Must be authorized to work in the United States",
            )
        ]
    )

    result = engine.evaluate(
        scorecard, candidate_factory(work_eligibility="US work authorized")
    )

    assert result.classification == "main"
    assert _criterion("candidate_requirement", result).state is EvidenceState.SUPPORTED
