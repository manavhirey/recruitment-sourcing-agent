"""Bind acceptance dimensions to their tenant.

Revision ID: 0015_tenant_acceptance_fks
Revises: 0014_final_review_contracts
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015_tenant_acceptance_fks"
down_revision: str | None = "0014_final_review_contracts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_client_companies_tenant_id_id",
        "client_companies",
        ["tenant_id", "id"],
    )
    op.drop_constraint(
        "crm_acceptance_cohorts_client_id_fkey",
        "crm_acceptance_cohorts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "crm_acceptance_cohorts_scorecard_version_id_fkey",
        "crm_acceptance_cohorts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_crm_acceptance_snapshots_client_id",
        "crm_acceptance_snapshots",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_crm_acceptance_snapshots_scorecard_version_id",
        "crm_acceptance_snapshots",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_crm_acceptance_cohorts_tenant_client",
        "crm_acceptance_cohorts",
        "client_companies",
        ["tenant_id", "client_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_crm_acceptance_cohorts_tenant_scorecard",
        "crm_acceptance_cohorts",
        "scorecard_versions",
        ["tenant_id", "scorecard_version_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_crm_acceptance_snapshots_tenant_client",
        "crm_acceptance_snapshots",
        "client_companies",
        ["tenant_id", "client_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_crm_acceptance_snapshots_tenant_scorecard",
        "crm_acceptance_snapshots",
        "scorecard_versions",
        ["tenant_id", "scorecard_version_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_crm_acceptance_snapshots_tenant_scorecard",
        "crm_acceptance_snapshots",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_crm_acceptance_snapshots_tenant_client",
        "crm_acceptance_snapshots",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_crm_acceptance_cohorts_tenant_scorecard",
        "crm_acceptance_cohorts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_crm_acceptance_cohorts_tenant_client",
        "crm_acceptance_cohorts",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_crm_acceptance_snapshots_scorecard_version_id",
        "crm_acceptance_snapshots",
        "scorecard_versions",
        ["scorecard_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_crm_acceptance_snapshots_client_id",
        "crm_acceptance_snapshots",
        "client_companies",
        ["client_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "crm_acceptance_cohorts_scorecard_version_id_fkey",
        "crm_acceptance_cohorts",
        "scorecard_versions",
        ["scorecard_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "crm_acceptance_cohorts_client_id_fkey",
        "crm_acceptance_cohorts",
        "client_companies",
        ["client_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_client_companies_tenant_id_id",
        "client_companies",
        type_="unique",
    )
