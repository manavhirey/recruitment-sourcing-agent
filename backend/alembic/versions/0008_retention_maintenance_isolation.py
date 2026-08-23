"""Add retention tombstones and isolate privileged maintenance.

Revision ID: 0008_retention_maintenance
Revises: 0007_enrichment_security_fixes
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_retention_maintenance"
down_revision: str | None = "0007_enrichment_security_fixes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "candidate_contact_retention_tombstones",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("candidate_id", uuid, nullable=False),
        sa.Column("contact_point_id", uuid, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("suppression_hmac", sa.String(length=64), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('email', 'phone')", name="ck_contact_tombstones_kind"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "contact_point_id"],
            ["candidate_contact_points.tenant_id", "candidate_contact_points.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "candidate_id", "kind", "suppression_hmac"),
    )
    for column in ("tenant_id", "candidate_id", "contact_point_id"):
        op.create_index(
            op.f(f"ix_candidate_contact_retention_tombstones_{column}"),
            "candidate_contact_retention_tombstones",
            [column],
        )
    op.execute(
        "ALTER TABLE candidate_contact_retention_tombstones ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE candidate_contact_retention_tombstones FORCE ROW LEVEL SECURITY"
    )
    predicate = "tenant_id = current_setting('app.tenant_id', true)::uuid"
    op.execute(
        "CREATE POLICY tenant_isolation "
        "ON candidate_contact_retention_tombstones "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_api') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON candidate_contact_retention_tombstones
                    TO sourcing_api;
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        INSERT INTO candidate_contact_retention_tombstones
            (id, tenant_id, candidate_id, contact_point_id, kind,
             suppression_hmac, retired_at, created_at, updated_at)
        SELECT gen_random_uuid(), tenant_id, candidate_id, id, kind,
               lookup_hmac, NULL, now(), now()
        FROM candidate_contact_points
        WHERE lookup_hmac IS NOT NULL
        """
    )
    op.add_column(
        "provider_snapshot_references",
        sa.Column("maintenance_claimed_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        op.f("ix_provider_snapshot_references_maintenance_claimed_at"),
        "provider_snapshot_references",
        ["maintenance_claimed_at"],
    )

    _remove_broad_maintenance_access()
    _create_maintenance_functions()


def _remove_broad_maintenance_access() -> None:
    for policy, table in (
        ("contact_expiry_maintenance_select", "candidate_contact_points"),
        ("contact_expiry_maintenance_update", "candidate_contact_points"),
        ("snapshot_expiry_maintenance_select", "provider_snapshot_references"),
        ("snapshot_expiry_maintenance_delete", "provider_snapshot_references"),
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_maintenance')
            THEN
                REVOKE ALL ON candidate_contact_points FROM sourcing_maintenance;
                REVOKE ALL ON candidate_contact_retention_tombstones
                    FROM sourcing_maintenance;
                REVOKE ALL ON provider_snapshot_references
                    FROM sourcing_maintenance;
            END IF;
        END
        $$
        """
    )


def _create_maintenance_functions() -> None:
    op.execute(
        """
        CREATE FUNCTION maintenance_erase_due_contacts()
        RETURNS integer
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            due record;
            affected integer := 0;
            cutoff timestamptz := clock_timestamp();
        BEGIN
            FOR due IN
                SELECT * FROM public.candidate_contact_points
                WHERE expires_at <= cutoff AND expired_at IS NULL
                ORDER BY expires_at, id
                FOR UPDATE SKIP LOCKED
            LOOP
                IF due.lookup_hmac IS NOT NULL THEN
                    INSERT INTO public.candidate_contact_retention_tombstones
                        (id, tenant_id, candidate_id, contact_point_id, kind,
                         suppression_hmac, retired_at, created_at, updated_at)
                    VALUES
                        (gen_random_uuid(), due.tenant_id, due.candidate_id, due.id,
                         due.kind, due.lookup_hmac, due.expires_at, cutoff, cutoff)
                    ON CONFLICT (tenant_id, candidate_id, kind, suppression_hmac)
                    DO UPDATE SET retired_at = EXCLUDED.retired_at,
                                  updated_at = EXCLUDED.updated_at;
                END IF;
                UPDATE public.candidate_contact_points
                SET value_ciphertext = NULL,
                    value_nonce = NULL,
                    encrypted_data_key = NULL,
                    key_nonce = NULL,
                    lookup_hmac = NULL,
                    verification_state = 'expired',
                    expired_at = cutoff,
                    updated_at = cutoff
                WHERE id = due.id AND tenant_id = due.tenant_id;
                affected := affected + 1;
            END LOOP;
            RETURN affected;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION maintenance_claim_expired_snapshots(p_limit integer)
        RETURNS TABLE(snapshot_id uuid, object_reference text)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH due AS (
                SELECT id
                FROM public.provider_snapshot_references
                WHERE expires_at <= statement_timestamp()
                  AND (
                    maintenance_claimed_at IS NULL
                    OR maintenance_claimed_at
                       <= statement_timestamp() - interval '1 hour'
                  )
                ORDER BY expires_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT greatest(p_limit, 0)
            )
            UPDATE public.provider_snapshot_references AS snapshots
            SET maintenance_claimed_at = statement_timestamp()
            FROM due
            WHERE snapshots.id = due.id
            RETURNING snapshots.id, snapshots.object_reference::text
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION maintenance_delete_claimed_snapshot(p_snapshot_id uuid)
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            affected integer;
        BEGIN
            DELETE FROM public.provider_snapshot_references
            WHERE id = p_snapshot_id
              AND expires_at <= statement_timestamp()
              AND maintenance_claimed_at IS NOT NULL;
            GET DIAGNOSTICS affected = ROW_COUNT;
            RETURN affected = 1;
        END
        $$
        """
    )
    for signature in (
        "maintenance_erase_due_contacts()",
        "maintenance_claim_expired_snapshots(integer)",
        "maintenance_delete_claimed_snapshot(uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_maintenance')
            THEN
                GRANT EXECUTE ON FUNCTION
                    maintenance_erase_due_contacts(),
                    maintenance_claim_expired_snapshots(integer),
                    maintenance_delete_claimed_snapshot(uuid)
                TO sourcing_maintenance;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    for signature in (
        "maintenance_delete_claimed_snapshot(uuid)",
        "maintenance_claim_expired_snapshots(integer)",
        "maintenance_erase_due_contacts()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.drop_index(
        op.f("ix_provider_snapshot_references_maintenance_claimed_at"),
        table_name="provider_snapshot_references",
    )
    op.drop_column("provider_snapshot_references", "maintenance_claimed_at")
    op.drop_table("candidate_contact_retention_tombstones")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_maintenance')
            THEN
                GRANT SELECT, UPDATE ON candidate_contact_points
                    TO sourcing_maintenance;
                GRANT SELECT, DELETE ON provider_snapshot_references
                    TO sourcing_maintenance;
                CREATE POLICY contact_expiry_maintenance_select
                    ON candidate_contact_points FOR SELECT TO sourcing_maintenance
                    USING (expires_at <= now());
                CREATE POLICY contact_expiry_maintenance_update
                    ON candidate_contact_points FOR UPDATE TO sourcing_maintenance
                    USING (expires_at <= now())
                    WITH CHECK (
                        verification_state = 'expired'
                        AND value_ciphertext IS NULL
                        AND value_nonce IS NULL
                        AND encrypted_data_key IS NULL
                        AND key_nonce IS NULL
                        AND lookup_hmac IS NULL
                    );
                CREATE POLICY snapshot_expiry_maintenance_select
                    ON provider_snapshot_references FOR SELECT TO sourcing_maintenance
                    USING (expires_at <= now());
                CREATE POLICY snapshot_expiry_maintenance_delete
                    ON provider_snapshot_references FOR DELETE TO sourcing_maintenance
                    USING (expires_at <= now());
            END IF;
        END
        $$
        """
    )
