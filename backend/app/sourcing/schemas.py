from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.sourcing.state_machine import RunState


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    job_id: UUID
    scorecard_version_id: UUID
    state: RunState
    current_stage: str
    candidate_count: int
    matched_count: int
    enriched_count: int
    failed_count: int
    cancellation_requested: bool
    budget_use: dict[str, int]
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


class RunActivityResponse(BaseModel):
    id: UUID
    action: str
    summary: str | None
    created_at: datetime


class NotificationResponse(BaseModel):
    id: UUID
    run_id: UUID | None
    code: str
    title: str
    message: str
    acknowledged_at: datetime | None
    created_at: datetime


class EnrichmentRequestResponse(BaseModel):
    id: UUID
    run_id: UUID
    status: str
