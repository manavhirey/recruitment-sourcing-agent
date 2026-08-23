"""Add privacy requests, suppression, deletion, and durable retention.

Revision ID: 0010_privacy
Revises: 0009_crm
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010_privacy"
down_revision: str | None = "0009_crm"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRIVACY_TABLES = (
    "privacy_requests",
    "privacy_request_checkpoints",
    "suppression_identifiers",
    "privacy_deletion_snapshot_targets",
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
    request_type = sa.Enum(
        "Access",
        "Correction",
        "Deletion",
        "Opt Out",
        name="privacy_request_type",
        native_enum=False,
        length=16,
    )
    request_state = sa.Enum(
        "Received",
        "Identity Verification Required",
        "Approved",
        "Executing",
        "Completed",
        "Rejected",
        name="privacy_request_state",
        native_enum=False,
        length=40,
    )

    op.create_table(
        "privacy_requests",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("candidate_id", uuid, nullable=False),
        sa.Column("request_type", request_type, nullable=False),
        sa.Column("state", request_state, nullable=False),
        sa.Column("submitted_by_user_id", uuid, nullable=False),
        sa.Column("identity_verified_by_user_id", uuid),
        sa.Column("identity_verified_at", sa.DateTime(timezone=True)),
        sa.Column("approved_by_user_id", uuid),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("rejected_by_user_id", uuid),
        sa.Column("rejected_at", sa.DateTime(timezone=True)),
        sa.Column("rejection_reason_code", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "rejection_reason_code IS NULL OR state = 'Rejected'",
            name="ck_privacy_requests_rejection_state",
        ),
        sa.CheckConstraint(
            "request_type IN ('Access', 'Correction', 'Deletion', 'Opt Out')",
            name="ck_privacy_requests_type",
        ),
        sa.CheckConstraint(
            "state IN ('Received', 'Identity Verification Required', 'Approved', "
            "'Executing', 'Completed', 'Rejected')",
            name="ck_privacy_requests_state",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["identity_verified_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["rejected_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
    )
    for column in ("tenant_id", "candidate_id", "request_type", "state"):
        op.create_index(
            op.f(f"ix_privacy_requests_{column}"), "privacy_requests", [column]
        )
    op.create_index(
        "uq_privacy_requests_active_candidate_type",
        "privacy_requests",
        ["tenant_id", "candidate_id", "request_type"],
        unique=True,
        postgresql_where=sa.text("state NOT IN ('Completed', 'Rejected')"),
    )

    op.create_table(
        "privacy_request_checkpoints",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("privacy_request_id", uuid, nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=64)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'failed')",
            name="ck_privacy_request_checkpoints_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_privacy_checkpoint_attempts"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "privacy_request_id"],
            ["privacy_requests.tenant_id", "privacy_requests.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "privacy_request_id", "name"),
    )
    for column in ("tenant_id", "privacy_request_id"):
        op.create_index(
            op.f(f"ix_privacy_request_checkpoints_{column}"),
            "privacy_request_checkpoints",
            [column],
        )

    op.create_table(
        "suppression_identifiers",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("privacy_request_id", uuid, nullable=False),
        sa.Column("identifier_type", sa.String(length=96), nullable=False),
        sa.Column("key_version", sa.String(length=32), nullable=False),
        sa.Column("digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "octet_length(digest) = 32", name="ck_suppression_identifiers_digest"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "privacy_request_id"],
            ["privacy_requests.tenant_id", "privacy_requests.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "identifier_type", "key_version", "digest"),
    )
    for column in ("tenant_id", "privacy_request_id"):
        op.create_index(
            op.f(f"ix_suppression_identifiers_{column}"),
            "suppression_identifiers",
            [column],
        )

    op.create_table(
        "privacy_deletion_snapshot_targets",
        sa.Column("id", uuid, nullable=False),
        sa.Column("tenant_id", uuid, nullable=False),
        sa.Column("privacy_request_id", uuid, nullable=False),
        sa.Column("snapshot_id", uuid, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("delete_attempts", sa.Integer(), nullable=False),
        sa.Column("failure_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(length=64)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'deleted')",
            name="ck_privacy_snapshot_targets_status",
        ),
        sa.CheckConstraint(
            "delete_attempts >= 0", name="ck_privacy_snapshot_delete_attempts"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "privacy_request_id"],
            ["privacy_requests.tenant_id", "privacy_requests.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "snapshot_id"],
            [
                "provider_snapshot_references.tenant_id",
                "provider_snapshot_references.id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "privacy_request_id", "snapshot_id"),
    )
    for column in ("tenant_id", "privacy_request_id", "snapshot_id"):
        op.create_index(
            op.f(f"ix_privacy_deletion_snapshot_targets_{column}"),
            "privacy_deletion_snapshot_targets",
            [column],
        )

    for table in _PRIVACY_TABLES:
        _tenant_policy(table)

    op.add_column(
        "candidate_contact_points",
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="180"),
    )
    op.create_check_constraint(
        "ck_contact_points_retention_days",
        "candidate_contact_points",
        "retention_days >= 1 AND retention_days <= 180",
    )
    op.alter_column("candidate_contact_points", "retention_days", server_default=None)

    op.add_column(
        "provider_snapshot_references",
        sa.Column("delete_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "provider_snapshot_references",
        sa.Column("delete_failure_started_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "provider_snapshot_references",
        sa.Column("last_delete_failure_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "provider_snapshot_references",
        sa.Column("last_delete_error_code", sa.String(length=64)),
    )
    op.add_column(
        "provider_snapshot_references",
        sa.Column("next_delete_attempt_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        op.f("ix_provider_snapshot_references_next_delete_attempt_at"),
        "provider_snapshot_references",
        ["next_delete_attempt_at"],
    )
    op.alter_column(
        "provider_snapshot_references", "delete_attempts", server_default=None
    )

    _create_immutability()
    _replace_contact_expiry_function()
    _replace_snapshot_claim_function()
    _create_snapshot_failure_function()
    _create_privacy_maintenance_functions()
    _grant_privileges()


def _create_immutability() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_suppression_identifier_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'suppression identifiers are append-only'
                USING ERRCODE = '55000';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER suppression_identifiers_append_only "
        "BEFORE UPDATE OR DELETE ON suppression_identifiers "
        "FOR EACH ROW EXECUTE FUNCTION reject_suppression_identifier_mutation()"
    )


def _replace_contact_expiry_function() -> None:
    op.execute("DROP FUNCTION maintenance_erase_due_contacts()")
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
            effective_expiry timestamptz;
        BEGIN
            FOR due IN
                SELECT * FROM public.candidate_contact_points
                WHERE LEAST(
                    expires_at,
                    CASE
                        WHEN last_verified_at IS NULL AND last_used_at IS NULL
                            THEN observed_at
                        ELSE GREATEST(
                            COALESCE(last_verified_at, '-infinity'::timestamptz),
                            COALESCE(last_used_at, '-infinity'::timestamptz)
                        )
                    END + make_interval(days => retention_days)
                ) <= cutoff
                  AND expired_at IS NULL
                ORDER BY expires_at, id
                FOR UPDATE SKIP LOCKED
            LOOP
                effective_expiry := LEAST(
                    due.expires_at,
                    CASE
                        WHEN due.last_verified_at IS NULL
                             AND due.last_used_at IS NULL
                            THEN due.observed_at
                        ELSE GREATEST(
                            COALESCE(
                                due.last_verified_at,
                                '-infinity'::timestamptz
                            ),
                            COALESCE(
                                due.last_used_at,
                                '-infinity'::timestamptz
                            )
                        )
                    END + make_interval(days => due.retention_days)
                );
                IF due.lookup_hmac IS NOT NULL THEN
                    INSERT INTO public.candidate_contact_retention_tombstones
                        (id, tenant_id, candidate_id, contact_point_id, kind,
                         suppression_hmac, retired_at, created_at, updated_at)
                    VALUES
                        (gen_random_uuid(), due.tenant_id, due.candidate_id, due.id,
                         due.kind, due.lookup_hmac, effective_expiry, cutoff, cutoff)
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
    op.execute("REVOKE ALL ON FUNCTION maintenance_erase_due_contacts() FROM PUBLIC")


def _create_snapshot_failure_function() -> None:
    op.execute(
        """
        CREATE FUNCTION maintenance_record_snapshot_delete_failure(
            p_snapshot_id uuid,
            p_error_code text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            failed public.provider_snapshot_references%ROWTYPE;
            cutoff timestamptz := clock_timestamp();
            audience text;
        BEGIN
            UPDATE public.provider_snapshot_references
            SET delete_attempts = delete_attempts + 1,
                delete_failure_started_at = COALESCE(
                    delete_failure_started_at, cutoff
                ),
                last_delete_failure_at = cutoff,
                last_delete_error_code = LEFT(p_error_code, 64),
                maintenance_claimed_at = NULL,
                next_delete_attempt_at = cutoff + make_interval(
                    secs => LEAST(3600, 60 * (1 << LEAST(delete_attempts, 6)))
                )
            WHERE id = p_snapshot_id
            RETURNING * INTO failed;
            IF NOT FOUND THEN
                RETURN false;
            END IF;
            IF failed.delete_failure_started_at <= cutoff - interval '24 hours' THEN
                FOREACH audience IN ARRAY ARRAY['owner', 'admin']
                LOOP
                    INSERT INTO public.tenant_notifications
                        (id, tenant_id, run_id, audience_role, code, title,
                         message, created_at)
                    VALUES
                        (gen_random_uuid(), failed.tenant_id, failed.run_id, audience,
                         'snapshot_expiry_failed', 'Snapshot deletion is delayed',
                         'Encrypted snapshot deletion has failed for more than 24 hours.',
                         cutoff)
                    ON CONFLICT (tenant_id, run_id, audience_role, code) DO NOTHING;
                END LOOP;
            END IF;
            RETURN true;
        END
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION "
        "maintenance_record_snapshot_delete_failure(uuid, text) FROM PUBLIC"
    )


