from datetime import UTC, datetime
from uuid import uuid4

import pytest
from app.jobs.schemas import (
    ConfirmedScorecard,
    CriterionKind,
    ScorecardCriterion,
    ScorecardDraft,
)
from app.jobs.seniority import SeniorityLevel
from pydantic import ValidationError


def _valid_draft(**overrides: object) -> ScorecardDraft:
    values: dict[str, object] = {
        "target_titles": ["Product Designer"],
        "criteria": [
            ScorecardCriterion(
                key="product_design",
                label="Product design experience",
                kind=CriterionKind.MUST_HAVE,
            )
        ],
        "seniority": [],
        "minimum_years": None,
        "maximum_years": None,
        "locations": [],
        "industry_code": "technology.software",
        "suggested_adjacent_industries": [],
        "uncertainties": [],
    }
    values.update(overrides)
    return ScorecardDraft.model_validate(values)


def test_scorecard_draft_normalizes_known_seniority_aliases() -> None:
    draft = _valid_draft(seniority=["mid-level", "SENIOR", "senior"])

    assert draft.seniority == [SeniorityLevel.MID_LEVEL, SeniorityLevel.SENIOR]


def test_scorecard_draft_rejects_legacy_unknown_seniority() -> None:
    with pytest.raises(ValidationError, match="unknown seniority value"):
        _valid_draft(seniority=["manager"])


def test_confirmed_historical_scorecard_remains_readable_but_not_reusable() -> None:
    scorecard = ConfirmedScorecard.model_validate(
        {
            **_valid_draft().model_dump(mode="json"),
            "id": uuid4(),
            "job_id": uuid4(),
            "version": 1,
            "confirmed_at": datetime(2026, 8, 23, tzinfo=UTC),
            "extraction_status": "ready",
            "seniority": ["manager"],
        }
    )

    assert scorecard.seniority == ["manager"]
    with pytest.raises(ValidationError, match="unknown seniority value"):
        scorecard.to_draft()


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


def test_scorecard_tracks_every_inferred_item_with_stable_confirmation_ids() -> None:
    draft = ScorecardDraft(
        target_titles=["Product Manager"],
        criteria=[
            ScorecardCriterion(
                key="growth",
                label="Led product-led growth",
                kind=CriterionKind.PREFERENCE,
                inferred=True,
            )
        ],
        seniority=[],
        locations=[],
        industry_code="technology.fintech",
        suggested_adjacent_industries=["financial_services.banking"],
        uncertainties=["Confirm ownership of go-to-market strategy"],
    )

    confirmation_ids = draft.inferred_item_ids()
    assert len(confirmation_ids) == 3
    assert {item.split(":", 1)[0] for item in confirmation_ids} == {
        "criterion",
        "adjacent",
        "uncertainty",
    }
    assert draft.unresolved_inferred_items() == draft.inferred_item_ids()

    edited = draft.model_copy(deep=True)
    edited.criteria[0].label = "Owned product-led growth"
    assert edited.inferred_item_ids() != confirmation_ids


def test_scorecard_rejects_stale_and_unknown_inference_confirmations() -> None:
    draft = ScorecardDraft(
        target_titles=["Product Manager"],
        criteria=[
            ScorecardCriterion(
                key="growth",
                label="Led product-led growth",
                kind=CriterionKind.PREFERENCE,
                inferred=True,
            )
        ],
        seniority=[],
        locations=[],
        industry_code="technology.fintech",
        suggested_adjacent_industries=[],
        uncertainties=[],
    )
    approved = sorted(draft.inferred_item_ids())

    with pytest.raises(ValidationError, match="unknown inferred item confirmation"):
        ScorecardDraft.model_validate(
            {**draft.model_dump(), "confirmed_inferred_items": ["criterion:unknown"]}
        )

    with pytest.raises(ValidationError, match="unknown inferred item confirmation"):
        ScorecardDraft.model_validate(
            {
                **draft.model_dump(),
                "criteria": [
                    {
                        **draft.criteria[0].model_dump(),
                        "label": "Owned product-led growth",
                    }
                ],
                "confirmed_inferred_items": approved,
            }
        )
