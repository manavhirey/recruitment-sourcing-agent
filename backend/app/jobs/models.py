from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("draft_revision >= 0", name="ck_jobs_draft_revision"),
        UniqueConstraint("tenant_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    client_id: Mapped[UUID] = mapped_column(
        ForeignKey("client_companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    job_description: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str | None] = mapped_column(String(255))
    employment_model: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(32), default="awaiting_scorecard", nullable=False
    )
    draft_payload: Mapped[dict[str, object] | None] = mapped_column(JSON)
    draft_revision: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    draft_extraction_status: Mapped[str] = mapped_column(
        String(32), default="ready", nullable=False
    )
    draft_extraction_warning: Mapped[str | None] = mapped_column(Text)
    current_scorecard_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "scorecard_versions.id",
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_jobs_current_scorecard_id",
        ),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ScorecardVersion(Base):
    __tablename__ = "scorecard_versions"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_scorecard_versions_version"),
        UniqueConstraint("job_id", "version"),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "job_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_titles: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    seniority: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    minimum_years: Mapped[int | None] = mapped_column(Integer)
    maximum_years: Mapped[int | None] = mapped_column(Integer)
    locations: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    industry_code: Mapped[str] = mapped_column(String(128), nullable=False)
    suggested_adjacent_industries: Mapped[list[str]] = mapped_column(
        JSON, nullable=False
    )
    uncertainties: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    extraction_status: Mapped[str] = mapped_column(String(32), nullable=False)
    confirmed_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ScorecardCriterionRecord(Base):
    __tablename__ = "scorecard_criteria"
    __table_args__ = (
        CheckConstraint("position >= 0", name="ck_scorecard_criteria_position"),
        UniqueConstraint("scorecard_version_id", "key"),
        UniqueConstraint("scorecard_version_id", "position"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scorecard_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("scorecard_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_required: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    source_text: Mapped[str | None] = mapped_column(String(500))
    inferred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recruiter_entered: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    lawful_requirement_confirmed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
