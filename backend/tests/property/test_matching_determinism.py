from datetime import UTC, datetime
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from app.candidates.schemas import CandidateExperienceProfile, CandidateProfile
from app.jobs.schemas import (
    ConfirmedScorecard,
    CriterionKind,
    ExtractionStatus,
    ScorecardCriterion,
)
from app.matching.engine import MatchingEngine
from app.matching.explanations import format_explanation

SCORECARD_ID = UUID("00000000-0000-0000-0000-000000000001")
JOB_ID = UUID("00000000-0000-0000-0000-000000000002")
CANDIDATE_ID = UUID("00000000-0000-0000-0000-000000000003")
TENANT_ID = UUID("00000000-0000-0000-0000-000000000004")
NOW = datetime(2026, 8, 15, tzinfo=UTC)


def _criterion(key: str, kind: CriterionKind) -> ScorecardCriterion:
    labels = {
        "payments": "Payments platform experience",
        "sql": "SQL analytics experience",
        "work_eligibility": "Authorized to work in the United States",
    }
    return ScorecardCriterion(
        key=key,
        label=labels[key],
        kind=kind,
        source_text=labels[key] if key == "work_eligibility" else None,
    )


def _scorecard(criteria: list[ScorecardCriterion]) -> ConfirmedScorecard:
    return ConfirmedScorecard(
        id=SCORECARD_ID,
        job_id=JOB_ID,
        version=1,
        confirmed_at=NOW,
        extraction_status=ExtractionStatus.READY,
        target_titles=["Product Manager", "Senior Product Manager"],
        criteria=criteria,
        seniority=["senior", "director"],
        minimum_years=5,
        maximum_years=12,
        locations=["New York, NY", "Remote"],
        industry_code="financial_services.banking",
        suggested_adjacent_industries=["technology.fintech"],
        uncertainties=[],
    )


def _candidate(
    *,
    skills: tuple[str, ...],
    industry_codes: tuple[str, ...],
    experiences: tuple[CandidateExperienceProfile, ...],
    title: str | None = "Sr Product Manager",
    location: str | None = "NYC",
    seniority: str | None = "senior",
    years: float | None = 8.0,
    work_eligibility: str | None = "US work authorized",
) -> CandidateProfile:
    return CandidateProfile(
        id=CANDIDATE_ID,
        tenant_id=TENANT_ID,
        full_name="Priya Sharma",
        current_title=title,
        current_company="Acme",
        location=location,
        profile_url=None,
        created_at=NOW,
        updated_at=NOW,
        skills=skills,
        industry_codes=industry_codes,
        seniority=seniority,
        years_experience=years,
        work_eligibility=work_eligibility,
        experiences=experiences,
    )


EXPERIENCES = (
    CandidateExperienceProfile(
        title="Product Manager",
        company_name="Acme",
        start_date="2018-01",
        end_date="2021-01",
    ),
    CandidateExperienceProfile(
        title="Senior Product Manager",
        company_name="Acme",
        start_date="2021-02",
        end_date="present",
    ),
)


@given(
    skills=st.permutations(("Payment Processing", "SQL")),
    industries=st.permutations(("technology.fintech", "financial_services.banking")),
    experiences=st.permutations(EXPERIENCES),
    criterion_order=st.permutations(
        (
            _criterion("payments", CriterionKind.MUST_HAVE),
            _criterion("sql", CriterionKind.PREFERENCE),
            _criterion("work_eligibility", CriterionKind.MUST_HAVE),
        )
    ),
)
def test_reordered_equivalent_inputs_produce_identical_results(
    skills: list[str],
    industries: list[str],
    experiences: list[CandidateExperienceProfile],
    criterion_order: list[ScorecardCriterion],
) -> None:
    engine = MatchingEngine()
    baseline_scorecard = _scorecard(
        [
            _criterion("payments", CriterionKind.MUST_HAVE),
            _criterion("sql", CriterionKind.PREFERENCE),
            _criterion("work_eligibility", CriterionKind.MUST_HAVE),
        ]
    )
    baseline_candidate = _candidate(
        skills=("Payment Processing", "SQL"),
        industry_codes=("technology.fintech", "financial_services.banking"),
        experiences=EXPERIENCES,
    )

    expected = engine.evaluate(baseline_scorecard, baseline_candidate)
    actual = engine.evaluate(
        _scorecard(list(criterion_order)),
        _candidate(
            skills=tuple(skills),
            industry_codes=tuple(industries),
            experiences=tuple(experiences),
        ),
    )

    assert actual == expected
    assert format_explanation(actual) == format_explanation(expected)


