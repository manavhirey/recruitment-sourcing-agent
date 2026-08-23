"""Create durable sourcing orchestration, usage, notifications, and audit.

Revision ID: 0005_sourcing_audit
Revises: 0004_candidates
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_sourcing_audit"
down_revision: str | None = "0004_candidates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_TABLES = (
    "sourcing_runs",
    "run_checkpoints",
    "run_candidates",
    "usage_budgets",
    "usage_ledger",
    "tenant_notifications",
    "audit_events",
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
    run_state = sa.Enum(
        "queued",
        "sourcing",
        "matching",
        "enriching",
        "partially_ready",
        "ready",
        "cancelled",
        "failed",
        name="sourcing_run_state",
        native_enum=False,
    )
    op.create_unique_constraint("uq_jobs_tenant_id_id", "jobs", ["tenant_id", "id"])
    op.create_unique_constraint(
        "uq_scorecard_versions_tenant_id_id",
        "scorecard_versions",
        ["tenant_id", "id"],
    )
    op.create_unique_constraint(
        "uq_scorecard_versions_tenant_job_id",
        "scorecard_versions",
        ["tenant_id", "job_id", "id"],
    )

    op.create_table(
        "sourcing_runs",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("job_id", uuid, nullable=False),
        sa.Column("scorecard_version_id", uuid, nullable=False),
        sa.Column("started_by_user_id", uuid, nullable=False),
        sa.Column("state", run_state, nullable=False),
        sa.Column("planned_queries", sa.JSON(), nullable=False),
        sa.Column("current_stage", sa.String(length=32), nullable=False),
        sa.Column("cancellation_requested", sa.Boolean(), nullable=False),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True)),
        sa.Column("cancellation_requested_by_user_id", uuid),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("matched_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64)),
        sa.Column("error_message", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "candidate_count >= 0", name="ck_sourcing_runs_candidate_count"
        ),
        sa.CheckConstraint("matched_count >= 0", name="ck_sourcing_runs_matched_count"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id", "scorecard_version_id"],
            [
                "scorecard_versions.tenant_id",
                "scorecard_versions.job_id",
                "scorecard_versions.id",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["started_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["cancellation_requested_by_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
    )
    for column in ("tenant_id", "job_id", "scorecard_version_id", "state"):
        op.create_index(op.f(f"ix_sourcing_runs_{column}"), "sourcing_runs", [column])
    op.create_index(
        "uq_sourcing_runs_active_scorecard",
        "sourcing_runs",
        ["tenant_id", "job_id", "scorecard_version_id"],
        unique=True,
        postgresql_where=sa.text(
            "state IN ('queued', 'sourcing', 'matching', 'enriching', "
            "'partially_ready')"
        ),
    )

    op.create_table(
        "run_checkpoints",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "run_id", "idempotency_key"),
    )
    for column in ("tenant_id", "run_id"):
        op.create_index(
            op.f(f"ix_run_checkpoints_{column}"), "run_checkpoints", [column]
        )

    op.create_table(
        "run_candidates",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("candidate_id", uuid, nullable=False),
        sa.Column("scorecard_version_id", uuid, nullable=False),
        sa.Column("match_score", sa.Integer()),
        sa.Column("classification", sa.String(length=32)),
        sa.Column("evidence", sa.JSON()),
        sa.Column("scoring_version", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("matched_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "match_score IS NULL OR (match_score >= 0 AND match_score <= 100)",
            name="ck_run_candidates_match_score",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "scorecard_version_id"],
            ["scorecard_versions.tenant_id", "scorecard_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "run_id", "candidate_id"),
    )
    for column in ("tenant_id", "run_id", "candidate_id", "scorecard_version_id"):
        op.create_index(op.f(f"ix_run_candidates_{column}"), "run_candidates", [column])

    op.create_table(
        "usage_budgets",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("job_id", uuid),
        sa.Column("max_search_pages", sa.Integer()),
        sa.Column("max_enrichments", sa.Integer()),
        sa.Column("max_estimated_credits", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "max_search_pages IS NULL OR max_search_pages >= 0",
            name="ck_usage_budgets_search_pages",
        ),
        sa.CheckConstraint(
            "max_enrichments IS NULL OR max_enrichments >= 0",
            name="ck_usage_budgets_enrichments",
        ),
        sa.CheckConstraint(
            "max_estimated_credits IS NULL OR max_estimated_credits >= 0",
            name="ck_usage_budgets_estimated_credits",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "job_id"),
    )
    for column in ("tenant_id", "job_id"):
        op.create_index(op.f(f"ix_usage_budgets_{column}"), "usage_budgets", [column])
    op.create_index(
        "uq_usage_budgets_tenant_default",
        "usage_budgets",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("job_id IS NULL"),
    )

    op.create_table(
        "usage_ledger",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("job_id", uuid, nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.String(length=128), nullable=False),
        sa.Column("unit_type", sa.String(length=32), nullable=False),
        sa.Column("reservation_key", sa.String(length=255), nullable=False),
        sa.Column("requested_units", sa.Integer(), nullable=False),
        sa.Column("charged_units", sa.Integer()),
        sa.Column("provider_request_id", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reconciled_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "requested_units > 0", name="ck_usage_ledger_requested_units"
        ),
        sa.CheckConstraint(
            "charged_units IS NULL OR charged_units >= 0",
            name="ck_usage_ledger_charged_units",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["jobs.tenant_id", "jobs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "run_id", "reservation_key", "unit_type"),
    )
    for column in ("tenant_id", "run_id", "job_id"):
        op.create_index(op.f(f"ix_usage_ledger_{column}"), "usage_ledger", [column])

    op.create_table(
        "tenant_notifications",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("run_id", uuid),
        sa.Column("audience_role", sa.String(length=32), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by_user_id", uuid),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["acknowledged_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "run_id", "audience_role", "code"),
    )
    for column in ("tenant_id", "run_id"):
        op.create_index(
            op.f(f"ix_tenant_notifications_{column}"),
            "tenant_notifications",
            [column],
        )

    op.create_table(
        "audit_events",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("run_id", uuid),
        sa.Column("actor_user_id", uuid),
        sa.Column("event_key", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", uuid, nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "event_key"),
    )
    for column in ("tenant_id", "run_id", "entity_id"):
        op.create_index(op.f(f"ix_audit_events_{column}"), "audit_events", [column])

    for table in _TENANT_TABLES:
        _tenant_policy(table)

    op.execute(
        """
        CREATE FUNCTION reject_audit_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'audit events are append-only'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_events_append_only "
        "BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION reject_audit_event_mutation()"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_api') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON sourcing_runs, run_checkpoints, run_candidates,
                       usage_budgets, usage_ledger, tenant_notifications,
                       audit_events
                    TO sourcing_api;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS reject_audit_event_mutation()")
    for table, columns in (
        ("audit_events", ("entity_id", "run_id", "tenant_id")),
        ("tenant_notifications", ("run_id", "tenant_id")),
        ("usage_ledger", ("job_id", "run_id", "tenant_id")),
        ("usage_budgets", ("job_id", "tenant_id")),
        (
            "run_candidates",
            ("scorecard_version_id", "candidate_id", "run_id", "tenant_id"),
        ),
        ("run_checkpoints", ("run_id", "tenant_id")),
        (
            "sourcing_runs",
            ("state", "scorecard_version_id", "job_id", "tenant_id"),
        ),
    ):
        if table == "usage_budgets":
            op.drop_index("uq_usage_budgets_tenant_default", table_name=table)
        if table == "sourcing_runs":
            op.drop_index("uq_sourcing_runs_active_scorecard", table_name=table)
        for column in columns:
            op.drop_index(op.f(f"ix_{table}_{column}"), table_name=table)
        op.drop_table(table)
    op.drop_constraint(
        "uq_scorecard_versions_tenant_job_id", "scorecard_versions", type_="unique"
    )
    op.drop_constraint(
        "uq_scorecard_versions_tenant_id_id", "scorecard_versions", type_="unique"
    )
    op.drop_constraint("uq_jobs_tenant_id_id", "jobs", type_="unique")
