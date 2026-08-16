import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from app.core.config import LifecycleAdminSettings, Settings
from app.maintenance_worker import celery_app as maintenance_celery_app
from app.providers import snapshot_lifecycle_cli
from app.providers.base import (
    ProviderAuthenticationError,
    ProviderRateLimited,
    ProviderTemporaryError,
)
from app.sourcing import maintenance_tasks, tasks
from app.sourcing.maintenance_tasks import (
    expire_contact_points,
    reconcile_expired_snapshots,
)
from app.sourcing.tasks import (
    _provider_retry_countdown,
    enrich_request,
    enrich_run,
    match_run,
    plan_run,
    poll_enrichment_result,
    source_run,
)
from app.worker import celery_app


def test_clean_worker_process_registers_sourcing_tasks() -> None:
    backend_root = Path(__file__).resolve().parents[3]
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app.worker import celery_app; "
                "required={'sourcing.plan_run','sourcing.source_run',"
                "'sourcing.match_run','sourcing.enrich_run','sourcing.enrich_request',"
                "'sourcing.poll_enrichment_result'}; "
                "missing=required-set(celery_app.tasks); "
                "forbidden={'maintenance.reconcile_expired_snapshots',"
                "'maintenance.expire_contact_points',"
                "'sourcing.configure_snapshot_lifecycle'}; "
                "assert not missing, sorted(missing); "
                "assert not forbidden.intersection(celery_app.tasks)"
            ),
        ],
        cwd=backend_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr


def test_clean_maintenance_worker_registers_only_maintenance_tasks() -> None:
    backend_root = Path(__file__).resolve().parents[3]
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app.maintenance_worker import celery_app; "
                "required={'maintenance.reconcile_expired_snapshots',"
                "'maintenance.expire_contact_points'}; "
                "missing=required-set(celery_app.tasks); "
                "assert not missing, sorted(missing); "
                "assert 'sourcing.plan_run' not in celery_app.tasks"
            ),
        ],
        cwd=backend_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr


def test_worker_acknowledges_after_commit_and_limits_prefetch() -> None:
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_acks_on_failure_or_timeout is False
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]


def test_sourcing_tasks_use_late_acknowledgement_and_bounded_retries() -> None:
    for task in (
        plan_run,
        source_run,
        match_run,
        enrich_run,
        enrich_request,
        poll_enrichment_result,
    ):
        assert task.acks_late is True
        assert task.reject_on_worker_lost is True
        assert task.max_retries == 5
        assert OperationalError in task.autoretry_for
    assert source_run.retry_backoff is True
    assert source_run.retry_jitter is True


def test_maintenance_tasks_are_isolated_on_a_dedicated_worker_and_queue() -> None:
    assert celery_app.conf.beat_schedule == {}
    assert "maintenance.reconcile_expired_snapshots" in maintenance_celery_app.tasks
    assert "maintenance.expire_contact_points" in maintenance_celery_app.tasks
    assert "sourcing.plan_run" not in maintenance_celery_app.tasks

    entry = maintenance_celery_app.conf.beat_schedule[
        "snapshot-reference-reconciliation"
    ]

    assert entry["task"] == reconcile_expired_snapshots.name
    assert str(entry["schedule"]) == "<crontab: 0 2 * * * (m/h/dM/MY/d)>"

    contact_entry = maintenance_celery_app.conf.beat_schedule[
        "contact-point-expiration"
    ]
    assert contact_entry["task"] == expire_contact_points.name
    assert str(contact_entry["schedule"]) == "<crontab: 15 2 * * * (m/h/dM/MY/d)>"
    assert maintenance_celery_app.conf.task_routes == {
        "maintenance.*": {"queue": "maintenance"}
    }


def test_maintenance_tasks_use_only_narrow_maintenance_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []

    settings = SimpleNamespace(
        maintenance_database_url="postgresql://maintenance-only",
    )
    monkeypatch.setattr(maintenance_tasks, "get_maintenance_settings", lambda: settings)
    monkeypatch.setattr(
        maintenance_tasks,
        "_run_contact_expiry",
        lambda url: opened.append(url),
    )
    monkeypatch.setattr(
        maintenance_tasks,
        "_run_snapshot_reconciliation",
        lambda value: opened.append(value.maintenance_database_url),
    )

    reconcile_expired_snapshots.run()
    expire_contact_points.run()

    assert opened == [settings.maintenance_database_url] * 2


def test_provider_runtime_dependency_setup_has_no_bucket_admin_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    class RuntimeObjectStore:
        def put_bucket_lifecycle_configuration(self, **kwargs: object) -> None:
            del kwargs
            raise AssertionError("provider runtime attempted bucket administration")

    def build_client(*args: object, **kwargs: object) -> RuntimeObjectStore:
        del args
        received.update(kwargs)
        return RuntimeObjectStore()

    monkeypatch.setattr("boto3.client", build_client)

    settings = Settings.for_test()
    tasks._enrichment_dependencies(settings)

    assert received["aws_access_key_id"] == "test-writer-key"
    assert received["aws_secret_access_key"] == "test-writer-secret"


