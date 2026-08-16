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
