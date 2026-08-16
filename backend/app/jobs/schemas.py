from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.jobs.legal_policy import DEFAULT_SCORECARD_LEGAL_POLICY


class CriterionKind(StrEnum):
    MUST_HAVE = "must_have"
    PREFERENCE = "preference"
    EXCLUSION = "exclusion"


def _criterion_text(criterion: "ScorecardCriterion") -> str:
    return " ".join((criterion.key, criterion.label, criterion.source_text or ""))


class ScorecardCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    label: str = Field(min_length=3, max_length=160)
    kind: CriterionKind
    evidence_required: bool = False
    source_text: str | None = Field(default=None, max_length=500)
    inferred: bool = False
    recruiter_entered: bool = False
    lawful_requirement_confirmed: bool = False

    @field_validator("source_text", mode="before")
    @classmethod
    def normalize_source_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def enforce_lawful_criterion(self) -> "ScorecardCriterion":
        criterion_text = _criterion_text(self)
        if DEFAULT_SCORECARD_LEGAL_POLICY.protected_characteristic_match(
            criterion_text
        ):
            raise ValueError("criterion refers to a protected characteristic")
        if (
            self.inferred
            and DEFAULT_SCORECARD_LEGAL_POLICY.refers_to_work_authorization(
                criterion_text
            )
        ):
            raise ValueError("work authorization cannot be inferred")
        if (
            self.kind is CriterionKind.EXCLUSION
            and self.source_text is None
            and not (self.recruiter_entered and self.lawful_requirement_confirmed)
        ):
            raise ValueError(
                "an unstated exclusion requires recruiter entry and lawful requirement "
                "confirmation"
            )
        return self


class ScorecardDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_titles: list[str] = Field(min_length=1, max_length=12)
    criteria: list[ScorecardCriterion] = Field(min_length=1, max_length=40)
    seniority: list[str] = Field(max_length=8)
    minimum_years: int | None = Field(default=None, ge=0, le=50)
    maximum_years: int | None = Field(default=None, ge=0, le=50)
    locations: list[str] = Field(max_length=20)
    industry_code: str = Field(min_length=1, max_length=128)
    suggested_adjacent_industries: list[str] = Field(max_length=12)
    uncertainties: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_scorecard(self) -> "ScorecardDraft":
        if (
            self.minimum_years is not None
            and self.maximum_years is not None
            and self.minimum_years > self.maximum_years
        ):
            raise ValueError("minimum_years cannot exceed maximum_years")
        criterion_keys = [criterion.key for criterion in self.criteria]
        if len(set(criterion_keys)) != len(criterion_keys):
            raise ValueError("scorecard criterion keys must be unique")
        return self


class ExtractionStatus(StrEnum):
    READY = "ready"
    MANUAL_REQUIRED = "manual_required"


class ClientContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    client_id: UUID
    industry_codes: tuple[str, ...]
    approved_adjacent_industries: tuple[str, ...]


class ConfirmedScorecard(ScorecardDraft):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    job_id: UUID
    version: int = Field(ge=1)
    confirmed_at: datetime
    extraction_status: ExtractionStatus

    def to_draft(self) -> ScorecardDraft:
        return ScorecardDraft.model_validate(
            self.model_dump(
                exclude={
                    "id",
                    "job_id",
                    "version",
                    "confirmed_at",
                    "extraction_status",
                }
            )
        )


class JobCreate(BaseModel):
    client_id: UUID
    title: str = Field(min_length=1, max_length=255)
    job_description: str = Field(min_length=1, max_length=50_000)
    location: str | None = Field(default=None, max_length=255)
    employment_model: str | None = Field(default=None, max_length=64)


class JobResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    client_id: UUID
    owner_user_id: UUID
    title: str
    job_description: str
    location: str | None
    employment_model: str | None
    status: str
    draft_revision: int
    extraction_status: ExtractionStatus
    extraction_warning: str | None
    current_scorecard_id: UUID | None
    created_at: datetime
    updated_at: datetime


class ScorecardGenerationRequest(BaseModel):
    expected_revision: int = Field(ge=0)


class ScorecardDraftUpdate(BaseModel):
    expected_revision: int = Field(ge=0)
    draft: ScorecardDraft


class ScorecardConfirmation(BaseModel):
    expected_revision: int = Field(ge=0)


class ScorecardRevisionRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    draft: ScorecardDraft


class EditableScorecardDraft(BaseModel):
    target_titles: list[str] = Field(default_factory=list, max_length=12)
    criteria: list[ScorecardCriterion] = Field(default_factory=list, max_length=40)
    seniority: list[str] = Field(default_factory=list, max_length=8)
    minimum_years: int | None = Field(default=None, ge=0, le=50)
    maximum_years: int | None = Field(default=None, ge=0, le=50)
    locations: list[str] = Field(default_factory=list, max_length=20)
    industry_code: str = Field(default="", max_length=128)
    suggested_adjacent_industries: list[str] = Field(
        default_factory=list, max_length=12
    )
    uncertainties: list[str] = Field(default_factory=list, max_length=20)


class ScorecardDraftResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: UUID
    draft_revision: int
    draft: ScorecardDraft | EditableScorecardDraft
    original_job_description: str
    extraction_status: ExtractionStatus
    extraction_warning: str | None
