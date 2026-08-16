"""Create encrypted contacts and replay-safe enrichment records.

Revision ID: 0006_contacts_enrichment
Revises: 0005_sourcing_audit
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_contacts_enrichment"
down_revision: str | None = "0005_sourcing_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT_TABLES = (
    "candidate_contact_points",
    "enrichment_requests",
    "enrichment_webhook_deliveries",
    "provider_snapshot_references",
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
    op.add_column(
        "run_candidates",
        sa.Column(
            "enrichment_status",
            sa.String(length=24),
            nullable=False,
            server_default="not_requested",
        ),
    )
    op.alter_column("run_candidates", "enrichment_status", server_default=None)
    op.add_column(
        "run_candidates",
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "candidate_contact_points",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("candidate_id", uuid, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False),
        sa.Column("verification_state", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("lookup_hmac", sa.String(length=64), nullable=False),
        sa.Column("value_ciphertext", sa.LargeBinary()),
        sa.Column("value_nonce", sa.LargeBinary()),
        sa.Column("encrypted_data_key", sa.LargeBinary()),
        sa.Column("key_nonce", sa.LargeBinary()),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expired_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('email', 'phone')", name="ck_contact_points_kind"),
        sa.CheckConstraint(
            "classification IN ('work', 'personal')",
            name="ck_contact_points_classification",
        ),
        sa.CheckConstraint(
            "verification_state IN "
            "('verified', 'unverified', 'unavailable', 'failed', 'expired')",
            name="ck_contact_points_verification_state",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_contact_points_confidence",
        ),
        sa.CheckConstraint(
            "schema_version > 0", name="ck_contact_points_schema_version"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "candidate_id", "kind", "lookup_hmac"),
    )
    for column in (
        "tenant_id",
        "candidate_id",
        "lookup_hmac",
        "expires_at",
    ):
        op.create_index(
            op.f(f"ix_candidate_contact_points_{column}"),
            "candidate_contact_points",
            [column],
        )

    op.create_table(
        "enrichment_requests",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_request_id", sa.String(length=255)),
        sa.Column("candidate_ids", sa.JSON(), nullable=False),
        sa.Column("capability_token_hmac", sa.String(length=64)),
        sa.Column("reservation_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reveal_personal_emails", sa.Boolean(), nullable=False),
        sa.Column("reveal_phone_number", sa.Boolean(), nullable=False),
        sa.Column("stage_deadline", sa.DateTime(timezone=True)),
        sa.Column("poll_after", sa.DateTime(timezone=True)),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued', 'submitting', 'pending', 'completed', 'failed', "
            "'cancelled')",
            name="ck_enrichment_requests_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "run_id", "reservation_key"),
    )
    for column in (
        "tenant_id",
        "run_id",
        "provider_request_id",
        "capability_token_hmac",
    ):
        op.create_index(
            op.f(f"ix_enrichment_requests_{column}"),
            "enrichment_requests",
            [column],
        )

    op.create_table(
        "enrichment_webhook_deliveries",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("enrichment_request_id", uuid, nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("payload_hmac", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "source IN ('webhook', 'poll', 'synchronous')",
            name="ck_webhook_deliveries_source",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "enrichment_request_id"],
            ["enrichment_requests.tenant_id", "enrichment_requests.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "enrichment_request_id", "payload_hmac"),
    )
    for column in ("tenant_id", "enrichment_request_id"):
        op.create_index(
            op.f(f"ix_enrichment_webhook_deliveries_{column}"),
            "enrichment_webhook_deliveries",
            [column],
        )

    op.create_table(
        "provider_snapshot_references",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("enrichment_request_id", uuid, nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("object_reference", sa.String(length=1024), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["sourcing_runs.tenant_id", "sourcing_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "enrichment_request_id"],
            ["enrichment_requests.tenant_id", "enrichment_requests.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "enrichment_request_id"),
    )
    for column in (
        "tenant_id",
        "run_id",
        "enrichment_request_id",
        "expires_at",
    ):
        op.create_index(
            op.f(f"ix_provider_snapshot_references_{column}"),
            "provider_snapshot_references",
            [column],
        )

    for table in _TENANT_TABLES:
        _tenant_policy(table)

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_api') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                    ON candidate_contact_points, enrichment_requests,
                       enrichment_webhook_deliveries, provider_snapshot_references
                    TO sourcing_api;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    for table, columns in (
        (
            "provider_snapshot_references",
            ("expires_at", "enrichment_request_id", "run_id", "tenant_id"),
        ),
        (
            "enrichment_webhook_deliveries",
            ("enrichment_request_id", "tenant_id"),
        ),
        (
            "enrichment_requests",
            (
                "capability_token_hmac",
                "provider_request_id",
                "run_id",
                "tenant_id",
            ),
        ),
        (
            "candidate_contact_points",
            ("expires_at", "lookup_hmac", "candidate_id", "tenant_id"),
        ),
    ):
        for column in columns:
            op.drop_index(op.f(f"ix_{table}_{column}"), table_name=table)
        op.drop_table(table)
    op.drop_column("run_candidates", "enriched_at")
    op.drop_column("run_candidates", "enrichment_status")
