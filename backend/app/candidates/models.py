from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index(
            "ix_candidates_normalized_name_trgm",
            "normalized_name",
            postgresql_using="gin",
            postgresql_ops={"normalized_name": "gin_trgm_ops"},
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_candidates_normalized_title_trgm",
            "normalized_title",
            postgresql_using="gin",
            postgresql_ops={"normalized_title": "gin_trgm_ops"},
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_candidates_normalized_company_trgm",
            "normalized_company",
            postgresql_using="gin",
            postgresql_ops={"normalized_company": "gin_trgm_ops"},
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_candidates_search_fts",
            text(
                "to_tsvector('simple'::regconfig, (((((COALESCE(normalized_name, "
                "''::character varying)::text || ' '::text) || "
                "COALESCE(normalized_title, ''::character varying)::text) || "
                "' '::text) || COALESCE(normalized_company, "
                "''::character varying)::text) || ' '::text) || "
                "normalized_skills::text)"
            ),
            postgresql_using="gin",
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    current_title: Mapped[str | None] = mapped_column(String(255))
    normalized_title: Mapped[str | None] = mapped_column(String(255))
    current_company: Mapped[str | None] = mapped_column(String(255))
    normalized_company: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    normalized_location: Mapped[str | None] = mapped_column(String(255))
    normalized_skills: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), default=list, nullable=False
    )
    industry_codes: Mapped[list[str]] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), default=list, nullable=False
    )
    profile_url: Mapped[str | None] = mapped_column(String(2048))
    normalized_profile_url: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SourceIdentity(Base):
    __tablename__ = "candidate_source_identities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "provider", "provider_person_id"),
        ForeignKeyConstraint(
            ("tenant_id", "candidate_id"),
            ("candidates.tenant_id", "candidates.id"),
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_candidate_source_identities_confidence",
        ),
        Index(
            "uq_candidate_source_provider_profile_url",
            "tenant_id",
            "provider",
            "normalized_profile_url",
            unique=True,
            postgresql_where=text("normalized_profile_url IS NOT NULL"),
            sqlite_where=text("normalized_profile_url IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_person_id: Mapped[str] = mapped_column(String(255), nullable=False)
    profile_url: Mapped[str | None] = mapped_column(String(2048))
    normalized_profile_url: Mapped[str | None] = mapped_column(String(2048))
    source_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CandidateFieldProvenance(Base):
    __tablename__ = "candidate_field_provenance"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id", "source_identity_id", "field_name", "observed_value_hash"
        ),
        ForeignKeyConstraint(
            ("tenant_id", "candidate_id"),
            ("candidates.tenant_id", "candidates.id"),
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "source_identity_id"),
            (
                "candidate_source_identities.tenant_id",
                "candidate_source_identities.id",
            ),
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_candidate_field_provenance_confidence",
        ),
        Index(
            "uq_candidate_current_field_provenance",
            "tenant_id",
            "candidate_id",
            "field_name",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current = 1"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_identity_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observed_value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class CandidateExperience(Base):
    __tablename__ = "candidate_experiences"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "source_identity_id", "position"),
        ForeignKeyConstraint(
            ("tenant_id", "candidate_id"),
            ("candidates.tenant_id", "candidates.id"),
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "source_identity_id"),
            (
                "candidate_source_identities.tenant_id",
                "candidate_source_identities.id",
            ),
            ondelete="CASCADE",
        ),
        CheckConstraint("position >= 0", name="ck_candidate_experiences_position"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_candidate_experiences_confidence",
        ),
        Index(
            "ix_candidate_experiences_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_candidate_experiences_company_trgm",
            "company_name",
            postgresql_using="gin",
            postgresql_ops={"company_name": "gin_trgm_ops"},
        ).ddl_if(dialect="postgresql"),
        Index(
            "ix_candidate_experiences_search_fts",
            text(
                "to_tsvector('simple'::regconfig, (COALESCE(title, "
                "''::character varying)::text || ' '::text) || "
                "COALESCE(company_name, ''::character varying)::text)"
            ),
            postgresql_using="gin",
        ).ddl_if(dialect="postgresql"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    source_identity_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    company_name: Mapped[str | None] = mapped_column(String(255))
    start_date: Mapped[str | None] = mapped_column(String(32))
    end_date: Mapped[str | None] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    source_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    observed_value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)


class DuplicateSuggestion(Base):
    __tablename__ = "candidate_duplicate_suggestions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "candidate_id", "suggested_candidate_id"),
        ForeignKeyConstraint(
            ("tenant_id", "candidate_id"),
            ("candidates.tenant_id", "candidates.id"),
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "suggested_candidate_id"),
            ("candidates.tenant_id", "candidates.id"),
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "candidate_id <> suggested_candidate_id",
            name="ck_candidate_duplicate_suggestions_distinct",
        ),
        CheckConstraint(
            "similarity >= 0 AND similarity <= 1",
            name="ck_candidate_duplicate_suggestions_similarity",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    suggested_candidate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    similarity: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ContactPoint(Base):
    __tablename__ = "candidate_contact_points"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "candidate_id", "kind", "lookup_hmac"),
        ForeignKeyConstraint(
            ("tenant_id", "candidate_id"),
            ("candidates.tenant_id", "candidates.id"),
            ondelete="CASCADE",
        ),
        CheckConstraint("kind IN ('email', 'phone')", name="ck_contact_points_kind"),
        CheckConstraint(
            "classification IN ('work', 'personal')",
            name="ck_contact_points_classification",
        ),
        CheckConstraint(
            "verification_state IN "
            "('verified', 'unverified', 'unavailable', 'failed', 'expired')",
            name="ck_contact_points_verification_state",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_contact_points_confidence",
        ),
        CheckConstraint(
            "retention_days >= 1 AND retention_days <= 180",
            name="ck_contact_points_retention_days",
        ),
        CheckConstraint("schema_version > 0", name="ck_contact_points_schema_version"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    classification: Mapped[str] = mapped_column(String(16), nullable=False)
    verification_state: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    lookup_hmac: Mapped[str | None] = mapped_column(String(64), index=True)
    value_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    value_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    encrypted_data_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    key_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False, default=180)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class ContactRetentionTombstone(Base):
    __tablename__ = "candidate_contact_retention_tombstones"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "candidate_id", "kind", "suppression_hmac"),
        ForeignKeyConstraint(
            ("tenant_id", "candidate_id"),
            ("candidates.tenant_id", "candidates.id"),
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "contact_point_id"),
            ("candidate_contact_points.tenant_id", "candidate_contact_points.id"),
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "kind IN ('email', 'phone')", name="ck_contact_tombstones_kind"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    contact_point_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    suppression_hmac: Mapped[str] = mapped_column(String(64), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