def test_daily_maintenance_worker_uses_delete_only_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}

    class DeleteOnlyObjectStore:
        def delete_object(self, **kwargs: object) -> None:
            del kwargs

    class EmptySession:
        def __init__(self, engine: object) -> None:
            del engine

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def execute(self, *args: object, **kwargs: object) -> SimpleNamespace:
            del args, kwargs
            return SimpleNamespace(all=list)

        def commit(self) -> None:
            return None

    class Engine:
        def dispose(self) -> None:
            return None

    def build_client(*args: object, **kwargs: object) -> DeleteOnlyObjectStore:
        del args
        received.update(kwargs)
        return DeleteOnlyObjectStore()

    monkeypatch.setattr("boto3.client", build_client)
    monkeypatch.setattr(
        maintenance_tasks, "create_engine", lambda *args, **kwargs: Engine()
    )
    monkeypatch.setattr(maintenance_tasks, "Session", EmptySession)

    maintenance_tasks._run_snapshot_reconciliation(
        maintenance_tasks.MaintenanceSettings.for_test()
    )

    assert received["aws_access_key_id"] == "test-delete-key"
    assert received["aws_secret_access_key"] == "test-delete-secret"
    assert not hasattr(DeleteOnlyObjectStore(), "put_bucket_lifecycle_configuration")


def test_one_shot_lifecycle_cli_uses_only_bucket_admin_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}
    configured: list[tuple[object, str]] = []
    client = object()

    def build_client(*args: object, **kwargs: object) -> object:
        del args
        received.update(kwargs)
        return client

    settings = LifecycleAdminSettings.for_test()
    monkeypatch.setattr("boto3.client", build_client)
    monkeypatch.setattr(
        snapshot_lifecycle_cli,
        "get_lifecycle_admin_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        snapshot_lifecycle_cli,
        "configure_snapshot_lifecycle",
        lambda value, bucket: configured.append((value, bucket)),
    )

    snapshot_lifecycle_cli.main()

    assert received["aws_access_key_id"] == "test-lifecycle-key"
    assert received["aws_secret_access_key"] == "test-lifecycle-secret"
    assert configured == [(client, settings.object_store_bucket)]


def test_provider_retry_uses_reset_time_or_exponential_jitter() -> None:
    assert (
        _provider_retry_countdown(
            ProviderRateLimited(17), retries=3, jitter=lambda upper: upper
        )
        == 17
    )
    assert (
        _provider_retry_countdown(
            ProviderTemporaryError("temporary"),
            retries=2,
            jitter=lambda upper: upper,
        )
        == 8
    )


def test_nonretryable_provider_failure_marks_run_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    marked: list[tuple[object, object, str]] = []

    monkeypatch.setattr(tasks, "get_settings", lambda: object())

    def fail_source(*args: object, **kwargs: object) -> None:
        raise ProviderAuthenticationError("provider authentication failed")

    monkeypatch.setattr(tasks, "execute_source_run", fail_source)
    monkeypatch.setattr(
        tasks,
        "_mark_source_retry_exhausted",
        lambda failed_run, context, key: marked.append((failed_run, context, key)),
    )
    monkeypatch.setattr(tasks, "_run_is_match_eligible", lambda *args: False)

    source_run.run(str(run_id), str(tenant_id), str(user_id), "source")

    assert [(entry[0], entry[1].tenant_id, entry[2]) for entry in marked] == [
        (run_id, tenant_id, "source")
    ]


def test_source_wrapper_dispatches_match_for_eligible_partial_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    dispatched: list[tuple[str, str, str, str]] = []

    monkeypatch.setattr(tasks, "get_settings", lambda: object())
    monkeypatch.setattr(tasks, "execute_source_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks, "_run_is_match_eligible", lambda *args: True)
    monkeypatch.setattr(
        tasks.match_run,
        "delay",
        lambda *args: dispatched.append(args),
    )

    source_run.run(str(run_id), str(tenant_id), str(user_id), "source")

    assert dispatched == [(str(run_id), str(tenant_id), str(user_id), "match")]


def test_match_wrapper_dispatches_top_fifty_enrichment_when_eligible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    dispatched: list[tuple[str, str, str, int]] = []

    monkeypatch.setattr(tasks, "execute_match_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks, "_run_is_enrich_eligible", lambda *args: True)
    monkeypatch.setattr(
        tasks.enrich_run,
        "delay",
        lambda *args: dispatched.append(args),
    )

    match_run.run(str(run_id), str(tenant_id), str(user_id), "match")

    assert dispatched == [(str(run_id), str(tenant_id), str(user_id), 50)]
