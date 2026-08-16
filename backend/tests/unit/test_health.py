from pathlib import Path

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.core.config import Settings
from app.main import create_app


def test_test_settings_supply_all_required_secrets() -> None:
    settings = Settings.for_test()
    assert settings.environment == "test"
    assert settings.database_url.startswith("postgresql+psycopg://")


def test_settings_load_an_isolated_environment_file(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    environment_file = tmp_path / "settings.env"
    environment_file.write_text(
        """DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/sourcing
REDIS_URL=redis://localhost:6379/0
OBJECT_STORE_ENDPOINT=http://localhost:9000
OIDC_ISSUER=https://issuer.example.com/
OIDC_AUDIENCE=sourcing-api
APOLLO_API_KEY=development-apollo-key
CONTACT_ENCRYPTION_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
SUPPRESSION_HMAC_KEY=development-suppression-key
WEBHOOK_HMAC_KEY=development-webhook-key
"""
    )
    for name in (
        "DATABASE_URL",
        "REDIS_URL",
        "OBJECT_STORE_ENDPOINT",
        "OIDC_ISSUER",
        "OIDC_AUDIENCE",
        "APOLLO_API_KEY",
        "CONTACT_ENCRYPTION_KEY",
        "SUPPRESSION_HMAC_KEY",
        "WEBHOOK_HMAC_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=environment_file)

    assert settings.database_url == "postgresql+psycopg://postgres:postgres@localhost:5432/sourcing"


def test_health_reports_ready() -> None:
    client = TestClient(create_app(Settings.for_test()))
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
