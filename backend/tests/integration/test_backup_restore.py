from __future__ import annotations

import pytest

from app.operations.restore_rehearsal import (
    RestoreManifest,
    RestoreSafetyError,
    assert_integrity,
    build_restore_plan,
)


def test_restore_plan_only_targets_explicit_isolated_database() -> None:
    plan = build_restore_plan(
        source_url="postgresql://backup_reader@db.internal/sourcing",
        admin_url="postgresql://restore_admin@restore.internal/postgres",
        restore_database="sourcing_restore_20260816",
        artifact="/tmp/sourcing.dump",
    )

    assert plan.restore_database == "sourcing_restore_20260816"
    assert all("postgresql://" not in argument for argument in plan.dump_command)
    assert all("postgresql://" not in argument for argument in plan.restore_command)
    assert plan.source_environment["PGDATABASE"] == "sourcing"
    assert plan.admin_environment["PGDATABASE"] == "postgres"
    assert "--clean" not in plan.restore_command
    assert "--create" not in plan.restore_command
    assert "--no-acl" not in plan.dump_command
    assert "--no-acl" not in plan.restore_command


@pytest.mark.parametrize(
    "database",
    ["sourcing", "postgres", "template0", "template1", "", "restore"],
)
def test_restore_plan_refuses_broad_or_non_isolated_targets(database: str) -> None:
    with pytest.raises(RestoreSafetyError, match="isolated_restore_database_required"):
        build_restore_plan(
            source_url="postgresql://backup_reader@db.internal/sourcing",
            admin_url="postgresql://restore_admin@restore.internal/postgres",
            restore_database=database,
            artifact="/tmp/sourcing.dump",
        )


def test_restore_integrity_compares_tenant_scoped_counts_and_schema_revision() -> None:
    expected = RestoreManifest(
        schema_revision="0013_provider_connector_state",
        tenant_counts={"tenant-a": 12, "tenant-b": 4},
        candidate_count=16,
        run_candidate_count=31,
        audit_event_count=52,
    )

    assert_integrity(expected, expected.model_copy())

    with pytest.raises(RestoreSafetyError, match="restore_integrity_mismatch"):
        assert_integrity(
            expected,
            expected.model_copy(update={"tenant_counts": {"tenant-a": 16}}),
        )
