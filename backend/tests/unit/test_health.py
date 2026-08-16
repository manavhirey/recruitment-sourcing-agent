import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_test_settings_supply_all_required_secrets() -> None:
    settings = Settings.for_test()
    assert settings.environment == "test"
    assert settings.database_url.startswith("postgresql+psycopg://")


def test_app_loads_root_env_file_when_launched_from_backend() -> None:
    environment_file = Path(__file__).resolve().parents[3] / ".env"
    previous_contents = environment_file.read_bytes() if environment_file.exists() else None
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
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from app.main import app; print(app.state.settings.database_url)",
            ],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            check=False,
            env={},
            text=True,
        )
    finally:
        if previous_contents is None:
            environment_file.unlink(missing_ok=True)
        else:
            environment_file.write_bytes(previous_contents)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "postgresql+psycopg://postgres:postgres@localhost:5432/sourcing\n"


def test_health_reports_ready() -> None:
    client = TestClient(create_app(Settings.for_test()))
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
