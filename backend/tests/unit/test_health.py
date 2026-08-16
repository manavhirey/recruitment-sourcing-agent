from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_test_settings_supply_all_required_secrets() -> None:
    settings = Settings.for_test()
    assert settings.environment == "test"
    assert settings.database_url.startswith("postgresql+psycopg://")


def test_health_reports_ready() -> None:
    client = TestClient(create_app(Settings.for_test()))
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
