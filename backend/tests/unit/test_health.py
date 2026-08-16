import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Self

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import (
    LifecycleAdminSettings,
    MaintenanceSettings,
    MigrationSettings,
    SchedulerSettings,
    Settings,
    WorkerSettings,
)
from app.main import (
    _dispatch_enrichment_request,
    _dispatch_privacy_request,
    _dispatch_sourcing_run,
    _production_readiness_checks,
    create_app,
)


def test_test_settings_supply_all_required_secrets() -> None:
    settings = Settings.for_test()
    assert settings.environment == "test"
    assert make_url(settings.database_url).username == "sourcing_api_test"
    assert not hasattr(settings, "migration_database_url")
    assert not hasattr(settings, "maintenance_database_url")
    assert not hasattr(settings, "object_store_delete_secret_access_key")
    assert not hasattr(settings, "object_store_lifecycle_admin_secret_access_key")
    assert not hasattr(settings, "apollo_api_key")
    worker = WorkerSettings.for_test()
    assert hasattr(worker, "apollo_api_key")
    assert not hasattr(worker, "oidc_issuer")
    assert not hasattr(worker, "openai_api_key")
    assert (
        settings.identity_hmac_key.get_secret_value()
        != settings.suppression_hmac_key.get_secret_value()
    )
    assert settings.identity_hmac_key_version == "v1"
    assert SchedulerSettings(
        _env_file=None, environment="test", redis_url="redis://broker/0"
    ).model_dump() == {"environment": "test", "redis_url": "redis://broker/0"}


def _production_runtime_payload() -> dict[str, object]:
    payload = Settings.for_test().model_dump()
    payload.update(
        environment="production",
        database_url=(
            "postgresql+psycopg://sourcing_api:"
            "api-password-with-32-random-characters@db.internal:5432/sourcing"
            "?sslmode=verify-full"
        ),
        redis_url=(
            "rediss://:redis-password-with-32-random-characters@redis.internal:6379/0"
        ),
        object_store_endpoint="https://objects.internal",
        object_store_writer_access_key_id="writer-access-identity-2026",
        object_store_writer_secret_access_key="writer-secret-with-32-random-characters",
        oidc_issuer="https://identity.company.com/",
        openai_api_key="sk-live-with-32-random-characters-2026",
        contact_encryption_key="QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI=",
        suppression_hmac_key="suppression-with-32-random-characters",
        identity_hmac_key="identity-with-32-random-characters-2026",
        telemetry_hmac_key="telemetry-with-32-random-characters-2026",
        webhook_hmac_key="webhook-with-32-random-characters-2026",
        webhook_base_url="https://api.company.com",
    )
    return payload


def _production_worker_payload() -> dict[str, object]:
    payload = WorkerSettings.for_test().model_dump()
    payload.update(_production_runtime_payload())
    payload.pop("oidc_issuer", None)
    payload.pop("oidc_audience", None)
    payload.pop("openai_api_key", None)
    payload.pop("scorecard_model", None)
    payload["apollo_api_key"] = "apollo-with-32-random-characters-2026"
    return payload


def _production_maintenance_payload() -> dict[str, object]:
    payload = MaintenanceSettings.for_test().model_dump()
    payload.update(
        environment="production",
        maintenance_database_url=(
            "postgresql+psycopg://sourcing_maintenance:"
            "maintenance-password-with-32-random@db.internal:5432/sourcing"
            "?sslmode=verify-full"
        ),
        redis_url=(
            "rediss://:maintenance-redis-password-32-random@redis.internal:6379/0"
        ),
        object_store_endpoint="https://objects.internal",
        object_store_delete_access_key_id="delete-access-identity-2026",
        object_store_delete_secret_access_key="delete-secret-with-32-random-characters",
        telemetry_hmac_key="maintenance-telemetry-32-random-characters",
    )
    return payload


def _production_lifecycle_payload() -> dict[str, object]:
    return {
        "environment": "production",
        "object_store_endpoint": "https://objects.internal",
        "object_store_lifecycle_admin_access_key_id": (
            "lifecycle-access-identity-2026"
        ),
        "object_store_lifecycle_admin_secret_access_key": (
            "lifecycle-secret-with-32-random-characters"
        ),
    }


def _production_scheduler_payload() -> dict[str, object]:
    return {
        "environment": "production",
        "redis_url": (
            "rediss://:scheduler-redis-password-32-random@redis.internal:6379/0"
        ),
    }


