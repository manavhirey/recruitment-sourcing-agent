from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import psycopg
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.operations.restore_rehearsal import (
    RestoreManifest,
    assert_integrity,
    build_restore_plan,
)


def _run(command: tuple[str, ...], environment: dict[str, str]) -> None:
    subprocess.run(
        command,
        env={**os.environ, **environment},
        check=True,
        capture_output=True,
        text=True,
    )


def _database_url(base_url: str, database: str) -> str:
    return (
        make_url(base_url).set(database=database).render_as_string(hide_password=False)
    )


def _manifest(database_url: str) -> RestoreManifest:
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        revision = str(cursor.fetchone()[0])
        cursor.execute(
            "SELECT tenant_id::text, count(*) FROM candidates GROUP BY tenant_id"
        )
        tenant_counts = {str(key): int(value) for key, value in cursor.fetchall()}
        counts: dict[str, int] = {}
        for key, table in (
            ("candidate_count", "candidates"),
            ("run_candidate_count", "run_candidates"),
            ("audit_event_count", "audit_events"),
        ):
            cursor.execute(f"SELECT count(*) FROM {table}")
            counts[key] = int(cursor.fetchone()[0])
    return RestoreManifest(
        schema_revision=revision,
        tenant_counts=tenant_counts,
        **counts,
    )


def _assert_rls(database_url: str, tenant_ids: set[str]) -> None:
    probe_tenants = {
        "7e570000-0000-4000-8000-000000000101",
        "7e570000-0000-4000-8000-000000000102",
    }
    probe_candidates = {
        "7e570000-0000-4000-8000-000000000201",
        "7e570000-0000-4000-8000-000000000202",
    }
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        for index, (tenant_id, candidate_id) in enumerate(
            zip(sorted(probe_tenants), sorted(probe_candidates), strict=True),
            start=1,
        ):
            cursor.execute(
                """
                INSERT INTO tenants (id, slug, created_at)
                VALUES (%s::uuid, %s, now())
                """,
                (tenant_id, f"restore-probe-{index}"),
            )
            cursor.execute(
                """
                INSERT INTO candidates (
                    id, tenant_id, full_name, normalized_name,
                    normalized_skills, industry_codes, created_at, updated_at
                )
                VALUES (
                    %s::uuid, %s::uuid, 'Restore Probe', 'restore probe',
                    '[]'::jsonb, '[]'::jsonb, now(), now()
                )
                """,
                (candidate_id, tenant_id),
            )
    tenant_ids.update(probe_tenants)
    with psycopg.connect(database_url) as connection:
        for tenant_id in tenant_ids:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SET LOCAL ROLE sourcing_api")
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", (tenant_id,)
                )
                cursor.execute(
                    "SELECT count(*) FROM candidates WHERE tenant_id <> %s::uuid",
                    (tenant_id,),
                )
                if int(cursor.fetchone()[0]) != 0:
                    raise RuntimeError("restore_tenant_isolation_failed")


def main() -> None:
    source_url = os.environ["RESTORE_SOURCE_DATABASE_URL"]
    admin_url = os.environ["RESTORE_ADMIN_DATABASE_URL"]
    restore_database = os.environ["RESTORE_DATABASE_NAME"]
    with tempfile.TemporaryDirectory(prefix="sourcing-restore-") as directory:
        artifact = Path(directory) / "database.dump"
        plan = build_restore_plan(
            source_url=source_url,
            admin_url=admin_url,
            restore_database=restore_database,
            artifact=str(artifact),
        )
        try:
            _run(plan.dump_command, dict(plan.source_environment))
            _run(plan.create_command, dict(plan.admin_environment))
            _run(plan.restore_command, dict(plan.restore_environment))
            restored_url = _database_url(admin_url, restore_database)
            expected = _manifest(source_url)
            restored = _manifest(restored_url)
            assert_integrity(expected, restored)
            _assert_rls(restored_url, set(restored.tenant_counts))
        finally:
            _run(
                ("dropdb", "--if-exists", restore_database),
                dict(plan.admin_environment),
            )


if __name__ == "__main__":
    main()
