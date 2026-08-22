from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict
from sqlalchemy.engine import URL, make_url

_ISOLATED_DATABASE = re.compile(r"^sourcing_restore_[a-zA-Z0-9_]{8,64}$")


class RestoreSafetyError(RuntimeError):
    pass


class RestoreManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_revision: str
    tenant_counts: dict[str, int]
    candidate_count: int
    run_candidate_count: int
    audit_event_count: int


@dataclass(frozen=True)
class RestorePlan:
    restore_database: str
    dump_command: tuple[str, ...]
    create_command: tuple[str, ...]
    restore_command: tuple[str, ...]
    source_environment: Mapping[str, str]
    admin_environment: Mapping[str, str]
    restore_environment: Mapping[str, str]


def _pg_environment(url: URL, database: str | None = None) -> dict[str, str]:
    if not url.host or not url.username or not (database or url.database):
        raise RestoreSafetyError("postgres_connection_fields_required")
    environment = {
        "PGHOST": url.host,
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username,
        "PGDATABASE": database or str(url.database),
    }
    if url.password:
        environment["PGPASSWORD"] = url.password
    return environment


def build_restore_plan(
    *,
    source_url: str,
    admin_url: str,
    restore_database: str,
    artifact: str,
) -> RestorePlan:
    if not _ISOLATED_DATABASE.fullmatch(restore_database):
        raise RestoreSafetyError("isolated_restore_database_required")
    artifact_path = Path(artifact)
    if not artifact_path.is_absolute():
        raise RestoreSafetyError("absolute_restore_artifact_required")
    source = make_url(source_url)
    admin = make_url(admin_url)
    return RestorePlan(
        restore_database=restore_database,
        dump_command=(
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--file",
            str(artifact_path),
        ),
        create_command=(
            "createdb",
            "--encoding=UTF8",
            "--template=template0",
            restore_database,
        ),
        restore_command=(
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--dbname",
            restore_database,
            str(artifact_path),
        ),
        source_environment=_pg_environment(source),
        admin_environment=_pg_environment(admin),
        restore_environment=_pg_environment(admin, restore_database),
    )


def assert_integrity(expected: RestoreManifest, restored: RestoreManifest) -> None:
    if expected != restored:
        raise RestoreSafetyError("restore_integrity_mismatch")