def _production_migration_payload() -> dict[str, object]:
    suffix = "@db.internal:5432/sourcing?sslmode=verify-full"
    return {
        "environment": "production",
        "database_url": (
            "postgresql+psycopg://sourcing_api:api-password-with-32-random" + suffix
        ),
        "migration_database_url": (
            "postgresql+psycopg://sourcing_migration:"
            "migration-password-with-32-random" + suffix
        ),
        "maintenance_database_url": (
            "postgresql+psycopg://sourcing_maintenance:"
            "maintenance-password-with-32-random" + suffix
        ),
    }


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (Settings, _production_runtime_payload()),
        (WorkerSettings, _production_worker_payload()),
        (MaintenanceSettings, _production_maintenance_payload()),
        (LifecycleAdminSettings, _production_lifecycle_payload()),
        (SchedulerSettings, _production_scheduler_payload()),
        (MigrationSettings, _production_migration_payload()),
    ],
)
def test_production_settings_accept_strong_distinct_secure_configuration(
    model: type[
        Settings
        | WorkerSettings
        | MaintenanceSettings
        | LifecycleAdminSettings
        | SchedulerSettings
        | MigrationSettings
    ],
    payload: dict[str, object],
) -> None:
    model(_env_file=None, **payload)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (Settings, _production_runtime_payload()),
        (WorkerSettings, _production_worker_payload()),
        (MaintenanceSettings, _production_maintenance_payload()),
        (LifecycleAdminSettings, _production_lifecycle_payload()),
        (SchedulerSettings, _production_scheduler_payload()),
        (MigrationSettings, _production_migration_payload()),
    ],
)
def test_environment_is_a_fail_closed_enum(
    model: object, payload: dict[str, object]
) -> None:
    payload["environment"] = "prod"

    with pytest.raises(ValidationError, match="environment"):
        model(_env_file=None, **payload)  # type: ignore[operator]


def test_production_database_requires_tls() -> None:
    payload = _production_runtime_payload()
    payload["database_url"] = str(payload["database_url"]).split("?", 1)[0]

    with pytest.raises(ValidationError, match="TLS"):
        Settings(_env_file=None, **payload)


@pytest.mark.parametrize(
    ("model", "payload", "field", "unsafe_value"),
    [
        (Settings, _production_runtime_payload(), "suppression_hmac_key", "short"),
        (
            Settings,
            _production_runtime_payload(),
            "identity_hmac_key",
            "suppression-with-32-random-characters",
        ),
        (
            Settings,
            _production_runtime_payload(),
            "object_store_endpoint",
            "http://objects.internal",
        ),
        (
            Settings,
            _production_runtime_payload(),
            "database_url",
            (
                "postgresql+psycopg://sourcing_api:"
                "api-password-with-32-random-characters@localhost:5432/sourcing"
            ),
        ),
        (
            WorkerSettings,
            _production_worker_payload(),
            "apollo_api_key",
            "replace-with-production-key",
        ),
        (
            WorkerSettings,
            _production_worker_payload(),
            "contact_encryption_key",
            "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!",
        ),
        (
            MaintenanceSettings,
            _production_maintenance_payload(),
            "redis_url",
            "redis://redis.internal:6379/0",
        ),
        (
            LifecycleAdminSettings,
            _production_lifecycle_payload(),
            "object_store_endpoint",
            "http://objects.internal",
        ),
        (
            SchedulerSettings,
            _production_scheduler_payload(),
            "redis_url",
            "redis://redis.internal:6379/0",
        ),
        (
            MigrationSettings,
            _production_migration_payload(),
            "migration_database_url",
            (
                "postgresql+psycopg://sourcing_migration:"
                "migration-password-with-32-random@db.internal:5432/sourcing"
            ),
        ),
    ],
)
def test_production_settings_reject_weak_reused_placeholder_or_insecure_values(
    model: type[
        Settings
        | WorkerSettings
        | MaintenanceSettings
        | LifecycleAdminSettings
        | SchedulerSettings
        | MigrationSettings
    ],
    payload: dict[str, object],
    field: str,
    unsafe_value: str,
) -> None:
    payload[field] = unsafe_value

    with pytest.raises(ValidationError) as caught:
        model(_env_file=None, **payload)

    assert unsafe_value not in str(caught.value)


