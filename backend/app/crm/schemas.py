from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.crm.models import CandidateStage


class StageUpdate(BaseModel):
    stage: CandidateStage
    reason_code: str | None = None
    note: str | None = Field(default=None, max_length=2000)


class NoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=5000)


class OwnerUpdate(BaseModel):
    owner_user_id: UUID | None


class TagsUpdate(BaseModel):
    tags: list[str] = Field(max_length=20)


class CandidateFilter(BaseModel):
    classification: str = "main"
    sort: str = "-score"
    score_min: int | None = Field(default=None, ge=0, le=100)
    score_max: int | None = Field(default=None, ge=0, le=100)
    stage: CandidateStage | None = None
    owner_user_id: UUID | None = None
    tags: tuple[str, ...] = ()
    location: str | None = None
    industry: str | None = None
    has_contact: bool | None = None
    query: str | None = None


class MaskedContact(BaseModel):
    id: UUID
    kind: str
    classification: str
    verification_state: str
    masked_value: str
    expires_at: datetime


class CandidateExperienceView(BaseModel):
    title: str | None
    company_name: str | None
    start_date: str | None
    end_date: str | None
    provider: str
    source_timestamp: datetime


class CandidateProvenanceView(BaseModel):
    field_name: str
    provider: str
    source_timestamp: datetime


class MandatoryGapView(BaseModel):
    key: str
    label: str
    state: Literal["failed", "unknown"]
    summary: str


class JobCandidateView(BaseModel):
    id: UUID
    job_id: UUID
    candidate_id: UUID
    run_candidate_id: UUID | None = None
    full_name: str
    current_title: str | None
    current_company: str | None
    location: str | None
    classification: str
    score: int
    score_json: dict[str, object] | None = None
    mandatory_gaps: list[MandatoryGapView] = Field(default_factory=list)
    scorecard_version_id: UUID
    scorecard_version: int | None = None
    scoring_version: str
    stage: CandidateStage
    owner_user_id: UUID | None
    rejection_reason_code: str | None
    rejection_note: str | None
    tags: list[str]
    has_contact: bool
    enrichment_eligible: bool = False
    estimated_enrichment_credits: int | None = Field(default=None, ge=1)
    contacts: list[MaskedContact] | None = None
    experiences: list[CandidateExperienceView] | None = None
    provenance: list[CandidateProvenanceView] | None = None
    notes: list["NoteResponse"] | None = None
    created_at: datetime
    updated_at: datetime


class JobCandidatePage(BaseModel):
    items: list[JobCandidateView]
    next_cursor: str | None


class NoteResponse(BaseModel):
    id: UUID
    job_candidate_id: UUID
    actor_user_id: UUID
    body: str
    created_at: datetime
    updated_at: datetime


class TagsResponse(BaseModel):
    job_candidate_id: UUID
    tags: list[str]


class ActivityResponse(BaseModel):
    id: UUID
    action: str
    created_at: datetime


class ActivityPage(BaseModel):
    items: list[ActivityResponse]
    next_cursor: str | None


class AcceptanceResponse(BaseModel):
    denominator: int
    accepted: int
    reviewed: int
    shortlisted: int
    new: int
    rejected: int
    rate: float
    ready_at: datetime
    final_at: datetime
    final: bool


class ContactRevealResponse(BaseModel):
    id: UUID
    value: str


class CandidateDirectoryItem(BaseModel):
    id: UUID
    name: str
    current_title: str | None
    current_company: str | None
    location: str | None
    industry_codes: list[str]
    job_ids: list[UUID]
    updated_at: datetime


class CandidateDirectoryPage(BaseModel):
    items: list[CandidateDirectoryItem]
    next_cursor: str | None


class CandidateJobView(BaseModel):
    job_candidate_id: UUID
    job_id: UUID
    job_title: str
    classification: str
    score: int
    stage: CandidateStage
    updated_at: datetime
