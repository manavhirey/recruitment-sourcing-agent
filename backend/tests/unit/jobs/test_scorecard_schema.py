import pytest
from pydantic import ValidationError

from app.jobs.schemas import CriterionKind, ScorecardCriterion


def test_protected_class_exclusion_is_rejected() -> None:
    with pytest.raises(ValidationError, match="protected characteristic"):
        ScorecardCriterion(
            key="gender",
            label="Male candidates",
            kind=CriterionKind.EXCLUSION,
            evidence_required=True,
        )


def test_inferred_work_authorization_is_rejected() -> None:
    with pytest.raises(ValidationError, match="work authorization"):
        ScorecardCriterion(
            key="work_authorization",
            label="Authorized to work in the United States",
            kind=CriterionKind.MUST_HAVE,
            inferred=True,
        )


def test_unstated_exclusion_requires_recruiter_lawfulness_confirmation() -> None:
    with pytest.raises(ValidationError, match="lawful requirement"):
        ScorecardCriterion(
            key="onsite_attendance",
            label="Cannot work onsite",
            kind=CriterionKind.EXCLUSION,
        )

    criterion = ScorecardCriterion(
        key="onsite_attendance",
        label="Cannot work onsite",
        kind=CriterionKind.EXCLUSION,
        recruiter_entered=True,
        lawful_requirement_confirmed=True,
    )

    assert criterion.source_text is None


def test_protected_term_substrings_do_not_reject_job_related_criteria() -> None:
    criterion = ScorecardCriterion(
        key="management_experience",
        label="Product management experience",
        kind=CriterionKind.PREFERENCE,
        source_text="product management experience",
    )

    assert criterion.key == "management_experience"


@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("preferred_background", "Black candidates"),
        ("preferred_faith", "Hindu candidates"),
        ("community_background", "Dalit applicants"),
        ("language_background", "Native English speakers only"),
        ("team_energy", "Young and energetic candidates"),
        ("family_status", "Unmarried candidates"),
        ("physical_profile", "Able-bodied professionals"),
    ],
)
def test_us_and_india_protected_values_synonyms_and_proxies_are_rejected(
    key: str, label: str
) -> None:
    with pytest.raises(ValidationError, match="protected characteristic"):
        ScorecardCriterion(
            key=key,
            label=label,
            kind=CriterionKind.PREFERENCE,
            source_text=label,
        )


@pytest.mark.parametrize(
    "label",
    [
        "Employment eligible in the United States",
        "Must be legally employable in India",
        "No visa sponsorship needed",
        "Green card holder",
        "United States citizen",
        "Has an employment authorization document",
    ],
)
def test_inferred_work_authorization_synonyms_are_rejected(label: str) -> None:
    with pytest.raises(ValidationError, match="work authorization"):
        ScorecardCriterion(
            key="employment_eligibility",
            label=label,
            kind=CriterionKind.MUST_HAVE,
            inferred=True,
        )


@pytest.mark.parametrize("source_text", ["", "   ", "\t\n"])
def test_blank_source_text_cannot_bypass_unstated_exclusion_guard(
    source_text: str,
) -> None:
    with pytest.raises(ValidationError, match="lawful requirement"):
        ScorecardCriterion(
            key="onsite_attendance",
            label="Cannot work onsite",
            kind=CriterionKind.EXCLUSION,
            source_text=source_text,
        )

    criterion = ScorecardCriterion(
        key="onsite_attendance",
        label="Cannot work onsite",
        kind=CriterionKind.EXCLUSION,
        source_text=source_text,
        recruiter_entered=True,
        lawful_requirement_confirmed=True,
    )
    assert criterion.source_text is None