def test_production_migration_database_credentials_are_distinct() -> None:
    payload = _production_migration_payload()
    api_password = make_url(str(payload["database_url"])).password
    payload["migration_database_url"] = (
        "postgresql+psycopg://sourcing_migration:"
        f"{api_password}@db.internal:5432/sourcing?sslmode=verify-full"
    )

    with pytest.raises(ValidationError, match="credentials must be distinct"):
        MigrationSettings(_env_file=None, **payload)


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
TELEMETRY_HMAC_KEY=development-telemetry-key
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


def test_readiness_fails_closed_without_leaking_dependency_errors() -> None:
    def unavailable() -> bool:
        raise RuntimeError("postgresql://user:secret@database/private")

    client = TestClient(
        create_app(
            Settings.for_test(),
            readiness_checks={"database": unavailable, "broker": lambda: True},
        )
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unavailable",
        "components": {"broker": "ready", "database": "unavailable"},
    }
    assert "secret" not in response.text


def test_production_readiness_probes_database_broker_and_object_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    class Connection:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def scalar(self, statement: object) -> int:
            observed.append("database_query")
            return 1

    class Engine:
        def connect(self) -> Connection:
            observed.append("database_connect")
            return Connection()

        def dispose(self) -> None:
            observed.append("database_dispose")

    class Broker:
        def ping(self) -> bool:
            observed.append("broker_ping")
            return True

        def close(self) -> None:
            observed.append("broker_close")

    class ObjectStore:
        def head_bucket(self, *, Bucket: str) -> None:
            observed.append(f"object_store:{Bucket}")

    monkeypatch.setattr("sqlalchemy.create_engine", lambda *args, **kwargs: Engine())
    monkeypatch.setattr("app.main.Redis.from_url", lambda *args, **kwargs: Broker())
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: ObjectStore())
    settings = Settings.for_test().model_copy(update={"environment": "production"})

    checks = _production_readiness_checks(settings)

    assert sorted(checks) == ["broker", "database", "object_store"]
    assert all(check() for check in checks.values())
    assert observed == [
        "broker_ping",
        "broker_close",
        "database_connect",
        "database_query",
        "database_dispose",
        "object_store:provider-snapshots",
    ]


def test_production_dispatchers_preserve_durable_task_identity_and_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4

    from app.sourcing.tasks import enrich_request, plan_run
    from app.worker import celery_app

    observed: list[tuple[str, object]] = []
    monkeypatch.setattr(
        plan_run,
        "apply_async",
        lambda **kwargs: observed.append(("source", kwargs)),
    )
    monkeypatch.setattr(
        enrich_request,
        "apply_async",
        lambda **kwargs: observed.append(("enrich", kwargs)),
    )
    monkeypatch.setattr(
        celery_app,
        "send_task",
        lambda *args, **kwargs: observed.append(("privacy", (args, kwargs))),
    )
    identifier, tenant_id, user_id = uuid4(), uuid4(), uuid4()

    _dispatch_sourcing_run(identifier, tenant_id, user_id, "source-task-key")
    _dispatch_enrichment_request(identifier, tenant_id, user_id, "enrich-task-key")
    _dispatch_privacy_request(identifier, tenant_id)

    assert observed == [
        (
            "source",
            {
                "args": (
                    str(identifier),
                    str(tenant_id),
                    str(user_id),
                    "plan",
                ),
                "task_id": "source-task-key",
            },
        ),
        (
            "enrich",
            {
                "args": (str(identifier), str(tenant_id), str(user_id)),
                "task_id": "enrich-task-key",
            },
        ),
        (
            "privacy",
            (
                ("maintenance.execute_privacy_deletion",),
                {
                    "args": (str(identifier), str(tenant_id)),
                    "queue": "maintenance",
                },
            ),
        ),
    ]


