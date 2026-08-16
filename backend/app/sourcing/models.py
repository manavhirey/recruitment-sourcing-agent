from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.candidates import models as candidate_models  # noqa: F401
from app.core.database import Base
from app.sourcing.state_machine import RunState


def utc_now() -> datetime:
    return datetime.now(UTC)


run_state_type = Enum(
    RunState,
    name="sourcing_run_state",
    native_enum=False,
    values_callable=lambda enum: [member.value for member in enum],
)


_ACTIVE_STATE_SQL = (
    "state IN ('queued', 'sourcing', 'matching', 'enriching', 'partially_ready')"
)


class SourcingRun(Base):
    __tablename__ = "sourcing_runs"
    __table_args__ = (
        CheckConstraint(
            "candidate_count >= 0", name="ck_sourcing_runs_candidate_count"
        ),
        CheckConstraint("matched_count >= 0", name="ck_sourcing_runs_matched_count"),
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "job_id", "scorecard_version_id"],
            [
                "scorecard_versions.tenant_id",
                "scorecard_versions.job_id",
                "scorecard_versions.id",
            ],
            ondelete="RESTRICT",
        ),
        Index(
            "uq_sourcing_runs_active_scorecard",
            "tenant_id",
            "job_id",
            "scorecard_version_id",
            unique=True,
            postgresql_where=text(_ACTIVE_STATE_SQL),
            sqlite_where=text(_ACTIVE_STATE_SQL),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    scorecard_version_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    started_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[RunState] = mapped_column(
        run_state_type, default=RunState.QUEUED, nullable=False, index=True
    )
    planned_queries: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    current_stage: Mapped[str] = mapped_column(
        String(32), default=RunState.QUEUED.value, nullable=False
    )
    cancellation_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    cancellation_requested_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    candidate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class RunCheckpoint(Base):
    __tablename__ = "run_checkpoints"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "run_id", "idempotency_key"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="running", nullable=False)
    payload: Mapped[dict[str, object] | None] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunCandidate(Base):
    __tablename__ = "run_candidates"
    __table_args__ = (
        CheckConstraint(
            "match_score IS NULL OR (match_score >= 0 AND match_score <= 100)",
            name="ck_run_candidates_match_score",
        ),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "run_id", "candidate_id"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "scorecard_version_id"],
            ["scorecard_versions.tenant_id", "scorecard_versions.id"],
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    candidate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    scorecard_version_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    match_score: Mapped[int | None] = mapped_column(Integer)
    classification: Mapped[str | None] = mapped_column(String(32))
    evidence: Mapped[dict[str, object] | None] = mapped_column(JSON)
    scoring_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrichment_status: Mapped[str] = mapped_column(
        String(24), default="not_requested", nullable=False
    )
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EnrichmentRequest(Base):
    __tablename__ = "enrichment_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "run_id", "reservation_key"),
        ForeignKeyConstraint(
            ("tenant_id", "run_id"),
            ("sourcing_runs.tenant_id", "sourcing_runs.id"),
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('queued', 'submitting', 'pending', 'completed', 'failed', "
            "'cancelled')",
            name="ck_enrichment_requests_status",
        ),
        CheckConstraint(
            "synchronous_credits >= 0",
            name="ck_enrichment_requests_synchronous_credits",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(255), index=True)
    candidate_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    capability_token_hmac: Mapped[str | None] = mapped_column(String(64), index=True)
    reservation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    reveal_personal_emails: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    reveal_phone_number: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    stage_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    poll_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    synchronous_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usage_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookDelivery(Base):
    __tablename__ = "enrichment_webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "enrichment_request_id", "payload_hmac"),
        ForeignKeyConstraint(
            ("tenant_id", "enrichment_request_id"),
            ("enrichment_requests.tenant_id", "enrichment_requests.id"),
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "source IN ('webhook', 'poll', 'synchronous')",
            name="ck_webhook_deliveries_source",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrichment_request_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_hmac: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderSnapshot(Base):
    __tablename__ = "provider_snapshot_references"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "enrichment_request_id"),
        ForeignKeyConstraint(
            ("tenant_id", "run_id"),
            ("sourcing_runs.tenant_id", "sourcing_runs.id"),
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "enrichment_request_id"),
            ("enrichment_requests.tenant_id", "enrichment_requests.id"),
            ondelete="CASCADE",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    enrichment_request_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    object_reference: Mapped[str] = mapped_column(String(1024), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class UsageBudget(Base):
    __tablename__ = "usage_budgets"
    __table_args__ = (
        CheckConstraint(
            "max_search_pages IS NULL OR max_search_pages >= 0",
            name="ck_usage_budgets_search_pages",
        ),
        CheckConstraint(
            "max_enrichments IS NULL OR max_enrichments >= 0",
            name="ck_usage_budgets_enrichments",
        ),
        CheckConstraint(
            "max_estimated_credits IS NULL OR max_estimated_credits >= 0",
            name="ck_usage_budgets_estimated_credits",
        ),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "job_id"),
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.id"],
            ondelete="CASCADE",
        ),
        Index(
            "uq_usage_budgets_tenant_default",
            "tenant_id",
            unique=True,
            postgresql_where=text("job_id IS NULL"),
            sqlite_where=text("job_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[UUID | None] = mapped_column(index=True)
    max_search_pages: Mapped[int | None] = mapped_column(Integer)
    max_enrichments: Mapped[int | None] = mapped_column(Integer)
    max_estimated_credits: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class UsageLedger(Base):
    __tablename__ = "usage_ledger"
    __table_args__ = (
        CheckConstraint("requested_units > 0", name="ck_usage_ledger_requested_units"),
        CheckConstraint(
            "charged_units IS NULL OR charged_units >= 0",
            name="ck_usage_ledger_charged_units",
        ),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "run_id", "reservation_key", "unit_type"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    job_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(128), nullable=False)
    unit_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reservation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    requested_units: Mapped[int] = mapped_column(Integer, nullable=False)
    charged_units: Mapped[int | None] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TenantNotification(Base):
    __tablename__ = "tenant_notifications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "run_id", "audience_role", "code"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[UUID | None] = mapped_column(index=True)
    audience_role: Mapped[str] = mapped_column(String(32), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
