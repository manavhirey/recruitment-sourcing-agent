from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CandidateExperienceProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    title: str | None
    company_name: str | None
    start_date: str | None
    end_date: str | None


class CandidateProfile(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    full_name: str
    current_title: str | None
    current_company: str | None
    location: str | None
    profile_url: str | None
    created_at: datetime
    updated_at: datetime
    experiences: tuple[CandidateExperienceProfile, ...] = ()
    skills: tuple[str, ...] = ()
    industry_codes: tuple[str, ...] = ()
    seniority: str | None = None
    years_experience: float | None = None
    work_eligibility: str | None = None


@dataclass(frozen=True)
class ResolutionDecision:
    candidate_id: UUID | None
    matched_by: str | None
    fuzzy_candidate_ids: tuple[UUID, ...] = ()

    @classmethod
    def reuse(cls, candidate_id: UUID, matched_by: str) -> "ResolutionDecision":
        return cls(candidate_id=candidate_id, matched_by=matched_by)

    @classmethod
    def create_with_suggestions(
        cls, candidate_ids: tuple[UUID, ...]
    ) -> "ResolutionDecision":
        return cls(
            candidate_id=None,
            matched_by=None,
            fuzzy_candidate_ids=candidate_ids,
        )


@dataclass(frozen=True)
class ResolutionResult:
    candidate_id: UUID | None
    source_identity_id: UUID | None
    created: bool
    matched_by: str | None
    duplicate_suggestion_id: UUID | None = None
    suppressed: bool = False

    @classmethod
    def suppressed_result(cls) -> "ResolutionResult":
        return cls(
            candidate_id=None,
            source_identity_id=None,
            created=False,
            matched_by=None,
            suppressed=True,
        )