def test_compose_processes_receive_only_their_required_capabilities() -> None:
    compose_path = Path(__file__).resolve().parents[3] / "compose.yaml"
    services = yaml.safe_load(compose_path.read_text())["services"]
    forbidden = {
        "api": {
            "APOLLO_API_KEY",
            "MIGRATION_DATABASE_URL",
            "MAINTENANCE_DATABASE_URL",
            "OBJECT_STORE_DELETE_SECRET_ACCESS_KEY",
            "OBJECT_STORE_LIFECYCLE_ADMIN_SECRET_ACCESS_KEY",
            "POSTGRES_PASSWORD",
            "MINIO_ROOT_PASSWORD",
        },
        "worker": {
            "OIDC_ISSUER",
            "OIDC_AUDIENCE",
            "OPENAI_API_KEY",
            "MIGRATION_DATABASE_URL",
            "MAINTENANCE_DATABASE_URL",
            "OBJECT_STORE_DELETE_SECRET_ACCESS_KEY",
            "OBJECT_STORE_LIFECYCLE_ADMIN_SECRET_ACCESS_KEY",
            "POSTGRES_PASSWORD",
            "MINIO_ROOT_PASSWORD",
        },
        "maintenance-worker": {
            "DATABASE_URL",
            "MIGRATION_DATABASE_URL",
            "OBJECT_STORE_WRITER_SECRET_ACCESS_KEY",
            "OBJECT_STORE_LIFECYCLE_ADMIN_SECRET_ACCESS_KEY",
            "APOLLO_API_KEY",
            "OPENAI_API_KEY",
            "CONTACT_ENCRYPTION_KEY",
            "SUPPRESSION_HMAC_KEY",
            "POSTGRES_PASSWORD",
            "MINIO_ROOT_PASSWORD",
        },
        "scheduler": {
            "DATABASE_URL",
            "MIGRATION_DATABASE_URL",
            "MAINTENANCE_DATABASE_URL",
            "OBJECT_STORE_WRITER_SECRET_ACCESS_KEY",
            "OBJECT_STORE_DELETE_SECRET_ACCESS_KEY",
            "OBJECT_STORE_LIFECYCLE_ADMIN_SECRET_ACCESS_KEY",
            "APOLLO_API_KEY",
            "OPENAI_API_KEY",
            "CONTACT_ENCRYPTION_KEY",
            "SUPPRESSION_HMAC_KEY",
            "POSTGRES_PASSWORD",
            "MINIO_ROOT_PASSWORD",
        },
        "web": {
            "DATABASE_URL",
            "MIGRATION_DATABASE_URL",
            "MAINTENANCE_DATABASE_URL",
            "OBJECT_STORE_WRITER_SECRET_ACCESS_KEY",
            "OBJECT_STORE_DELETE_SECRET_ACCESS_KEY",
            "OBJECT_STORE_LIFECYCLE_ADMIN_SECRET_ACCESS_KEY",
            "APOLLO_API_KEY",
            "OPENAI_API_KEY",
            "CONTACT_ENCRYPTION_KEY",
            "SUPPRESSION_HMAC_KEY",
            "TELEMETRY_HMAC_KEY",
            "WEBHOOK_HMAC_KEY",
            "POSTGRES_PASSWORD",
            "MINIO_ROOT_PASSWORD",
        },
    }

    for service_name, forbidden_names in forbidden.items():
        service = services[service_name]
        assert "env_file" not in service
        assert not forbidden_names.intersection(service.get("environment", {}))

    assert "DATABASE_URL" in services["api"]["environment"]
    assert "DATABASE_URL" in services["worker"]["environment"]
    assert "MAINTENANCE_DATABASE_URL" in services["maintenance-worker"]["environment"]
    assert "AUTH_SECRET" in services["web"]["environment"]
    for data_service in ("postgres", "redis", "minio", "prometheus"):
        assert "ports" not in services[data_service]
    assert services["scheduler"]["environment"] == {
        "ENVIRONMENT": "${ENVIRONMENT:-production}",
        "REDIS_URL": "${COMPOSE_REDIS_URL}",
    }
    assert services["api"]["environment"]["DATABASE_URL"] == ("${COMPOSE_DATABASE_URL}")
    assert services["api"]["environment"]["OBJECT_STORE_ENDPOINT"] == (
        "${COMPOSE_OBJECT_STORE_ENDPOINT}"
    )
    assert "--no-access-log" in services["api"]["command"]


def test_rendered_compose_preserves_capability_and_port_isolation() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker is required to resolve the production Compose model")
    repository = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(repository / ".env.example"),
            "--profile",
            "application",
            "--profile",
            "observability",
            "-f",
            str(repository / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(result.stdout)["services"]

    assert "APOLLO_API_KEY" not in services["api"]["environment"]
    assert {
        "OIDC_ISSUER",
        "OIDC_AUDIENCE",
        "OPENAI_API_KEY",
    }.isdisjoint(services["worker"]["environment"])
    assert (
        "OBJECT_STORE_WRITER_SECRET_ACCESS_KEY"
        not in services["maintenance-worker"]["environment"]
    )
    assert set(services["scheduler"]["environment"]) == {
        "ENVIRONMENT",
        "REDIS_URL",
    }
    for service_name in ("postgres", "redis", "minio", "prometheus"):
        assert "ports" not in services[service_name]
