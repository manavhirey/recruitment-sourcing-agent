import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import MigrationSettings, Settings
from app.main import create_app


def test_test_settings_supply_all_required_secrets() -> None:
    settings = Settings.for_test()
    assert settings.environment == "test"
    assert make_url(settings.database_url).username == "sourcing_api_test"
    assert not hasattr(settings, "migration_database_url")
    assert make_url(settings.maintenance_database_url).username == (
        "sourcing_maintenance"
    )


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
        "maintenance_database_url": runtime.maintenance_database_url,
    }

    with pytest.raises(ValidationError, match="database roles must be distinct"):
        MigrationSettings.model_validate(payload)


def test_maintenance_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = Settings.for_test().model_dump()
    payload.pop("maintenance_database_url")
    monkeypatch.delenv("MAINTENANCE_DATABASE_URL")

    with pytest.raises(ValidationError, match="maintenance_database_url"):
        Settings(_env_file=None, **payload)


def test_maintenance_database_url_must_use_a_distinct_api_role() -> None:
    payload = Settings.for_test().model_dump()
    payload["maintenance_database_url"] = payload["database_url"]

    with pytest.raises(ValidationError, match="distinct from API"):
        Settings.model_validate(payload)


def test_maintenance_database_url_must_differ_from_migration_role() -> None:
    runtime = Settings.for_test()
    payload = {
        "database_url": runtime.database_url,
        "migration_database_url": runtime.maintenance_database_url,
        "maintenance_database_url": runtime.maintenance_database_url,
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