TIED_EXPERIENCES = (
    CandidateExperienceProfile(
        title="Senior Product Manager",
        company_name="Acme",
        start_date="2021-02",
        end_date="Present",
    ),
    CandidateExperienceProfile(
        title="senior product manager",
        company_name="Acme",
        start_date="2021-02",
        end_date="present",
    ),
)


@given(
    skills=st.permutations(("SQL", "sql")),
    experiences=st.permutations(TIED_EXPERIENCES),
)
def test_normalized_duplicate_spellings_and_tied_dates_are_deterministic(
    skills: list[str], experiences: list[CandidateExperienceProfile]
) -> None:
    engine = MatchingEngine()
    scorecard = _scorecard([_criterion("sql", CriterionKind.MUST_HAVE)])
    baseline = engine.evaluate(
        scorecard,
        _candidate(
            skills=("SQL", "sql"),
            industry_codes=("financial_services.banking",),
            experiences=TIED_EXPERIENCES,
        ),
    )

    actual = engine.evaluate(
        scorecard,
        _candidate(
            skills=tuple(skills),
            industry_codes=("financial_services.banking",),
            experiences=tuple(experiences),
        ),
    )

    assert actual == baseline
    assert format_explanation(actual) == format_explanation(baseline)


@given(
    title=st.one_of(st.none(), st.sampled_from(["Product Manager", "Engineer"])),
    location=st.one_of(st.none(), st.sampled_from(["NYC", "London"])),
    skills=st.lists(
        st.sampled_from(["payments", "sql", "retail merchandising"]),
        unique=True,
        max_size=3,
    ),
    industries=st.lists(
        st.sampled_from(["financial_services.banking", "technology.fintech", "retail"]),
        unique=True,
        max_size=3,
    ),
    seniority=st.one_of(st.none(), st.sampled_from(["senior", "entry"])),
    years=st.one_of(st.none(), st.floats(min_value=0, max_value=50)),
    eligibility=st.one_of(
        st.none(),
        st.sampled_from(
            ["US work authorized", "Requires sponsorship in the United States"]
        ),
    ),
)
def test_scores_remain_bounded_and_equal_the_fixed_component_sum(
    title: str | None,
    location: str | None,
    skills: list[str],
    industries: list[str],
    seniority: str | None,
    years: float | None,
    eligibility: str | None,
) -> None:
    result = MatchingEngine().evaluate(
        _scorecard(
            [
                _criterion("payments", CriterionKind.MUST_HAVE),
                _criterion("work_eligibility", CriterionKind.MUST_HAVE),
            ]
        ),
        _candidate(
            skills=tuple(skills),
            industry_codes=tuple(industries),
            experiences=EXPERIENCES,
            title=title,
            location=location,
            seniority=seniority,
            years=years,
            work_eligibility=eligibility,
        ),
    )

    assert 0 <= result.total <= 100
    assert result.total == sum(result.breakdown.model_dump().values())
    assert 0 <= result.breakdown.role_and_skills <= 35
    assert 0 <= result.breakdown.scope_seniority_years <= 25
    assert 0 <= result.breakdown.industry <= 20
    assert 0 <= result.breakdown.location_and_eligibility <= 10
    assert 0 <= result.breakdown.recency_and_trajectory <= 10
