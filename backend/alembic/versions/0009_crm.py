"""Add tenant-isolated candidate review CRM.

Revision ID: 0009_crm
Revises: 0008_retention_maintenance
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_crm"
down_revision: str | None = "0008_retention_maintenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CRM_TABLES = (
    "job_candidates",
    "crm_acceptance_snapshots",
    "candidate_notes",
    "crm_tags",
    "job_candidate_tags",
    "crm_activity_events",
)


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
    jsonb = postgresql.JSONB(astext_type=sa.Text())
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.add_column(
        "candidates",
        sa.Column(
            "normalized_skills",
            jsonb,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "candidates",
        sa.Column(
            "industry_codes",
            jsonb,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("candidates", "normalized_skills", server_default=None)
    op.alter_column("candidates", "industry_codes", server_default=None)
    for column in ("normalized_name", "normalized_title", "normalized_company"):
        op.create_index(
            f"ix_candidates_{column}_trgm",
            "candidates",
            [column],
            postgresql_using="gin",
            postgresql_ops={column: "gin_trgm_ops"},
        )
    op.execute(
        "CREATE INDEX ix_candidates_search_fts ON candidates USING gin "
        "(to_tsvector('simple'::regconfig, "
        "coalesce(normalized_name, '') || ' ' || "
        "coalesce(normalized_title, '') || ' ' || "
        "coalesce(normalized_company, '') || ' ' || normalized_skills::text))"
    )
    for index_name, column in (
        ("ix_candidate_experiences_title_trgm", "title"),
        ("ix_candidate_experiences_company_trgm", "company_name"),
    ):
        op.create_index(
            index_name,
            "candidate_experiences",
            [column],
            postgresql_using="gin",
            postgresql_ops={column: "gin_trgm_ops"},
        )
    op.execute(
        "CREATE INDEX ix_candidate_experiences_search_fts "
        "ON candidate_experiences USING gin "
        "(to_tsvector('simple'::regconfig, coalesce(title, '') || ' ' || "
        "coalesce(company_name, '')))"
    )

    stage = sa.Enum(
        "New",
        "Reviewed",
        "Shortlisted",
        "Rejected",
        name="candidate_stage",
        native_enum=False,
        length=16,
    )
    op.create_table(
        "job_candidates",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("job_id", uuid, nullable=False),
        sa.Column("candidate_id", uuid, nullable=False),
        sa.Column("latest_run_id", uuid),
        sa.Column("classification", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("score_json", sa.JSON(), nullable=False),
        sa.Column("scorecard_version_id", uuid, nullable=False),
        sa.Column("scoring_version", sa.String(length=64), nullable=False),
        sa.Column("stage", stage, nullable=False),
        sa.Column("owner_user_id", uuid),
        sa.Column("rejection_reason_code", sa.String(length=64)),
        sa.Column("rejection_note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "classification IN ('main', 'near_match')",
            name="ck_job_candidates_classification",
        ),
        sa.CheckConstraint(
            "stage IN ('New', 'Reviewed', 'Shortlisted', 'Rejected')",
            name="ck_job_candidates_stage",
        ),
        sa.CheckConstraint(
            "rejection_reason_code IS NULL OR rejection_reason_code IN "
            "('not_qualified', 'compensation_mismatch', 'location_mismatch', "
            "'work_authorization', 'duplicate', 'other')",
            name="ck_job_candidates_rejection_reason",
        ),
        sa.CheckConstraint(
            "(stage = 'Rejected' AND rejection_reason_code IS NOT NULL) OR "
            "(stage <> 'Rejected' AND rejection_reason_code IS NULL AND "
            "rejection_note IS NULL)",
            name="ck_job_candidates_rejection_state",
        ),
        sa.CheckConstraint(
            "score >= 0 AND score <= 100", name="ck_job_candidates_score"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "latest_run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "scorecard_version_id"],
            ["scorecard_versions.tenant_id", "scorecard_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "job_id", "candidate_id"),
    )
    for column in (
        "tenant_id",
        "job_id",
        "candidate_id",
        "latest_run_id",
        "scorecard_version_id",
        "stage",
        "owner_user_id",
    ):
        op.create_index(op.f(f"ix_job_candidates_{column}"), "job_candidates", [column])
    op.create_index(
        "ix_job_candidates_rank",
        "job_candidates",
        ["tenant_id", "job_id", "score", "id"],
    )
    op.create_index(
        "ix_job_candidates_directory",
        "job_candidates",
        ["tenant_id", "updated_at", "id"],
    )

    op.create_table(
        "crm_acceptance_snapshots",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("job_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("finalized_by_user_id", uuid, nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("denominator", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("reviewed_count", sa.Integer(), nullable=False),
        sa.Column("shortlisted_count", sa.Integer(), nullable=False),
        sa.Column("new_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("cohort_candidate_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "denominator = 20", name="ck_crm_acceptance_snapshots_denominator"
        ),
        sa.CheckConstraint(
            "accepted_count = reviewed_count + shortlisted_count",
            name="ck_crm_acceptance_snapshots_accepted",
        ),
        sa.CheckConstraint(
            "accepted_count >= 0 AND reviewed_count >= 0 AND "
            "shortlisted_count >= 0 AND new_count >= 0 AND rejected_count >= 0",
            name="ck_crm_acceptance_snapshots_counts",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["finalized_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "job_id", "run_id"),
    )
    for column in ("tenant_id", "job_id", "run_id"):
        op.create_index(
            op.f(f"ix_crm_acceptance_snapshots_{column}"),
            "crm_acceptance_snapshots",
            [column],
        )

    op.create_table(
        "candidate_notes",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("job_candidate_id", uuid, nullable=False),
        sa.Column("actor_user_id", uuid, nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_candidate_id"],
            ["job_candidates.tenant_id", "job_candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
    )
    for column in ("tenant_id", "job_candidate_id"):
        op.create_index(
            op.f(f"ix_candidate_notes_{column}"), "candidate_notes", [column]
        )
    op.create_index(
        "ix_candidate_notes_activity",
        "candidate_notes",
        ["tenant_id", "job_candidate_id", "updated_at", "id"],
    )

    op.create_table(
        "crm_tags",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("normalized_name", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "normalized_name"),
    )
    op.create_index(op.f("ix_crm_tags_tenant_id"), "crm_tags", ["tenant_id"])

    op.create_table(
        "job_candidate_tags",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("job_candidate_id", uuid, nullable=False),
        sa.Column("tag_id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_candidate_id"],
            ["job_candidates.tenant_id", "job_candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tag_id"],
            ["crm_tags.tenant_id", "crm_tags.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "job_candidate_id", "tag_id"),
    )
    for column in ("tenant_id", "job_candidate_id", "tag_id"):
        op.create_index(
            op.f(f"ix_job_candidate_tags_{column}"), "job_candidate_tags", [column]
        )

    op.create_table(
        "crm_activity_events",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("job_candidate_id", uuid, nullable=False),
        sa.Column("actor_user_id", uuid, nullable=False),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_candidate_id"],
            ["job_candidates.tenant_id", "job_candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "event_key"),
        sa.UniqueConstraint("tenant_id", "id"),
    )
    for column in ("tenant_id", "job_candidate_id"):
        op.create_index(
            op.f(f"ix_crm_activity_events_{column}"), "crm_activity_events", [column]
        )
    op.create_index(
        "ix_crm_activity_cursor",
        "crm_activity_events",
        ["tenant_id", "job_candidate_id", "updated_at", "id"],
    )

    for table in _CRM_TABLES:
        _tenant_policy(table)
    op.execute(
        """
        CREATE FUNCTION reject_crm_activity_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'CRM activity events are append-only'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER crm_activity_events_append_only "
        "BEFORE UPDATE OR DELETE ON crm_activity_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_crm_activity_mutation()"
    )
    op.execute(
        """
        CREATE FUNCTION reject_crm_acceptance_snapshot_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'CRM acceptance snapshots are append-only'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER crm_acceptance_snapshots_append_only "
        "BEFORE UPDATE OR DELETE ON crm_acceptance_snapshots "
        "FOR EACH ROW EXECUTE FUNCTION reject_crm_acceptance_snapshot_mutation()"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_api') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON job_candidates, candidate_notes, crm_tags,
                       job_candidate_tags, crm_activity_events
                    TO sourcing_api;
                GRANT SELECT, INSERT ON crm_acceptance_snapshots TO sourcing_api;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS crm_acceptance_snapshots_append_only "
        "ON crm_acceptance_snapshots"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_crm_acceptance_snapshot_mutation()")
    op.execute(
        "DROP TRIGGER IF EXISTS crm_activity_events_append_only ON crm_activity_events"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_crm_activity_mutation()")
    for table in (
        "crm_activity_events",
        "job_candidate_tags",
        "crm_tags",
        "candidate_notes",
        "crm_acceptance_snapshots",
        "job_candidates",
    ):
        op.drop_table(table, if_exists=True)
    op.execute("DROP INDEX IF EXISTS ix_candidate_experiences_search_fts")
    op.drop_index(
        "ix_candidate_experiences_company_trgm",
        table_name="candidate_experiences",
    )
    op.drop_index(
        "ix_candidate_experiences_title_trgm",
        table_name="candidate_experiences",
    )
    op.execute("DROP INDEX IF EXISTS ix_candidates_search_fts")
    for column in ("normalized_company", "normalized_title", "normalized_name"):
        op.drop_index(f"ix_candidates_{column}_trgm", table_name="candidates")
    op.drop_column("candidates", "industry_codes")
    op.drop_column("candidates", "normalized_skills")
