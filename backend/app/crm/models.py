from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.sourcing import models as sourcing_models  # noqa: F401


def utc_now() -> datetime:
    return datetime.now(UTC)


class CandidateStage(StrEnum):
    NEW = "New"
    REVIEWED = "Reviewed"
    SHORTLISTED = "Shortlisted"
    REJECTED = "Rejected"


candidate_stage_type = Enum(
    CandidateStage,
    name="candidate_stage",
    native_enum=False,
    length=16,
    values_callable=lambda enum: [member.value for member in enum],
)


class JobCandidate(Base):
    __tablename__ = "job_candidates"
    __table_args__ = (
        CheckConstraint("score >= 0 AND score <= 100", name="ck_job_candidates_score"),
        CheckConstraint(
            "stage IN ('New', 'Reviewed', 'Shortlisted', 'Rejected')",
            name="ck_job_candidates_stage",
        ),
        CheckConstraint(
            "classification IN ('main', 'near_match')",
            name="ck_job_candidates_classification",
        ),
        CheckConstraint(
            "rejection_reason_code IS NULL OR rejection_reason_code IN "
            "('not_qualified', 'compensation_mismatch', 'location_mismatch', "
            "'work_authorization', 'duplicate', 'other')",
            name="ck_job_candidates_rejection_reason",
        ),
        CheckConstraint(
            "(stage = 'Rejected' AND rejection_reason_code IS NOT NULL) OR "
            "(stage <> 'Rejected' AND rejection_reason_code IS NULL AND "
            "rejection_note IS NULL)",
            name="ck_job_candidates_rejection_state",
        ),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "job_id", "candidate_id"),
        ForeignKeyConstraint(
            ("tenant_id", "job_id"),
            ("jobs.tenant_id", "jobs.id"),
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "candidate_id"),
            ("candidates.tenant_id", "candidates.id"),
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "scorecard_version_id"),
            ("scorecard_versions.tenant_id", "scorecard_versions.id"),
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "latest_run_id"),
            ("sourcing_runs.tenant_id", "sourcing_runs.id"),
            ondelete="RESTRICT",
        ),
        Index("ix_job_candidates_rank", "tenant_id", "job_id", "score", "id"),
        Index("ix_job_candidates_directory", "tenant_id", "updated_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    candidate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    latest_run_id: Mapped[UUID | None] = mapped_column(index=True)
    classification: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    score_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    scorecard_version_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    scoring_version: Mapped[str] = mapped_column(String(64), nullable=False)
    stage: Mapped[CandidateStage] = mapped_column(
        candidate_stage_type, nullable=False, default=CandidateStage.NEW, index=True
    )
    owner_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    rejection_reason_code: Mapped[str | None] = mapped_column(String(64))
    rejection_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class AcceptanceSnapshot(Base):
    __tablename__ = "crm_acceptance_snapshots"
    __table_args__ = (
        CheckConstraint(
            "denominator = 20", name="ck_crm_acceptance_snapshots_denominator"
        ),
        CheckConstraint(
            "accepted_count = reviewed_count + shortlisted_count",
            name="ck_crm_acceptance_snapshots_accepted",
        ),
        CheckConstraint(
            "accepted_count >= 0 AND reviewed_count >= 0 AND "
            "shortlisted_count >= 0 AND new_count >= 0 AND rejected_count >= 0",
            name="ck_crm_acceptance_snapshots_counts",
        ),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "job_id", "run_id"),
        ForeignKeyConstraint(
            ("tenant_id", "job_id"),
            ("jobs.tenant_id", "jobs.id"),
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "run_id"),
            ("sourcing_runs.tenant_id", "sourcing_runs.id"),
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    finalized_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    ready_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finalized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    denominator: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    shortlisted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    new_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cohort_candidate_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CandidateNote(Base):
    __tablename__ = "candidate_notes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ("tenant_id", "job_candidate_id"),
            ("job_candidates.tenant_id", "job_candidates.id"),
            ondelete="CASCADE",
        ),
        Index(
            "ix_candidate_notes_activity",
            "tenant_id",
            "job_candidate_id",
            "updated_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_candidate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Tag(Base):
    __tablename__ = "crm_tags"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "normalized_name"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class JobCandidateTag(Base):
    __tablename__ = "job_candidate_tags"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "job_candidate_id", "tag_id"),
        ForeignKeyConstraint(
            ("tenant_id", "job_candidate_id"),
            ("job_candidates.tenant_id", "job_candidates.id"),
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "tag_id"),
            ("crm_tags.tenant_id", "crm_tags.id"),
            ondelete="CASCADE",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_candidate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    tag_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ActivityEvent(Base):
    __tablename__ = "crm_activity_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "event_key"),
        ForeignKeyConstraint(
            ("tenant_id", "job_candidate_id"),
            ("job_candidates.tenant_id", "job_candidates.id"),
            ondelete="CASCADE",
        ),
        Index(
            "ix_crm_activity_cursor",
            "tenant_id",
            "job_candidate_id",
            "updated_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_candidate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    actor_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    event_key: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