def _replace_snapshot_claim_function() -> None:
    op.execute("DROP FUNCTION maintenance_claim_expired_snapshots(integer)")
    op.execute(
        """
        CREATE FUNCTION maintenance_claim_expired_snapshots(p_limit integer)
        RETURNS TABLE(snapshot_id uuid, tenant_id uuid, object_reference text)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH due AS (
                SELECT id
                FROM public.provider_snapshot_references
                WHERE expires_at <= statement_timestamp()
                  AND (
                    next_delete_attempt_at IS NULL
                    OR next_delete_attempt_at <= statement_timestamp()
                  )
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
            RETURNING snapshots.id, snapshots.tenant_id,
                      snapshots.object_reference::text
        $$
        """
    )
    op.execute(
        "REVOKE ALL ON FUNCTION maintenance_claim_expired_snapshots(integer) "
        "FROM PUBLIC"
    )


def _create_privacy_maintenance_functions() -> None:
    op.execute(
        """
        CREATE FUNCTION privacy_due_deletions(p_limit integer)
        RETURNS TABLE(request_id uuid, tenant_id uuid)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT requests.id, requests.tenant_id
            FROM public.privacy_requests AS requests
            WHERE requests.state = 'Executing'
              AND (
                NOT EXISTS (
                    SELECT 1
                    FROM public.privacy_deletion_snapshot_targets AS targets
                    WHERE targets.tenant_id = requests.tenant_id
                      AND targets.privacy_request_id = requests.id
                )
                OR EXISTS (
                    SELECT 1
                    FROM public.privacy_deletion_snapshot_targets AS targets
                    WHERE targets.tenant_id = requests.tenant_id
                      AND targets.privacy_request_id = requests.id
                      AND (
                        targets.next_attempt_at IS NULL
                        OR targets.next_attempt_at <= statement_timestamp()
                      )
                )
              )
            ORDER BY requests.updated_at, requests.id
            LIMIT greatest(p_limit, 0)
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION privacy_claim_deletion_snapshots(
            p_request_id uuid,
            p_tenant_id uuid,
            p_limit integer
        )
        RETURNS TABLE(target_id uuid, tenant_id uuid, object_reference text)
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH due AS (
                SELECT targets.id, snapshots.object_reference
                FROM public.privacy_deletion_snapshot_targets AS targets
                JOIN public.privacy_requests AS requests
                 ON requests.tenant_id = targets.tenant_id
                 AND requests.id = targets.privacy_request_id
                JOIN public.provider_snapshot_references AS snapshots
                  ON snapshots.tenant_id = targets.tenant_id
                 AND snapshots.id = targets.snapshot_id
                WHERE targets.privacy_request_id = p_request_id
                  AND targets.tenant_id = p_tenant_id
                  AND requests.state = 'Executing'
                  AND targets.status <> 'deleted'
                  AND (
                    targets.next_attempt_at IS NULL
                    OR targets.next_attempt_at <= statement_timestamp()
                  )
                  AND (
                    targets.claimed_at IS NULL
                    OR targets.claimed_at
                       <= statement_timestamp() - interval '1 hour'
                  )
                ORDER BY targets.created_at, targets.id
                FOR UPDATE OF targets SKIP LOCKED
                LIMIT greatest(p_limit, 0)
            )
            UPDATE public.privacy_deletion_snapshot_targets AS targets
            SET status = 'claimed',
                claimed_at = statement_timestamp(),
                next_attempt_at = NULL,
                updated_at = statement_timestamp()
            FROM due
            WHERE targets.id = due.id
            RETURNING targets.id, targets.tenant_id, due.object_reference::text
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION privacy_mark_deletion_snapshot_deleted(p_target_id uuid)
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            target record;
        BEGIN
            SELECT tenant_id, snapshot_id INTO target
            FROM public.privacy_deletion_snapshot_targets
            WHERE id = p_target_id AND status = 'claimed'
            FOR UPDATE;
            IF NOT FOUND THEN
                RETURN false;
            END IF;
            DELETE FROM public.provider_snapshot_references
            WHERE tenant_id = target.tenant_id AND id = target.snapshot_id;
            RETURN FOUND;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION privacy_mark_deletion_snapshot_failed(
            p_target_id uuid,
            p_error_code text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            failed record;
            cutoff timestamptz := clock_timestamp();
            audience text;
        BEGIN
            UPDATE public.privacy_deletion_snapshot_targets
            SET status = 'pending',
                claimed_at = NULL,
                delete_attempts = delete_attempts + 1,
                failure_started_at = COALESCE(failure_started_at, cutoff),
                last_failure_at = cutoff,
                last_error_code = LEFT(p_error_code, 64),
                next_attempt_at = cutoff + make_interval(
                    secs => LEAST(3600, 60 * (1 << LEAST(delete_attempts, 6)))
                ),
                updated_at = cutoff
            WHERE id = p_target_id
            RETURNING tenant_id, snapshot_id, failure_started_at INTO failed;
            IF NOT FOUND THEN
                RETURN false;
            END IF;
            IF failed.failure_started_at <= cutoff - interval '24 hours' THEN
                FOREACH audience IN ARRAY ARRAY['owner', 'admin']
                LOOP
                    INSERT INTO public.tenant_notifications
                        (id, tenant_id, run_id, audience_role, code, title,
                         message, created_at)
                    SELECT gen_random_uuid(), snapshots.tenant_id, snapshots.run_id,
                           audience, 'privacy_snapshot_delete_failed',
                           'Privacy deletion is delayed',
                           'Snapshot deletion for a privacy request has failed for more than 24 hours.',
                           cutoff
                    FROM public.provider_snapshot_references AS snapshots
                    WHERE snapshots.tenant_id = failed.tenant_id
                      AND snapshots.id = failed.snapshot_id
                    ON CONFLICT (tenant_id, run_id, audience_role, code) DO NOTHING;
                END LOOP;
            END IF;
            RETURN true;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION privacy_finalize_deletion(
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
                score_json = '{}'::json,
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
                (gen_random_uuid(), request_row.tenant_id, NULL,
                 COALESCE(
                     request_row.approved_by_user_id,
                     request_row.submitted_by_user_id
                 ),
                 'privacy-deletion-completed:' || request_row.id::text,
                 'privacy.deletion_completed', 'privacy_request', request_row.id,
                 '{}'::json, cutoff)
            ON CONFLICT (tenant_id, event_key) DO NOTHING;
            RETURN true;
        END
        $$
        """
    )
    for signature in (
        "privacy_due_deletions(integer)",
        "privacy_claim_deletion_snapshots(uuid, uuid, integer)",
        "privacy_mark_deletion_snapshot_deleted(uuid)",
        "privacy_mark_deletion_snapshot_failed(uuid, text)",
        "privacy_finalize_deletion(uuid, uuid)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")


