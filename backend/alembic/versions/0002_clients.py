"""Create client companies, recruiter grants, and industry tables.

Revision ID: 0002_clients
Revises: 0001_identity_and_rls
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_clients"
down_revision: str | None = "0001_identity_and_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tenant_policy(table: str, tenant_column: str = "tenant_id") -> None:
    predicate = f"{tenant_column} = current_setting('app.tenant_id', true)::uuid"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "client_companies",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "normalized_name"),
    )
    op.create_index(
        op.f("ix_client_companies_tenant_id"), "client_companies", ["tenant_id"]
    )
    op.create_table(
        "client_industries",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("client_id", uuid, nullable=False),
        sa.Column("industry_code", sa.String(length=128), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["client_id"], ["client_companies.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "industry_code"),
    )
    op.create_index(
        op.f("ix_client_industries_tenant_id"), "client_industries", ["tenant_id"]
    )
    op.create_index(
        op.f("ix_client_industries_client_id"), "client_industries", ["client_id"]
    )
    op.create_table(
        "client_adjacent_industries",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("client_id", uuid, nullable=False),
        sa.Column("industry_code", sa.String(length=128), nullable=False),
        sa.Column("adjacent_industry_code", sa.String(length=128), nullable=False),
        sa.Column("approved_by_user_id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["client_id"], ["client_companies.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "industry_code", "adjacent_industry_code"),
    )
    op.create_index(
        op.f("ix_client_adjacent_industries_tenant_id"),
        "client_adjacent_industries",
        ["tenant_id"],
    )
    op.create_index(
        op.f("ix_client_adjacent_industries_client_id"),
        "client_adjacent_industries",
        ["client_id"],
    )
    op.create_table(
        "client_grants",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("client_id", uuid, nullable=False),
        sa.Column("membership_id", uuid, nullable=False),
        sa.Column("granted_by_user_id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["client_id"], ["client_companies.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["membership_id"], ["memberships.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_id", "membership_id"),
    )
    op.create_index(op.f("ix_client_grants_tenant_id"), "client_grants", ["tenant_id"])
    op.create_index(op.f("ix_client_grants_client_id"), "client_grants", ["client_id"])
    op.create_index(
        op.f("ix_client_grants_membership_id"), "client_grants", ["membership_id"]
    )

    for table in (
        "client_companies",
        "client_industries",
        "client_adjacent_industries",
        "client_grants",
    ):
        _tenant_policy(table)
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_api') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON client_companies, client_industries, client_adjacent_industries,
                       client_grants
                    TO sourcing_api;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_client_grants_membership_id"), table_name="client_grants")
    op.drop_index(op.f("ix_client_grants_client_id"), table_name="client_grants")
    op.drop_index(op.f("ix_client_grants_tenant_id"), table_name="client_grants")
    op.drop_table("client_grants")
    op.drop_index(
        op.f("ix_client_adjacent_industries_client_id"),
        table_name="client_adjacent_industries",
    )
    op.drop_index(
        op.f("ix_client_adjacent_industries_tenant_id"),
        table_name="client_adjacent_industries",
    )
    op.drop_table("client_adjacent_industries")
    op.drop_index(
        op.f("ix_client_industries_client_id"), table_name="client_industries"
    )
    op.drop_index(
        op.f("ix_client_industries_tenant_id"), table_name="client_industries"
    )
    op.drop_table("client_industries")
    op.drop_index(op.f("ix_client_companies_tenant_id"), table_name="client_companies")
    op.drop_table("client_companies")
