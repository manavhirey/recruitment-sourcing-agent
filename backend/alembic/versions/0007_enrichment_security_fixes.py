"""Harden enrichment retention, accounting, and maintenance access.

Revision ID: 0007_enrichment_security_fixes
Revises: 0006_contacts_enrichment
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_enrichment_security_fixes"
down_revision: str | None = "0006_contacts_enrichment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "candidate_contact_points",
        "lookup_hmac",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.add_column(
        "enrichment_requests",
        sa.Column(
            "synchronous_credits",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.alter_column("enrichment_requests", "synchronous_credits", server_default=None)
    op.create_check_constraint(
        "ck_enrichment_requests_synchronous_credits",
        "enrichment_requests",
        "synchronous_credits >= 0",
    )
    op.add_column(
        "enrichment_requests",
        sa.Column("usage_reconciled_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_maintenance'
            ) THEN
                GRANT SELECT, UPDATE ON candidate_contact_points
                    TO sourcing_maintenance;
                GRANT SELECT, DELETE ON provider_snapshot_references
                    TO sourcing_maintenance;
                CREATE POLICY contact_expiry_maintenance_select
                    ON candidate_contact_points
                    FOR SELECT TO sourcing_maintenance
                    USING (expires_at <= now());
                CREATE POLICY contact_expiry_maintenance_update
                    ON candidate_contact_points
                    FOR UPDATE TO sourcing_maintenance
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
                    ON provider_snapshot_references
                    FOR SELECT TO sourcing_maintenance
                    USING (expires_at <= now());
                CREATE POLICY snapshot_expiry_maintenance_delete
                    ON provider_snapshot_references
                    FOR DELETE TO sourcing_maintenance
                    USING (expires_at <= now());
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP POLICY IF EXISTS snapshot_expiry_maintenance_delete "
        "ON provider_snapshot_references"
    )
    op.execute(
        "DROP POLICY IF EXISTS snapshot_expiry_maintenance_select "
        "ON provider_snapshot_references"
    )
    op.execute(
        "DROP POLICY IF EXISTS contact_expiry_maintenance_update "
        "ON candidate_contact_points"
    )
    op.execute(
        "DROP POLICY IF EXISTS contact_expiry_maintenance_select "
        "ON candidate_contact_points"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_maintenance'
            ) THEN
                REVOKE SELECT, UPDATE ON candidate_contact_points
                    FROM sourcing_maintenance;
                REVOKE SELECT, DELETE ON provider_snapshot_references
                    FROM sourcing_maintenance;
            END IF;
        END
        $$
        """
    )
    op.drop_column("enrichment_requests", "usage_reconciled_at")
    op.execute(
        "ALTER TABLE enrichment_requests DROP CONSTRAINT IF EXISTS "
        "ck_enrichment_requests_synchronous_credits"
    )
    op.drop_column("enrichment_requests", "synchronous_credits")
    op.execute("DELETE FROM candidate_contact_points WHERE lookup_hmac IS NULL")
    op.alter_column(
        "candidate_contact_points",
        "lookup_hmac",
        existing_type=sa.String(length=64),
        nullable=False,
    )
