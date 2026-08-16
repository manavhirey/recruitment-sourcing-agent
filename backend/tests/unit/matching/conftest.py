from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.candidates.schemas import CandidateExperienceProfile, CandidateProfile
from app.jobs.schemas import (
    ConfirmedScorecard,
    CriterionKind,
    ExtractionStatus,
    ScorecardCriterion,
)
from app.matching.engine import MatchingEngine


@pytest.fixture
def scorecard_factory() -> Callable[..., ConfirmedScorecard]:
    def factory(**overrides: object) -> ConfirmedScorecard:
        values: dict[str, object] = {
            "id": uuid4(),
            "job_id": uuid4(),
            "version": 1,
            "confirmed_at": datetime(2026, 8, 15, tzinfo=UTC),
            "extraction_status": ExtractionStatus.READY,
            "target_titles": ["Senior Product Manager"],
            "criteria": [
                ScorecardCriterion(
                    key="payments",
                    label="Payments platform experience",
                    kind=CriterionKind.MUST_HAVE,
                )
            ],
            "seniority": ["senior"],
            "minimum_years": 5,
            "maximum_years": 10,
            "locations": ["New York, NY"],
            "industry_code": "financial_services.banking",
            "suggested_adjacent_industries": ["technology.fintech"],
            "uncertainties": [],
        }
        values.update(overrides)
        return ConfirmedScorecard.model_validate(values)

    return factory


@pytest.fixture
def candidate_factory() -> Callable[..., CandidateProfile]:
    def factory(**overrides: object) -> CandidateProfile:
        values: dict[str, object] = {
            "id": uuid4(),
            "tenant_id": uuid4(),
            "full_name": "Priya Sharma",
            "current_title": "Senior Product Manager",
            "current_company": "Acme",
            "location": "New York, NY",
            "profile_url": "https://www.linkedin.com/in/priya",
            "created_at": datetime(2026, 8, 1, tzinfo=UTC),
            "updated_at": datetime(2026, 8, 15, tzinfo=UTC),
            "skills": ("payments",),
            "industry_codes": ("financial_services.banking",),
            "seniority": "senior",
            "years_experience": 8.0,
            "work_eligibility": None,
            "experiences": (
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
            ),
        }
        values.update(overrides)
        return CandidateProfile.model_validate(values)

    return factory


@pytest.fixture
def engine() -> MatchingEngine:
    return MatchingEngine()