def _grant_privileges() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_api') THEN
                GRANT SELECT, INSERT, UPDATE ON privacy_requests,
                    privacy_request_checkpoints TO sourcing_api;
                GRANT SELECT, INSERT ON suppression_identifiers,
                    privacy_deletion_snapshot_targets TO sourcing_api;
                GRANT EXECUTE ON FUNCTION privacy_finalize_deletion(uuid, uuid)
                    TO sourcing_api;
            END IF;
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_maintenance'
            ) THEN
                REVOKE ALL ON privacy_requests, privacy_request_checkpoints,
                    suppression_identifiers, privacy_deletion_snapshot_targets
                    FROM sourcing_maintenance;
                GRANT EXECUTE ON FUNCTION
                    maintenance_erase_due_contacts(),
                    maintenance_claim_expired_snapshots(integer),
                    maintenance_delete_claimed_snapshot(uuid),
                    maintenance_record_snapshot_delete_failure(uuid, text),
                    privacy_due_deletions(integer),
                    privacy_claim_deletion_snapshots(uuid, uuid, integer),
                    privacy_mark_deletion_snapshot_deleted(uuid),
                    privacy_mark_deletion_snapshot_failed(uuid, text),
                    privacy_finalize_deletion(uuid, uuid)
                    TO sourcing_maintenance;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    for signature in (
        "privacy_finalize_deletion(uuid, uuid)",
        "privacy_finalize_deletion(uuid)",
        "privacy_mark_deletion_snapshot_failed(uuid, text)",
        "privacy_mark_deletion_snapshot_deleted(uuid)",
        "privacy_claim_deletion_snapshots(uuid, uuid, integer)",
        "privacy_due_deletions(integer)",
        "maintenance_record_snapshot_delete_failure(uuid, text)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("DROP FUNCTION maintenance_erase_due_contacts()")
    op.execute("DROP FUNCTION maintenance_claim_expired_snapshots(integer)")
    _restore_previous_contact_expiry_function()
    op.drop_constraint(
        "ck_contact_points_retention_days",
        "candidate_contact_points",
        type_="check",
    )
    op.drop_column("candidate_contact_points", "retention_days")
    op.execute(
        "DROP INDEX IF EXISTS ix_provider_snapshot_references_next_delete_attempt_at"
    )
    op.execute(
        "ALTER TABLE provider_snapshot_references "
        "DROP COLUMN IF EXISTS next_delete_attempt_at"
    )
    _restore_previous_snapshot_claim_function()
    for column in (
        "last_delete_error_code",
        "last_delete_failure_at",
        "delete_failure_started_at",
        "delete_attempts",
    ):
        op.drop_column("provider_snapshot_references", column)
    op.execute(
        "DROP TRIGGER IF EXISTS suppression_identifiers_append_only "
        "ON suppression_identifiers"
    )
    op.execute("DROP FUNCTION IF EXISTS reject_suppression_identifier_mutation()")
    for table in reversed(_PRIVACY_TABLES):
        op.drop_table(table)


def _restore_previous_snapshot_claim_function() -> None:
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
        "REVOKE ALL ON FUNCTION maintenance_claim_expired_snapshots(integer) "
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
                    maintenance_claim_expired_snapshots(integer)
                    TO sourcing_maintenance;
            END IF;
        END
        $$
        """
    )


def _restore_previous_contact_expiry_function() -> None:
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
    op.execute("REVOKE ALL ON FUNCTION maintenance_erase_due_contacts() FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'sourcing_maintenance'
            ) THEN
                GRANT EXECUTE ON FUNCTION maintenance_erase_due_contacts()
                    TO sourcing_maintenance;
            END IF;
        END
        $$
        """
    )
