"""Make on-demand enrichment recovery deadline aware.

Revision ID: 0017_enrich_dispatch_deadlines
Revises: 0016_enrichment_retry_dispatch
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_enrich_dispatch_deadlines"
down_revision: str | None = "0016_enrichment_retry_dispatch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _replace_claim_function(*, deadline_aware: bool) -> None:
    state_predicate = (
        """
                  AND (
                    (
                      pending.status = 'queued'
                      AND (
                        pending.poll_after IS NULL
                        OR pending.poll_after <= pg_catalog.clock_timestamp()
                      )
                    )
                    OR (
                      pending.status = 'submitting'
                      AND pending.stage_deadline IS NOT NULL
                      AND pending.stage_deadline <= pg_catalog.clock_timestamp()
                    )
                  )
        """
        if deadline_aware
        else "AND pending.status = 'queued'"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION maintenance_claim_pending_enrichment_dispatches(
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
                  AND pending.dispatch_requested_by_user_id IS NOT NULL
                  {state_predicate}
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
        "REVOKE ALL ON FUNCTION "
        "maintenance_claim_pending_enrichment_dispatches(integer) FROM PUBLIC"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_maintenance'
            ) THEN
                GRANT EXECUTE ON FUNCTION
                    maintenance_claim_pending_enrichment_dispatches(integer)
                    TO sourcing_maintenance;
            END IF;
        END
        $$
        """
    )


def upgrade() -> None:
    _replace_claim_function(deadline_aware=True)


def downgrade() -> None:
    _replace_claim_function(deadline_aware=False)
