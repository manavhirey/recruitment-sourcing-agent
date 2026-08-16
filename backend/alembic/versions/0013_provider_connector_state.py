"""Add durable platform provider connector state.

Revision ID: 0013_provider_connector_state
Revises: 0012_enrich_dispatch_recovery
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_provider_connector_state"
down_revision: str | None = "0012_enrich_dispatch_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_connector_states",
        sa.Column("provider", sa.String(length=64), primary_key=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("disabled_reason", sa.String(length=64)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute("REVOKE ALL ON TABLE provider_connector_states FROM PUBLIC")
    op.execute(
        """
        INSERT INTO provider_connector_states (provider, enabled)
        VALUES ('apollo', TRUE)
        ON CONFLICT (provider) DO NOTHING
        """
    )
    op.execute(
        """
        CREATE FUNCTION maintenance_stuck_run_count(requested_state text)
        RETURNS bigint
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT count(*)
            FROM public.sourcing_runs
            WHERE state::text = requested_state
              AND requested_state IN ('queued', 'sourcing', 'matching', 'enriching')
              AND updated_at < clock_timestamp() - interval '10 minutes'
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION maintenance_stuck_run_count(text) FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_maintenance'
            ) THEN
                GRANT EXECUTE ON FUNCTION maintenance_stuck_run_count(text)
                    TO sourcing_maintenance;
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_api') THEN
                GRANT SELECT, INSERT, UPDATE ON TABLE provider_connector_states
                    TO sourcing_api;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    connection = op.get_bind()
    disabled = connection.execute(
        sa.text(
            "SELECT 1 FROM provider_connector_states WHERE enabled IS FALSE LIMIT 1"
        )
    ).scalar()
    if disabled:
        raise RuntimeError("provider_circuit_disabled_downgrade_blocked")
    op.execute("DROP FUNCTION maintenance_stuck_run_count(text)")
    op.drop_table("provider_connector_states")
