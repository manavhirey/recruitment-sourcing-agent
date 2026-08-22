"""Create agency-scoped candidate identity and provenance.

Revision ID: 0004_candidates
Revises: 0003_jobs_scorecards
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_candidates"
down_revision: str | None = "0003_jobs_scorecards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tenant_policy(table: str) -> None:
    predicate = "tenant_id = current_setting('app.tenant_id', true)::uuid"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "candidates",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("current_title", sa.String(length=255), nullable=True),
        sa.Column("normalized_title", sa.String(length=255), nullable=True),
        sa.Column("current_company", sa.String(length=255), nullable=True),
        sa.Column("normalized_company", sa.String(length=255), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("normalized_location", sa.String(length=255), nullable=True),
        sa.Column("profile_url", sa.String(length=2048), nullable=True),
        sa.Column("normalized_profile_url", sa.String(length=2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
    )
    op.create_index(op.f("ix_candidates_tenant_id"), "candidates", ["tenant_id"])
    op.create_index(
        op.f("ix_candidates_normalized_name"), "candidates", ["normalized_name"]
    )

    op.create_table(
        "candidate_source_identities",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("candidate_id", uuid, nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_person_id", sa.String(length=255), nullable=False),
        sa.Column("profile_url", sa.String(length=2048), nullable=True),
        sa.Column("normalized_profile_url", sa.String(length=2048), nullable=True),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_candidate_source_identities_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "provider", "provider_person_id"),
    )
    op.create_index(
        op.f("ix_candidate_source_identities_tenant_id"),
        "candidate_source_identities",
        ["tenant_id"],
    )
    op.create_index(
        op.f("ix_candidate_source_identities_candidate_id"),
        "candidate_source_identities",
        ["candidate_id"],
    )
    op.create_index(
        "uq_candidate_source_provider_profile_url",
        "candidate_source_identities",
        ["tenant_id", "provider", "normalized_profile_url"],
        unique=True,
        postgresql_where=sa.text("normalized_profile_url IS NOT NULL"),
    )

    op.create_table(
        "candidate_field_provenance",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("candidate_id", uuid, nullable=False),
        sa.Column("source_identity_id", uuid, nullable=False),
        sa.Column("field_name", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_value_hash", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_candidate_field_provenance_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_identity_id"],
            ["candidate_source_identities.tenant_id", "candidate_source_identities.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint(
            "tenant_id", "source_identity_id", "field_name", "observed_value_hash"
        ),
    )
    op.create_index(
        op.f("ix_candidate_field_provenance_tenant_id"),
        "candidate_field_provenance",
        ["tenant_id"],
    )
    op.create_index(
        op.f("ix_candidate_field_provenance_candidate_id"),
        "candidate_field_provenance",
        ["candidate_id"],
    )
    op.create_index(
        op.f("ix_candidate_field_provenance_source_identity_id"),
        "candidate_field_provenance",
        ["source_identity_id"],
    )
    op.create_index(
        "uq_candidate_current_field_provenance",
        "candidate_field_provenance",
        ["tenant_id", "candidate_id", "field_name"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )

    op.create_table(
        "candidate_experiences",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("candidate_id", uuid, nullable=False),
        sa.Column("source_identity_id", uuid, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("company_name", sa.String(length=255), nullable=True),
        sa.Column("start_date", sa.String(length=32), nullable=True),
        sa.Column("end_date", sa.String(length=32), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_value_hash", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_candidate_experiences_position"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_candidate_experiences_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_identity_id"],
            ["candidate_source_identities.tenant_id", "candidate_source_identities.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "source_identity_id", "position"),
    )
    for column in ("tenant_id", "candidate_id", "source_identity_id"):
        op.create_index(
            op.f(f"ix_candidate_experiences_{column}"),
            "candidate_experiences",
            [column],
        )

    op.create_table(
        "candidate_duplicate_suggestions",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("candidate_id", uuid, nullable=False),
        sa.Column("suggested_candidate_id", uuid, nullable=False),
        sa.Column("similarity", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "candidate_id <> suggested_candidate_id",
            name="ck_candidate_duplicate_suggestions_distinct",
        ),
        sa.CheckConstraint(
            "similarity >= 0 AND similarity <= 1",
            name="ck_candidate_duplicate_suggestions_similarity",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "suggested_candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "candidate_id", "suggested_candidate_id"),
    )
    for column in ("tenant_id", "candidate_id", "suggested_candidate_id"):
        op.create_index(
            op.f(f"ix_candidate_duplicate_suggestions_{column}"),
            "candidate_duplicate_suggestions",
            [column],
        )

    tables = (
        "candidates",
        "candidate_source_identities",
        "candidate_field_provenance",
        "candidate_experiences",
        "candidate_duplicate_suggestions",
    )
    for table in tables:
        _tenant_policy(table)
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_api') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON candidates, candidate_source_identities,
                       candidate_field_provenance, candidate_experiences,
                       candidate_duplicate_suggestions
                    TO sourcing_api;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    for table, columns in (
        (
            "candidate_duplicate_suggestions",
            ("suggested_candidate_id", "candidate_id", "tenant_id"),
        ),
        (
            "candidate_experiences",
            ("source_identity_id", "candidate_id", "tenant_id"),
        ),
    ):
        for column in columns:
            op.drop_index(op.f(f"ix_{table}_{column}"), table_name=table)
        op.drop_table(table)
    op.drop_index(
        "uq_candidate_current_field_provenance",
        table_name="candidate_field_provenance",
    )
    for column in ("source_identity_id", "candidate_id", "tenant_id"):
        op.drop_index(
            op.f(f"ix_candidate_field_provenance_{column}"),
            table_name="candidate_field_provenance",
        )
    op.drop_table("candidate_field_provenance")
    op.drop_index(
        "uq_candidate_source_provider_profile_url",
        table_name="candidate_source_identities",
    )
    op.drop_index(
        op.f("ix_candidate_source_identities_candidate_id"),
        table_name="candidate_source_identities",
    )
    op.drop_index(
        op.f("ix_candidate_source_identities_tenant_id"),
        table_name="candidate_source_identities",
    )
    op.drop_table("candidate_source_identities")
    op.drop_index(op.f("ix_candidates_normalized_name"), table_name="candidates")
    op.drop_index(op.f("ix_candidates_tenant_id"), table_name="candidates")
    op.drop_table("candidates")
