"""Add durable on-demand enrichment dispatch recovery.

Revision ID: 0012_enrich_dispatch_recovery
Revises: 0011_sourcing_dispatch_recovery
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012_enrich_dispatch_recovery"
down_revision: str | None = "0011_sourcing_dispatch_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "enrichment_requests",
        sa.Column(
            "dispatch_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "enrichment_requests",
        sa.Column(
            "dispatch_requested_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
        ),
    )
    op.add_column(
        "enrichment_requests",
        sa.Column("dispatch_claimed_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "enrichment_requests",
        sa.Column("dispatch_claim_token", postgresql.UUID(as_uuid=True)),
    )
    op.create_index(
        "ix_enrichment_requests_pending_dispatch",
        "enrichment_requests",
        ["created_at", "id"],
        postgresql_where=sa.text("dispatch_pending"),
    )
    op.execute(
        """
        CREATE FUNCTION maintenance_claim_pending_enrichment_dispatches(
            p_batch_size integer DEFAULT 100
        )
        RETURNS TABLE (
            request_id uuid,
            tenant_id uuid,
            user_id uuid,
            claim_token uuid,
            dispatch_key text
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
                SELECT pending.id
                FROM public.enrichment_requests AS pending
                WHERE pending.dispatch_pending
                  AND pending.status = 'queued'
                  AND pending.dispatch_requested_by_user_id IS NOT NULL
                  AND (
                    pending.dispatch_claimed_at IS NULL
                    OR pending.dispatch_claimed_at
                        < pg_catalog.clock_timestamp() - interval '5 minutes'
                  )
                ORDER BY pending.created_at, pending.id
                FOR UPDATE SKIP LOCKED
                LIMIT p_batch_size
            )
            UPDATE public.enrichment_requests AS pending
            SET dispatch_claimed_at = pg_catalog.clock_timestamp(),
                dispatch_claim_token = pg_catalog.gen_random_uuid()
            FROM candidates
            WHERE pending.id = candidates.id
            RETURNING
                pending.id,
                pending.tenant_id,
                pending.dispatch_requested_by_user_id,
                pending.dispatch_claim_token,
                'enrichment-request-' || pending.id::text;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION maintenance_complete_enrichment_dispatch(
            p_request_id uuid,
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
            UPDATE public.enrichment_requests
            SET dispatch_pending = false,
                dispatch_claimed_at = NULL,
                dispatch_claim_token = NULL
            WHERE id = p_request_id
              AND dispatch_pending
              AND dispatch_claim_token = p_claim_token;
            GET DIAGNOSTICS changed = ROW_COUNT;
            RETURN changed = 1;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION maintenance_release_enrichment_dispatch(
            p_request_id uuid,
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
            UPDATE public.enrichment_requests
            SET dispatch_claimed_at = NULL,
                dispatch_claim_token = NULL
            WHERE id = p_request_id
              AND dispatch_pending
              AND dispatch_claim_token = p_claim_token;
            GET DIAGNOSTICS changed = ROW_COUNT;
            RETURN changed = 1;
        END;
        $$
        """
    )
    for signature in (
        "maintenance_claim_pending_enrichment_dispatches(integer)",
        "maintenance_complete_enrichment_dispatch(uuid, uuid)",
        "maintenance_release_enrichment_dispatch(uuid, uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_maintenance'
            ) THEN
                GRANT USAGE ON SCHEMA public TO sourcing_maintenance;
                GRANT EXECUTE ON FUNCTION
                    maintenance_claim_pending_enrichment_dispatches(integer),
                    maintenance_complete_enrichment_dispatch(uuid, uuid),
                    maintenance_release_enrichment_dispatch(uuid, uuid)
                    TO sourcing_maintenance;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP FUNCTION IF EXISTS maintenance_release_enrichment_dispatch(uuid, uuid)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS maintenance_complete_enrichment_dispatch(uuid, uuid)"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS maintenance_claim_pending_enrichment_dispatches(integer)"
    )
    op.drop_index(
        "ix_enrichment_requests_pending_dispatch",
        table_name="enrichment_requests",
    )
    op.drop_column("enrichment_requests", "dispatch_claim_token")
    op.drop_column("enrichment_requests", "dispatch_claimed_at")
    op.drop_column("enrichment_requests", "dispatch_requested_by_user_id")
    op.drop_column("enrichment_requests", "dispatch_pending")
