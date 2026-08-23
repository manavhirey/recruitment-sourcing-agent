"""Add durable enrichment retry generations.

Revision ID: 0016_enrichment_retry_dispatch
Revises: 0015_tenant_acceptance_fks
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016_enrichment_retry_dispatch"
down_revision: str | None = "0015_tenant_acceptance_fks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "enrichment_retry_dispatches",
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("state_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=255), nullable=False),
        sa.Column("requested_by_user_id", uuid, nullable=False),
        sa.Column("candidate_limit", sa.Integer(), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_token", uuid),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'published', 'claimed', 'completed')",
            name="ck_enrichment_retry_dispatches_status",
        ),
        sa.CheckConstraint(
            "generation > 0", name="ck_enrichment_retry_dispatches_generation"
        ),
        sa.CheckConstraint(
            "candidate_limit >= 0 AND candidate_limit <= 50",
            name="ck_enrichment_retry_dispatches_candidate_limit",
        ),
        sa.CheckConstraint(
            "(claim_token IS NULL) = (claimed_at IS NULL)",
            name="ck_enrichment_retry_dispatches_claim_pair",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed') OR claim_token IS NULL",
            name="ck_enrichment_retry_dispatches_claim_status",
        ),
        sa.CheckConstraint(
            "status <> 'claimed' OR claim_token IS NOT NULL",
            name="ck_enrichment_retry_dispatches_claimed_token",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("tenant_id", "run_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "task_id",
            name="uq_enrichment_retry_dispatches_tenant_task_id",
        ),
    )
    op.create_index(
        "ix_enrichment_retry_dispatches_status_not_before",
        "enrichment_retry_dispatches",
        ["status", "not_before"],
    )
    predicate = "tenant_id = current_setting('app.tenant_id', true)::uuid"
    op.execute("ALTER TABLE enrichment_retry_dispatches ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE enrichment_retry_dispatches FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON enrichment_retry_dispatches "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute("REVOKE ALL ON TABLE enrichment_retry_dispatches FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_api') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON enrichment_retry_dispatches TO sourcing_api;
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION maintenance_claim_pending_enrichment_retries(
            p_batch_size integer DEFAULT 100
        )
        RETURNS TABLE (
            tenant_id uuid,
            run_id uuid,
            generation integer,
            user_id uuid,
            candidate_limit integer,
            task_id text,
            claim_token uuid
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF session_user <> 'sourcing_maintenance' THEN
                RAISE EXCEPTION 'maintenance role required'
                    USING ERRCODE = '42501';
            END IF;
            IF p_batch_size < 1 OR p_batch_size > 100 THEN
                RAISE EXCEPTION 'invalid dispatch batch size'
                    USING ERRCODE = '22023';
            END IF;
            RETURN QUERY
            WITH candidates AS (
                SELECT retries.tenant_id, retries.run_id
                FROM public.enrichment_retry_dispatches AS retries
                WHERE retries.not_before <= pg_catalog.clock_timestamp()
                  AND (
                    (
                      retries.status = 'pending'
                      AND (
                        retries.claimed_at IS NULL
                        OR retries.claimed_at < pg_catalog.clock_timestamp()
                            - interval '15 minutes'
                      )
                    )
                    OR (
                      retries.status = 'claimed'
                      AND retries.claimed_at < pg_catalog.clock_timestamp()
                          - interval '15 minutes'
                    )
                    OR (
                      retries.status = 'published'
                    )
                  )
                ORDER BY retries.not_before, retries.run_id
                FOR UPDATE SKIP LOCKED
                LIMIT p_batch_size
            )
            UPDATE public.enrichment_retry_dispatches AS retries
            SET status = 'pending',
                claimed_at = pg_catalog.clock_timestamp(),
                claim_token = pg_catalog.gen_random_uuid(),
                updated_at = pg_catalog.clock_timestamp()
            FROM candidates
            WHERE retries.tenant_id = candidates.tenant_id
              AND retries.run_id = candidates.run_id
            RETURNING
                retries.tenant_id,
                retries.run_id,
                retries.generation,
                retries.requested_by_user_id,
                retries.candidate_limit,
                retries.task_id::text,
                retries.claim_token;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION maintenance_complete_enrichment_retry_publish(
            p_tenant_id uuid,
            p_run_id uuid,
            p_generation integer,
            p_claim_token uuid
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE changed integer;
        BEGIN
            IF session_user <> 'sourcing_maintenance' THEN
                RAISE EXCEPTION 'maintenance role required'
                    USING ERRCODE = '42501';
            END IF;
            UPDATE public.enrichment_retry_dispatches
            SET status = 'published',
                claimed_at = NULL,
                claim_token = NULL,
                updated_at = pg_catalog.clock_timestamp()
            WHERE tenant_id = p_tenant_id
              AND run_id = p_run_id
              AND generation = p_generation
              AND status = 'pending'
              AND claim_token = p_claim_token;
            GET DIAGNOSTICS changed = ROW_COUNT;
            RETURN changed = 1;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION maintenance_release_enrichment_retry_publish(
            p_tenant_id uuid,
            p_run_id uuid,
            p_generation integer,
            p_claim_token uuid
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE changed integer;
        BEGIN
            IF session_user <> 'sourcing_maintenance' THEN
                RAISE EXCEPTION 'maintenance role required'
                    USING ERRCODE = '42501';
            END IF;
            UPDATE public.enrichment_retry_dispatches
            SET claimed_at = NULL,
                claim_token = NULL,
                updated_at = pg_catalog.clock_timestamp()
            WHERE tenant_id = p_tenant_id
              AND run_id = p_run_id
              AND generation = p_generation
              AND status = 'pending'
              AND claim_token = p_claim_token;
            GET DIAGNOSTICS changed = ROW_COUNT;
            RETURN changed = 1;
        END;
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "maintenance_claim_pending_enrichment_retries(integer) FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "maintenance_complete_enrichment_retry_publish(uuid, uuid, integer, uuid) "
        "FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "maintenance_release_enrichment_retry_publish(uuid, uuid, integer, uuid) "
        "FROM PUBLIC"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_maintenance'
            ) THEN
                GRANT EXECUTE ON FUNCTION
                    maintenance_claim_pending_enrichment_retries(integer),
                    maintenance_complete_enrichment_retry_publish(
                        uuid, uuid, integer, uuid
                    ),
                    maintenance_release_enrichment_retry_publish(
                        uuid, uuid, integer, uuid
                    )
                TO sourcing_maintenance;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "maintenance_release_enrichment_retry_publish(uuid, uuid, integer, uuid)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "maintenance_complete_enrichment_retry_publish(uuid, uuid, integer, uuid)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS maintenance_claim_pending_enrichment_retries(integer)"
    )
    op.drop_index(
        "ix_enrichment_retry_dispatches_status_not_before",
        table_name="enrichment_retry_dispatches",
    )
    op.drop_table("enrichment_retry_dispatches")
