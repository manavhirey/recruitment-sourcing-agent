import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import (
    LifecycleAdminSettings,
    MaintenanceSettings,
    MigrationSettings,
    Settings,
)
from app.main import create_app


def test_test_settings_supply_all_required_secrets() -> None:
    settings = Settings.for_test()
    assert settings.environment == "test"
    assert make_url(settings.database_url).username == "sourcing_api_test"
    assert not hasattr(settings, "migration_database_url")
    assert not hasattr(settings, "maintenance_database_url")
    assert not hasattr(settings, "object_store_delete_secret_access_key")
    assert not hasattr(settings, "object_store_lifecycle_admin_secret_access_key")


def test_object_store_capability_settings_expose_only_their_own_credentials() -> None:
    runtime = Settings.for_test()
    maintenance = MaintenanceSettings.for_test()
    lifecycle = LifecycleAdminSettings.for_test()

    assert hasattr(runtime, "object_store_writer_secret_access_key")
    assert not hasattr(runtime, "object_store_delete_secret_access_key")
    assert not hasattr(runtime, "object_store_lifecycle_admin_secret_access_key")
    assert hasattr(maintenance, "object_store_delete_secret_access_key")
    assert not hasattr(maintenance, "object_store_writer_secret_access_key")
    assert not hasattr(maintenance, "object_store_lifecycle_admin_secret_access_key")
    assert hasattr(lifecycle, "object_store_lifecycle_admin_secret_access_key")
    assert not hasattr(lifecycle, "object_store_writer_secret_access_key")
    assert not hasattr(lifecycle, "object_store_delete_secret_access_key")


def test_object_store_access_key_identities_must_be_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OBJECT_STORE_DELETE_ACCESS_KEY_ID", "shared-key")
    monkeypatch.setenv("OBJECT_STORE_LIFECYCLE_ADMIN_ACCESS_KEY_ID", "lifecycle-key")
    payload = Settings.for_test().model_dump()
    payload["object_store_writer_access_key_id"] = "shared-key"

    with pytest.raises(ValidationError, match="object-store access-key identities"):
        Settings(_env_file=None, **payload)


def test_clean_process_capability_settings_are_disjoint() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    capabilities = (
        ("Settings", "OBJECT_STORE_WRITER_SECRET_ACCESS_KEY"),
        ("MaintenanceSettings", "OBJECT_STORE_DELETE_SECRET_ACCESS_KEY"),
        (
            "LifecycleAdminSettings",
            "OBJECT_STORE_LIFECYCLE_ADMIN_SECRET_ACCESS_KEY",
        ),
    )
    secret_names = {secret for _, secret in capabilities}
    for model_name, own_secret in capabilities:
        probe_environment = os.environ.copy()
        for secret_name in secret_names:
            probe_environment.pop(secret_name, None)
        probe_environment[own_secret] = "process-specific-secret"
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from app.core.config import LifecycleAdminSettings, "
                    "MaintenanceSettings, Settings; "
                    f"settings={model_name}(); "
                    f"assert '{own_secret.lower()}' in settings.model_fields; "
                    f"assert not set({sorted(secret.lower() for secret in secret_names - {own_secret})!r}) "
                    ".intersection(settings.model_fields)"
                ),
            ],
            cwd=backend_root,
            env=probe_environment,
            capture_output=True,
            text=True,
            check=False,
        )

        assert probe.returncode == 0, probe.stderr


def test_runtime_settings_do_not_require_schema_owner_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = Settings.for_test().model_dump()
    monkeypatch.delenv("MIGRATION_DATABASE_URL")

    settings = Settings(_env_file=None, **payload)

    assert not hasattr(settings, "migration_database_url")


def test_migration_database_url_must_use_a_distinct_role() -> None:
    runtime = Settings.for_test()
    payload = {
        "database_url": runtime.database_url,
        "migration_database_url": runtime.database_url,
        "maintenance_database_url": MaintenanceSettings.for_test().maintenance_database_url,
    }

    with pytest.raises(ValidationError, match="database roles must be distinct"):
        MigrationSettings.model_validate(payload)


def test_maintenance_database_url_is_required_only_by_maintenance_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MAINTENANCE_DATABASE_URL")

    with pytest.raises(ValidationError, match="maintenance_database_url"):
        MaintenanceSettings(
            _env_file=None,
            redis_url="redis://localhost/15",
            object_store_endpoint="http://localhost:9000",
            object_store_delete_access_key_id="delete",
            object_store_delete_secret_access_key="secret",
        )

    assert not hasattr(Settings.for_test(), "maintenance_database_url")


def test_maintenance_database_url_must_use_a_distinct_api_role() -> None:
    payload = MaintenanceSettings.for_test().model_dump()
    payload["maintenance_database_url"] = Settings.for_test().database_url

    with pytest.raises(ValidationError, match="dedicated maintenance role"):
        MaintenanceSettings.model_validate(payload)


def test_maintenance_database_url_must_differ_from_migration_role() -> None:
    runtime = Settings.for_test()
    maintenance = MaintenanceSettings.for_test()
    payload = {
        "database_url": runtime.database_url,
        "migration_database_url": maintenance.maintenance_database_url,
        "maintenance_database_url": maintenance.maintenance_database_url,
    }

    with pytest.raises(ValidationError, match="database roles must be distinct"):
        MigrationSettings.model_validate(payload)


def test_app_loads_the_project_root_env_file_from_backend(tmp_path: Path) -> None:
    source_backend = Path(__file__).resolve().parents[2]
    isolated_backend = tmp_path / "backend"
    shutil.copytree(source_backend / "app", isolated_backend / "app")

    environment_file = tmp_path / ".env"
    environment_file.write_text(
        """DATABASE_URL=postgresql+psycopg://sourcing_api:api-password@localhost:5432/sourcing
MIGRATION_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/sourcing
MAINTENANCE_DATABASE_URL=postgresql+psycopg://sourcing_maintenance:maintenance-password@localhost:5432/sourcing
REDIS_URL=redis://localhost:6379/0
OBJECT_STORE_ENDPOINT=http://localhost:9000
OBJECT_STORE_WRITER_ACCESS_KEY_ID=writer-key
OBJECT_STORE_WRITER_SECRET_ACCESS_KEY=writer-secret
OBJECT_STORE_DELETE_ACCESS_KEY_ID=delete-key
OBJECT_STORE_DELETE_SECRET_ACCESS_KEY=delete-secret
OBJECT_STORE_LIFECYCLE_ADMIN_ACCESS_KEY_ID=lifecycle-key
OBJECT_STORE_LIFECYCLE_ADMIN_SECRET_ACCESS_KEY=lifecycle-secret
OIDC_ISSUER=https://issuer.example.com/
OIDC_AUDIENCE=sourcing-api
OPENAI_API_KEY=development-openai-key
APOLLO_API_KEY=development-apollo-key
CONTACT_ENCRYPTION_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
SUPPRESSION_HMAC_KEY=development-suppression-key
WEBHOOK_HMAC_KEY=development-webhook-key
"""
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.main import app; print(app.state.settings.database_url)",
        ],
        capture_output=True,
        check=False,
        cwd=isolated_backend,
        env={},
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (
        result.stdout
        == "postgresql+psycopg://sourcing_api:api-password@localhost:5432/sourcing\n"
    )


def test_health_reports_ready() -> None:
    client = TestClient(create_app(Settings.for_test()))
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
