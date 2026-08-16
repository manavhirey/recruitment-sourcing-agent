"""Add immutable acceptance cohorts and fail-closed privacy execution.

Revision ID: 0014_final_review_contracts
Revises: 0013_provider_connector_state
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014_final_review_contracts"
down_revision: str | None = "0013_provider_connector_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.drop_constraint("ck_privacy_requests_state", "privacy_requests", type_="check")
    op.create_check_constraint(
        "ck_privacy_requests_state",
        "privacy_requests",
        "state IN ('Received', 'Identity Verification Required', 'Approved', "
        "'Executing', 'Manual Fulfillment Required', 'Completed', 'Rejected')",
    )

    op.create_table(
        "crm_acceptance_cohorts",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("job_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("client_id", uuid, nullable=False),
        sa.Column("scorecard_version_id", uuid, nullable=False),
        sa.Column("market", sa.String(length=16), nullable=False),
        sa.Column("scoring_version", sa.String(length=64), nullable=False),
        sa.Column("candidate_ids", sa.JSON(), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
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
            ["client_id"], ["client_companies.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["scorecard_version_id"],
            ["scorecard_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "run_id"),
    )
    for column in (
        "tenant_id",
        "job_id",
        "run_id",
        "client_id",
        "scorecard_version_id",
    ):
        op.create_index(
            op.f(f"ix_crm_acceptance_cohorts_{column}"),
            "crm_acceptance_cohorts",
            [column],
        )
    predicate = "tenant_id = current_setting('app.tenant_id', true)::uuid"
    op.execute("ALTER TABLE crm_acceptance_cohorts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE crm_acceptance_cohorts FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON crm_acceptance_cohorts "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(
        "CREATE TRIGGER crm_acceptance_cohorts_append_only "
        "BEFORE UPDATE OR DELETE ON crm_acceptance_cohorts "
        "FOR EACH ROW EXECUTE FUNCTION reject_crm_acceptance_snapshot_mutation()"
    )
    op.execute("REVOKE ALL ON TABLE crm_acceptance_cohorts FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_api') THEN
                GRANT SELECT, INSERT ON crm_acceptance_cohorts TO sourcing_api;
            END IF;
        END
        $$
        """
    )

    op.add_column("crm_acceptance_snapshots", sa.Column("client_id", uuid))
    op.add_column("crm_acceptance_snapshots", sa.Column("scorecard_version_id", uuid))
    op.add_column(
        "crm_acceptance_snapshots",
        sa.Column(
            "market", sa.String(length=16), nullable=False, server_default="unknown"
        ),
    )
    op.add_column(
        "crm_acceptance_snapshots",
        sa.Column(
            "scoring_version",
            sa.String(length=64),
            nullable=False,
            server_default="matching-v1",
        ),
    )
    op.execute(
        """
        UPDATE crm_acceptance_snapshots AS snapshots
        SET client_id = jobs.client_id,
            scorecard_version_id = runs.scorecard_version_id
        FROM jobs, sourcing_runs AS runs
        WHERE jobs.tenant_id = snapshots.tenant_id
          AND jobs.id = snapshots.job_id
          AND runs.tenant_id = snapshots.tenant_id
          AND runs.id = snapshots.run_id
        """
    )
    op.alter_column("crm_acceptance_snapshots", "client_id", nullable=False)
    op.alter_column("crm_acceptance_snapshots", "scorecard_version_id", nullable=False)
    op.alter_column("crm_acceptance_snapshots", "market", server_default=None)
    op.alter_column("crm_acceptance_snapshots", "scoring_version", server_default=None)
    op.create_foreign_key(
        "fk_crm_acceptance_snapshots_client_id",
        "crm_acceptance_snapshots",
        "client_companies",
        ["client_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_crm_acceptance_snapshots_scorecard_version_id",
        "crm_acceptance_snapshots",
        "scorecard_versions",
        ["scorecard_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    for column in ("client_id", "scorecard_version_id"):
        op.create_index(
            op.f(f"ix_crm_acceptance_snapshots_{column}"),
            "crm_acceptance_snapshots",
            [column],
        )

    _replace_privacy_finalizer(system_actor=True)


def _replace_privacy_finalizer(*, system_actor: bool) -> None:
    actor_sql = (
        "NULL"
        if system_actor
        else "COALESCE(request_row.approved_by_user_id, "
        "request_row.submitted_by_user_id)"
    )
    payload_sql = '\'{"executor":"system"}\'::json' if system_actor else "'{}'::json"
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION privacy_finalize_deletion(
            p_request_id uuid,
            p_tenant_id uuid
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            request_row public.privacy_requests%ROWTYPE;
            cutoff timestamptz := clock_timestamp();
            configured_tenant text;
        BEGIN
            IF session_user <> 'sourcing_maintenance' THEN
                configured_tenant := NULLIF(
                    current_setting('app.tenant_id', true), ''
                );
                IF configured_tenant IS NULL
                   OR configured_tenant::uuid <> p_tenant_id THEN
                    RETURN false;
                END IF;
            END IF;
            SELECT * INTO request_row
            FROM public.privacy_requests
            WHERE id = p_request_id AND tenant_id = p_tenant_id
            FOR UPDATE;
            IF NOT FOUND THEN
                RETURN false;
            END IF;
            IF request_row.state = 'Completed' THEN
                RETURN false;
            END IF;
            IF request_row.state <> 'Executing' THEN
                RAISE EXCEPTION 'privacy request is not executing'
                    USING ERRCODE = '55000';
            END IF;
            IF EXISTS (
                SELECT 1 FROM public.privacy_deletion_snapshot_targets
                WHERE tenant_id = request_row.tenant_id
                  AND privacy_request_id = request_row.id
                  AND status <> 'deleted'
            ) THEN
                RETURN false;
            END IF;

            DELETE FROM public.candidate_notes AS notes
            USING public.job_candidates AS jobs
            WHERE notes.tenant_id = request_row.tenant_id
              AND jobs.tenant_id = request_row.tenant_id
              AND jobs.candidate_id = request_row.candidate_id
              AND notes.job_candidate_id = jobs.id;
            DELETE FROM public.job_candidate_tags AS tags
            USING public.job_candidates AS jobs
            WHERE tags.tenant_id = request_row.tenant_id
              AND jobs.tenant_id = request_row.tenant_id
              AND jobs.candidate_id = request_row.candidate_id
              AND tags.job_candidate_id = jobs.id;
            UPDATE public.job_candidates
            SET score = 0,
                score_json = '{{}}'::json,
                rejection_note = NULL,
                updated_at = cutoff
            WHERE tenant_id = request_row.tenant_id
              AND candidate_id = request_row.candidate_id;
            UPDATE public.run_candidates
            SET match_score = NULL,
                classification = NULL,
                evidence = NULL,
                scoring_version = NULL
            WHERE tenant_id = request_row.tenant_id
              AND candidate_id = request_row.candidate_id;
            DELETE FROM public.candidate_contact_points
            WHERE tenant_id = request_row.tenant_id
              AND candidate_id = request_row.candidate_id;
            DELETE FROM public.candidate_duplicate_suggestions
            WHERE tenant_id = request_row.tenant_id
              AND (
                candidate_id = request_row.candidate_id
                OR suggested_candidate_id = request_row.candidate_id
              );
            DELETE FROM public.candidate_experiences
            WHERE tenant_id = request_row.tenant_id
              AND candidate_id = request_row.candidate_id;
            DELETE FROM public.candidate_field_provenance
            WHERE tenant_id = request_row.tenant_id
              AND candidate_id = request_row.candidate_id;
            DELETE FROM public.candidate_source_identities
            WHERE tenant_id = request_row.tenant_id
              AND candidate_id = request_row.candidate_id;
            UPDATE public.candidates
            SET full_name = '[deleted]',
                normalized_name = 'deleted-' || id::text,
                current_title = NULL,
                normalized_title = NULL,
                current_company = NULL,
                normalized_company = NULL,
                location = NULL,
                normalized_location = NULL,
                normalized_skills = '[]'::jsonb,
                industry_codes = '[]'::jsonb,
                profile_url = NULL,
                normalized_profile_url = NULL,
                updated_at = cutoff
            WHERE tenant_id = request_row.tenant_id
              AND id = request_row.candidate_id;

            INSERT INTO public.privacy_request_checkpoints
                (id, tenant_id, privacy_request_id, name, status, attempt_count,
                 completed_at, created_at, updated_at)
            SELECT gen_random_uuid(), request_row.tenant_id, request_row.id, name,
                   'completed', 1, cutoff, cutoff, cutoff
            FROM unnest(ARRAY[
                'snapshot_objects_deleted', 'personal_data_redacted', 'completed'
            ]) AS names(name)
            ON CONFLICT (tenant_id, privacy_request_id, name)
            DO UPDATE SET status = 'completed',
                          attempt_count = public.privacy_request_checkpoints.attempt_count + 1,
                          last_error_code = NULL,
                          completed_at = cutoff,
                          updated_at = cutoff;
            UPDATE public.privacy_requests
            SET state = 'Completed', completed_at = cutoff, updated_at = cutoff
            WHERE tenant_id = request_row.tenant_id AND id = request_row.id;
            INSERT INTO public.audit_events
                (id, tenant_id, run_id, actor_user_id, event_key, action,
                 entity_type, entity_id, payload, created_at)
            VALUES
                (gen_random_uuid(), request_row.tenant_id, NULL, {actor_sql},
                 'privacy-deletion-completed:' || request_row.id::text,
                 'privacy.deletion_completed', 'privacy_request', request_row.id,
                 {payload_sql}, cutoff)
            ON CONFLICT (tenant_id, event_key) DO NOTHING;
            RETURN true;
        END
        $$
        """
    )


def downgrade() -> None:
    connection = op.get_bind()
    manual = connection.execute(
        sa.text(
            "SELECT 1 FROM privacy_requests "
            "WHERE state = 'Manual Fulfillment Required' LIMIT 1"
        )
    ).scalar()
    if manual:
        raise RuntimeError("manual_privacy_requests_downgrade_blocked")
    for column in ("scorecard_version_id", "client_id"):
        op.drop_index(
            op.f(f"ix_crm_acceptance_snapshots_{column}"),
            table_name="crm_acceptance_snapshots",
        )
    op.drop_constraint(
        "fk_crm_acceptance_snapshots_scorecard_version_id",
        "crm_acceptance_snapshots",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_crm_acceptance_snapshots_client_id",
        "crm_acceptance_snapshots",
        type_="foreignkey",
    )
    for column in (
        "scoring_version",
        "market",
        "scorecard_version_id",
        "client_id",
    ):
        op.drop_column("crm_acceptance_snapshots", column)
    op.execute(
        "DROP TRIGGER IF EXISTS crm_acceptance_cohorts_append_only "
        "ON crm_acceptance_cohorts"
    )
    op.drop_table("crm_acceptance_cohorts")
    op.drop_constraint("ck_privacy_requests_state", "privacy_requests", type_="check")
    op.create_check_constraint(
        "ck_privacy_requests_state",
        "privacy_requests",
        "state IN ('Received', 'Identity Verification Required', 'Approved', "
        "'Executing', 'Completed', 'Rejected')",
    )
    _replace_privacy_finalizer(system_actor=False)
