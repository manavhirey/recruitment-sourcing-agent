from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.privacy.schemas import PrivacyRequestState, PrivacyRequestType


def utc_now() -> datetime:
    return datetime.now(UTC)


privacy_request_type = Enum(
    PrivacyRequestType,
    name="privacy_request_type",
    native_enum=False,
    length=16,
    values_callable=lambda enum: [member.value for member in enum],
)
privacy_request_state = Enum(
    PrivacyRequestState,
    name="privacy_request_state",
    native_enum=False,
    length=40,
    values_callable=lambda enum: [member.value for member in enum],
)


class PrivacyRequest(Base):
    __tablename__ = "privacy_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ("tenant_id", "candidate_id"),
            ("candidates.tenant_id", "candidates.id"),
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "rejection_reason_code IS NULL OR state = 'Rejected'",
            name="ck_privacy_requests_rejection_state",
        ),
        CheckConstraint(
            "request_type IN ('Access', 'Correction', 'Deletion', 'Opt Out')",
            name="ck_privacy_requests_type",
        ),
        CheckConstraint(
            "state IN ('Received', 'Identity Verification Required', 'Approved', "
            "'Executing', 'Completed', 'Rejected')",
            name="ck_privacy_requests_state",
        ),
        Index(
            "uq_privacy_requests_active_candidate_type",
            "tenant_id",
            "candidate_id",
            "request_type",
            unique=True,
            postgresql_where=text("state NOT IN ('Completed', 'Rejected')"),
            sqlite_where=text("state NOT IN ('Completed', 'Rejected')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    request_type: Mapped[PrivacyRequestType] = mapped_column(
        privacy_request_type, nullable=False, index=True
    )
    state: Mapped[PrivacyRequestState] = mapped_column(
        privacy_request_state,
        nullable=False,
        default=PrivacyRequestState.IDENTITY_VERIFICATION_REQUIRED,
        index=True,
    )
    submitted_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    identity_verified_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    identity_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    approved_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class PrivacyRequestCheckpoint(Base):
    __tablename__ = "privacy_request_checkpoints"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "privacy_request_id", "name"),
        ForeignKeyConstraint(
            ("tenant_id", "privacy_request_id"),
            ("privacy_requests.tenant_id", "privacy_requests.id"),
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('pending', 'completed', 'failed')",
            name="ck_privacy_request_checkpoints_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_privacy_checkpoint_attempts"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    privacy_request_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SuppressionIdentifier(Base):
    __tablename__ = "suppression_identifiers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "identifier_type", "key_version", "digest"),
        ForeignKeyConstraint(
            ("tenant_id", "privacy_request_id"),
            ("privacy_requests.tenant_id", "privacy_requests.id"),
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "octet_length(digest) = 32", name="ck_suppression_identifiers_digest"
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    privacy_request_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    identifier_type: Mapped[str] = mapped_column(String(96), nullable=False)
    key_version: Mapped[str] = mapped_column(String(32), nullable=False)
    digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class PrivacyDeletionSnapshotTarget(Base):
    __tablename__ = "privacy_deletion_snapshot_targets"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "privacy_request_id", "snapshot_id"),
        ForeignKeyConstraint(
            ("tenant_id", "privacy_request_id"),
            ("privacy_requests.tenant_id", "privacy_requests.id"),
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "snapshot_id"),
            (
                "provider_snapshot_references.tenant_id",
                "provider_snapshot_references.id",
            ),
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'deleted')",
            name="ck_privacy_snapshot_targets_status",
        ),
        CheckConstraint(
            "delete_attempts >= 0", name="ck_privacy_snapshot_delete_attempts"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    privacy_request_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    snapshot_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
