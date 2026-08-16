"""Create tenant-scoped jobs and immutable scorecard versions.

Revision ID: 0003_jobs_scorecards
Revises: 0002_clients
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_jobs_scorecards"
down_revision: str | None = "0002_clients"
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
        "jobs",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("client_id", uuid, nullable=False),
        sa.Column("owner_user_id", uuid, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("job_description", sa.Text(), nullable=False),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("employment_model", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("draft_payload", sa.JSON(), nullable=True),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("draft_extraction_status", sa.String(length=32), nullable=False),
        sa.Column("draft_extraction_warning", sa.Text(), nullable=True),
        sa.Column("current_scorecard_id", uuid, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("draft_revision >= 0", name="ck_jobs_draft_revision"),
        sa.ForeignKeyConstraint(
            ["client_id"], ["client_companies.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_jobs_tenant_id"), "jobs", ["tenant_id"])
    op.create_index(op.f("ix_jobs_client_id"), "jobs", ["client_id"])
    op.create_index(
        op.f("ix_jobs_current_scorecard_id"), "jobs", ["current_scorecard_id"]
    )

    op.create_table(
        "scorecard_versions",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("job_id", uuid, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("target_titles", sa.JSON(), nullable=False),
        sa.Column("seniority", sa.JSON(), nullable=False),
        sa.Column("minimum_years", sa.Integer(), nullable=True),
        sa.Column("maximum_years", sa.Integer(), nullable=True),
        sa.Column("locations", sa.JSON(), nullable=False),
        sa.Column("industry_code", sa.String(length=128), nullable=False),
        sa.Column("suggested_adjacent_industries", sa.JSON(), nullable=False),
        sa.Column("uncertainties", sa.JSON(), nullable=False),
        sa.Column("extraction_status", sa.String(length=32), nullable=False),
        sa.Column("confirmed_by_user_id", uuid, nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_scorecard_versions_version"),
        sa.ForeignKeyConstraint(
            ["confirmed_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "version"),
    )
    op.create_index(
        op.f("ix_scorecard_versions_tenant_id"),
        "scorecard_versions",
        ["tenant_id"],
    )
    op.create_index(
        op.f("ix_scorecard_versions_job_id"), "scorecard_versions", ["job_id"]
    )

    op.create_foreign_key(
        "fk_jobs_current_scorecard_id",
        "jobs",
        "scorecard_versions",
        ["current_scorecard_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "scorecard_criteria",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("scorecard_version_id", uuid, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("evidence_required", sa.Boolean(), nullable=False),
        sa.Column("source_text", sa.String(length=500), nullable=True),
        sa.Column("inferred", sa.Boolean(), nullable=False),
        sa.Column("recruiter_entered", sa.Boolean(), nullable=False),
        sa.Column("lawful_requirement_confirmed", sa.Boolean(), nullable=False),
        sa.CheckConstraint("position >= 0", name="ck_scorecard_criteria_position"),
        sa.ForeignKeyConstraint(
            ["scorecard_version_id"],
            ["scorecard_versions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scorecard_version_id", "key"),
        sa.UniqueConstraint("scorecard_version_id", "position"),
    )
    op.create_index(
        op.f("ix_scorecard_criteria_tenant_id"),
        "scorecard_criteria",
        ["tenant_id"],
    )
    op.create_index(
        op.f("ix_scorecard_criteria_scorecard_version_id"),
        "scorecard_criteria",
        ["scorecard_version_id"],
    )

    for table in ("jobs", "scorecard_versions", "scorecard_criteria"):
        _tenant_policy(table)

    op.execute(
        """
        CREATE FUNCTION reject_confirmed_scorecard_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'confirmed scorecards are immutable'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    for table in ("scorecard_versions", "scorecard_criteria"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable "
            f"BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_confirmed_scorecard_update()"
        )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_api') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON jobs, scorecard_versions, scorecard_criteria
                    TO sourcing_api;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    for table in ("scorecard_criteria", "scorecard_versions"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS reject_confirmed_scorecard_update()")
    op.drop_index(
        op.f("ix_scorecard_criteria_scorecard_version_id"),
        table_name="scorecard_criteria",
    )
    op.drop_index(
        op.f("ix_scorecard_criteria_tenant_id"), table_name="scorecard_criteria"
    )
    op.drop_table("scorecard_criteria")
    op.drop_constraint("fk_jobs_current_scorecard_id", "jobs", type_="foreignkey")
    op.drop_index(op.f("ix_scorecard_versions_job_id"), table_name="scorecard_versions")
    op.drop_index(
        op.f("ix_scorecard_versions_tenant_id"), table_name="scorecard_versions"
    )
    op.drop_table("scorecard_versions")
    op.drop_index(op.f("ix_jobs_current_scorecard_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_client_id"), table_name="jobs")
    op.drop_index(op.f("ix_jobs_tenant_id"), table_name="jobs")
    op.drop_table("jobs")
