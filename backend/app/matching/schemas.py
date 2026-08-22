from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceState(StrEnum):
    SUPPORTED = "supported"
    FAILED = "failed"
    UNKNOWN = "unknown"


class CriterionEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    state: EvidenceState
    summary: str
    points: int = Field(ge=0, le=100)
    max_points: int = Field(ge=0, le=100)
    evidence: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def points_do_not_exceed_maximum(self) -> Self:
        if self.points > self.max_points:
            raise ValueError("criterion points cannot exceed its maximum")
        return self


class ScoreBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True)

    role_and_skills: int = Field(ge=0, le=35)
    scope_seniority_years: int = Field(ge=0, le=25)
    industry: int = Field(ge=0, le=20)
    location_and_eligibility: int = Field(ge=0, le=10)
    recency_and_trajectory: int = Field(ge=0, le=10)


class MatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    classification: Literal["main", "near_match"]
    total: int = Field(ge=0, le=100)
    breakdown: ScoreBreakdown
    criteria: tuple[CriterionEvaluation, ...]
    failed_must_haves: tuple[str, ...]
    unknown_keys: tuple[str, ...]
    scoring_version: str = "matching-v1"

    @model_validator(mode="after")
    def total_is_component_sum(self) -> Self:
        component_sum = sum(self.breakdown.model_dump().values())
        if self.total != component_sum:
            raise ValueError("match total must equal the component score sum")
        return self


class MatchExplanation(BaseModel):
    model_config = ConfigDict(frozen=True)

    supported: tuple[str, ...]
    failed: tuple[str, ...]
    unknown: tuple[str, ...]
