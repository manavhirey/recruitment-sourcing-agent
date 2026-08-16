import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from app.core.config import LifecycleAdminSettings, WorkerSettings
from app.maintenance_worker import celery_app as maintenance_celery_app
from app.privacy import tasks as privacy_tasks
from app.privacy.tasks import execute_privacy_deletion, resume_privacy_deletions
from app.providers import snapshot_lifecycle_cli
from app.providers.base import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimited,
    ProviderTemporaryError,
)
from app.sourcing import maintenance_tasks, tasks
from app.sourcing.enrichment import DeferredEnrichment, FailedEnrichment
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


def _allow_enrichment_run(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def acquired(*args: object):
        yield True

    monkeypatch.setattr(tasks, "_enrichment_execution_lock", acquired)
    monkeypatch.setattr(tasks, "_has_active_enrichment_retry", lambda *args: False)
    monkeypatch.setattr(
        tasks, "fail_active_enrichment_requests", lambda *args, **kwargs: 0
    )


def _allow_enrichment_request(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid4()

    @contextmanager
    def acquired(*args: object):
        yield True

    monkeypatch.setattr(
        tasks, "_enrichment_request_run_id", lambda *args: run_id, raising=False
    )
    monkeypatch.setattr(tasks, "_enrichment_execution_lock", acquired)


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
                "'maintenance.expire_contact_points',"
                "'maintenance.recover_sourcing_dispatches',"
                "'maintenance.execute_privacy_deletion',"
                "'maintenance.resume_privacy_deletions'}; "
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


def test_clean_scheduler_process_has_only_broker_configuration() -> None:
    backend_root = Path(__file__).resolve().parents[3]
    probe_environment = os.environ.copy()
    for name in (
        "DATABASE_URL",
        "MAINTENANCE_DATABASE_URL",
        "OBJECT_STORE_DELETE_ACCESS_KEY_ID",
        "OBJECT_STORE_DELETE_SECRET_ACCESS_KEY",
        "APOLLO_API_KEY",
    ):
        probe_environment.pop(name, None)
    probe_environment["REDIS_URL"] = "redis://scheduler-only/0"
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app.scheduler import celery_app; "
                "assert celery_app.conf.broker_url == 'redis://scheduler-only/0'; "
                "assert len(celery_app.conf.beat_schedule) == 4"
            ),
        ],
        cwd=backend_root,
        env=probe_environment,
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
    assert getattr(celery_app, "_safe_telemetry_installed", False) is True
    assert getattr(maintenance_celery_app, "_safe_telemetry_installed", False) is True


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


def test_disabled_platform_connector_short_circuits_source_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, tenant_id, user_id = uuid4(), uuid4(), uuid4()
    observed: list[str] = []
    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: False)
    monkeypatch.setattr(
        tasks,
        "execute_source_run",
        lambda *args, **kwargs: observed.append("provider_called"),
    )
    monkeypatch.setattr(
        tasks,
        "_mark_source_retry_exhausted",
        lambda *args: observed.append("run_marked"),
    )

    source_run.run(str(run_id), str(tenant_id), str(user_id))

    assert observed == ["run_marked"]


def test_provider_authentication_failure_disables_shared_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, tenant_id, user_id = uuid4(), uuid4(), uuid4()
    observed: list[tuple[str, str]] = []
    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: True)
    monkeypatch.setattr(tasks, "get_worker_settings", WorkerSettings.for_test)
    monkeypatch.setattr(
        tasks,
        "execute_source_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ProviderAuthenticationError("authentication failed")
        ),
    )
    monkeypatch.setattr(
        tasks,
        "disable_provider",
        lambda _sessions, provider, reason: observed.append((provider, reason)),
    )
    monkeypatch.setattr(tasks, "_mark_source_retry_exhausted", lambda *args: None)
    monkeypatch.setattr(tasks, "_run_is_match_eligible", lambda *args: False)

    source_run.run(str(run_id), str(tenant_id), str(user_id))

    assert observed == [("apollo", "authentication_error")]


@pytest.mark.parametrize(
    ("provider_error", "expected_disable"),
    [
        (ProviderPermissionError("permission denied"), "permission_error"),
        (ProviderError("provider failed"), None),
    ],
)
def test_source_terminal_provider_failures_mark_work_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
    provider_error: ProviderError,
    expected_disable: str | None,
) -> None:
    observed: list[object] = []
    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: True)
    monkeypatch.setattr(tasks, "get_worker_settings", WorkerSettings.for_test)

    def fail(*args: object, **kwargs: object) -> None:
        raise provider_error

    monkeypatch.setattr(tasks, "execute_source_run", fail)
    monkeypatch.setattr(
        tasks,
        "disable_provider",
        lambda _sessions, provider, reason: observed.append((provider, reason)),
    )
    monkeypatch.setattr(
        tasks,
        "_mark_source_retry_exhausted",
        lambda *args: observed.append("run_marked"),
    )
    monkeypatch.setattr(tasks, "_run_is_match_eligible", lambda *args: False)

    source_run.run(str(uuid4()), str(uuid4()), str(uuid4()))

    expected: list[object] = ["run_marked"]
    if expected_disable is not None:
        expected.insert(0, ("apollo", expected_disable))
    assert observed == expected


def test_disabled_connector_marks_enrichment_partial_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, tenant_id, user_id = uuid4(), uuid4(), uuid4()
    observed: list[str] = []
    _allow_enrichment_run(monkeypatch)
    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: False)
    monkeypatch.setattr(
        tasks,
        "_mark_enrichment_provider_disabled",
        lambda *args: observed.append("run_marked"),
    )
    monkeypatch.setattr(
        tasks,
        "ApolloGateway",
        lambda *args: observed.append("provider_called"),
    )

    enrich_run.run(str(run_id), str(tenant_id), str(user_id))

    assert observed == ["run_marked"]


def test_disabled_connector_sweeps_active_requests_before_retry_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, tenant_id, user_id = uuid4(), uuid4(), uuid4()
    claim_token = uuid4()
    observed: list[str] = []
    _allow_enrichment_run(monkeypatch)
    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: False)
    monkeypatch.setattr(
        tasks,
        "_claim_enrichment_retry_delivery",
        lambda *args: claim_token,
    )
    monkeypatch.setattr(
        tasks,
        "fail_active_enrichment_requests",
        lambda *args, **kwargs: observed.append("requests_swept") or 2,
    )
    monkeypatch.setattr(
        tasks,
        "_mark_enrichment_provider_disabled",
        lambda *args: observed.append("run_marked"),
    )
    monkeypatch.setattr(
        tasks,
        "_complete_enrichment_retry_delivery",
        lambda *args: observed.append("retry_completed") or True,
    )
    monkeypatch.setattr(
        tasks,
        "ApolloGateway",
        lambda *args: pytest.fail("disabled connector must not reach provider"),
    )

    enrich_run.run(str(run_id), str(tenant_id), str(user_id), 50, 3)

    assert observed == ["requests_swept", "run_marked", "retry_completed"]


def test_disabled_connector_fails_queued_enrichment_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id, tenant_id, user_id = uuid4(), uuid4(), uuid4()
    observed: list[str] = []
    _allow_enrichment_request(monkeypatch)
    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: False)
    monkeypatch.setattr(
        tasks,
        "_mark_enrichment_request_provider_disabled",
        lambda *args: observed.append("request_marked"),
    )
    monkeypatch.setattr(
        tasks,
        "ApolloGateway",
        lambda *args: observed.append("provider_called"),
    )

    enrich_request.run(str(request_id), str(tenant_id), str(user_id))

    assert observed == ["request_marked"]


def test_disabled_connector_stops_enrichment_poll_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id, tenant_id, user_id = uuid4(), uuid4(), uuid4()
    observed: list[str] = []
    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: False)
    monkeypatch.setattr(
        tasks,
        "_mark_enrichment_request_provider_disabled",
        lambda *args: observed.append("request_marked"),
    )
    monkeypatch.setattr(
        tasks,
        "ApolloGateway",
        lambda *args: observed.append("provider_called"),
    )

    poll_enrichment_result.run(str(request_id), str(tenant_id), str(user_id))

    assert observed == ["request_marked"]


@pytest.mark.parametrize(
    ("provider_error", "reason"),
    [
        (ProviderAuthenticationError, "authentication_error"),
        (ProviderPermissionError, "permission_error"),
    ],
)
@pytest.mark.parametrize(
    ("task", "operation", "marker"),
    [
        (enrich_run, "enqueue_top_enrichment", "_mark_enrichment_provider_disabled"),
        (
            enrich_request,
            "execute_queued_enrichment_request",
            "_mark_enrichment_request_provider_disabled",
        ),
        (
            poll_enrichment_result,
            "poll_enrichment_request",
            "_mark_enrichment_request_provider_disabled",
        ),
    ],
)
def test_enrichment_authz_failures_disable_platform_before_future_calls(
    monkeypatch: pytest.MonkeyPatch,
    task: object,
    operation: str,
    marker: str,
    provider_error: type[Exception],
    reason: str,
) -> None:
    observed: list[object] = []
    if task is enrich_run:
        _allow_enrichment_run(monkeypatch)
        monkeypatch.setattr(
            tasks,
            "fail_active_enrichment_requests",
            lambda *args, **kwargs: observed.append("requests_swept") or 1,
        )
    elif task is enrich_request:
        _allow_enrichment_request(monkeypatch)

    class Gateway:
        def close(self) -> None:
            observed.append("gateway_closed")

    def fail(*args: object, **kwargs: object) -> None:
        raise provider_error("provider denied request")

    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: True)
    monkeypatch.setattr(tasks, "get_worker_settings", WorkerSettings.for_test)
    monkeypatch.setattr(tasks, "_enrichment_dependencies", lambda *args: (None,) * 4)
    monkeypatch.setattr(tasks, "ApolloGateway", lambda *args: Gateway())
    monkeypatch.setattr(tasks, operation, fail)
    monkeypatch.setattr(
        tasks,
        "disable_provider",
        lambda _sessions, provider, disabled_reason: observed.append(
            (provider, disabled_reason)
        ),
    )
    monkeypatch.setattr(
        tasks,
        marker,
        lambda *args: observed.append("work_marked"),
    )
    identifier, tenant_id, user_id = uuid4(), uuid4(), uuid4()

    task.run(str(identifier), str(tenant_id), str(user_id))

    expected: list[object] = [("apollo", reason)]
    if task is enrich_run:
        expected.append("requests_swept")
    expected.extend(("work_marked", "gateway_closed"))
    assert observed == expected


def test_duplicate_enrichment_delivery_retries_after_the_submission_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    _allow_enrichment_request(monkeypatch)

    class RetryRequested(RuntimeError):
        pass

    class Gateway:
        def close(self) -> None:
            observed["closed"] = True

    monkeypatch.setattr(tasks, "get_worker_settings", WorkerSettings.for_test)
    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: True)
    monkeypatch.setattr(tasks, "ApolloGateway", lambda settings: Gateway())
    monkeypatch.setattr(
        tasks,
        "_enrichment_dependencies",
        lambda settings: (None, None, None, None),
    )
    monkeypatch.setattr(
        tasks,
        "execute_queued_enrichment_request",
        lambda *args, **kwargs: DeferredEnrichment(retry_after_seconds=37),
    )
    monkeypatch.setattr(
        tasks,
        "_record_provider_outcome",
        lambda endpoint, outcome: observed.update(outcome=(endpoint, outcome)),
    )

    def retry(**kwargs: object) -> None:
        observed.update(kwargs)
        raise RetryRequested

    monkeypatch.setattr(enrich_request, "retry", retry)

    with pytest.raises(RetryRequested):
        enrich_request.run(str(uuid4()), str(uuid4()), str(uuid4()))

    assert observed == {
        "closed": True,
        "countdown": 37,
        "outcome": ("people_enrichment", "retry_scheduled"),
    }


def test_enrichment_request_lock_contention_rearms_durable_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id, run_id, tenant_id, user_id = uuid4(), uuid4(), uuid4(), uuid4()
    observed: list[object] = []

    @contextmanager
    def contended(*args: object):
        yield False

    monkeypatch.setattr(
        tasks, "_enrichment_request_run_id", lambda *args: run_id, raising=False
    )
    monkeypatch.setattr(tasks, "_enrichment_execution_lock", contended)
    monkeypatch.setattr(
        tasks,
        "_requeue_enrichment_dispatch",
        lambda *args: observed.append("dispatch_rearmed") or True,
        raising=False,
    )
    monkeypatch.setattr(
        tasks,
        "_record_provider_outcome",
        lambda endpoint, outcome: observed.append((endpoint, outcome)),
    )
    monkeypatch.setattr(
        tasks,
        "is_provider_enabled",
        lambda *args: pytest.fail("contended delivery must not inspect provider state"),
    )

    enrich_request.run(str(request_id), str(tenant_id), str(user_id))

    assert observed == [
        "dispatch_rearmed",
        ("people_enrichment", "retry_scheduled"),
    ]


def test_exhausted_batch_schedules_one_fresh_run_delivery_for_later_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    run_id, tenant_id, user_id = uuid4(), uuid4(), uuid4()
    schedule = tasks.EnrichmentRetrySchedule(
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        candidate_limit=50,
        generation=7,
        task_id=f"enrich-run-retry:{run_id}:7",
        not_before=datetime.now(UTC),
    )
    _allow_enrichment_run(monkeypatch)

    class Gateway:
        def close(self) -> None:
            observed["closed"] = True

    monkeypatch.setattr(tasks, "get_worker_settings", WorkerSettings.for_test)
    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: True)
    monkeypatch.setattr(tasks, "ApolloGateway", lambda settings: Gateway())
    monkeypatch.setattr(
        tasks,
        "_enrichment_dependencies",
        lambda settings: (None, None, None, None),
    )
    monkeypatch.setattr(
        tasks,
        "enqueue_top_enrichment",
        lambda *args, **kwargs: [
            FailedEnrichment(request_id=uuid4()),
            DeferredEnrichment(retry_after_seconds=23),
        ],
    )
    monkeypatch.setattr(
        tasks,
        "_record_provider_outcome",
        lambda endpoint, outcome: observed.update(outcome=(endpoint, outcome)),
    )

    monkeypatch.setattr(
        tasks,
        "_stage_enrichment_retry",
        lambda *args, **kwargs: observed.update(staged=kwargs) or schedule,
    )
    monkeypatch.setattr(
        tasks,
        "_publish_enrichment_retry",
        lambda *args: observed.update(published=args[-1]) or True,
    )

    enrich_run.run(str(run_id), str(tenant_id), str(user_id))

    assert observed == {
        "closed": True,
        "outcome": ("people_enrichment", "retry_scheduled"),
        "staged": {
            "candidate_limit": 50,
            "retry_after": 23,
            "current_generation": None,
            "current_claim_token": None,
        },
        "published": schedule,
    }


def test_stale_retry_generation_is_a_noop_before_provider_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []
    _allow_enrichment_run(monkeypatch)
    monkeypatch.setattr(
        tasks,
        "_claim_enrichment_retry_delivery",
        lambda *args: None,
    )
    monkeypatch.setattr(
        tasks,
        "is_provider_enabled",
        lambda *args: observed.append("provider_checked") or True,
    )

    enrich_run.run(str(uuid4()), str(uuid4()), str(uuid4()), 50, 7)

    assert observed == []


def test_retry_publisher_carries_generation_and_marks_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, tenant_id, user_id = uuid4(), uuid4(), uuid4()
    schedule = tasks.EnrichmentRetrySchedule(
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        candidate_limit=37,
        generation=4,
        task_id=f"enrich-run-retry:{run_id}:4",
        not_before=datetime.now(UTC),
    )
    claim_token = uuid4()
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        tasks,
        "_claim_retry_publish",
        lambda *args: claim_token,
    )
    monkeypatch.setattr(
        enrich_run,
        "apply_async",
        lambda *, args, countdown, task_id: observed.update(
            scheduled=(args, countdown, task_id)
        ),
    )
    monkeypatch.setattr(
        tasks,
        "_finish_retry_publish",
        lambda *args, **kwargs: observed.update(finished=(args[-1], kwargs)) or True,
    )

    assert tasks._publish_enrichment_retry(object(), object(), schedule) is True  # type: ignore[arg-type]
    assert observed == {
        "scheduled": (
            (str(run_id), str(tenant_id), str(user_id), 37, 4),
            0,
            schedule.task_id,
        ),
        "finished": (claim_token, {"published": True}),
    }


def test_retry_publisher_releases_claim_when_broker_publish_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, tenant_id, user_id = uuid4(), uuid4(), uuid4()
    schedule = tasks.EnrichmentRetrySchedule(
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        candidate_limit=50,
        generation=2,
        task_id=f"enrich-run-retry:{run_id}:2",
        not_before=datetime.now(UTC),
    )
    claim_token = uuid4()
    finished: list[tuple[object, dict[str, object]]] = []

    def fail_publish(**kwargs: object) -> None:
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(tasks, "_claim_retry_publish", lambda *args: claim_token)
    monkeypatch.setattr(enrich_run, "apply_async", fail_publish)
    monkeypatch.setattr(
        tasks,
        "_finish_retry_publish",
        lambda *args, **kwargs: finished.append((args[-1], kwargs)) or True,
    )

    with pytest.raises(ConnectionError, match="broker unavailable"):
        tasks._publish_enrichment_retry(object(), object(), schedule)  # type: ignore[arg-type]

    assert finished == [(claim_token, {"published": False})]


@pytest.mark.parametrize(
    ("task", "operation"),
    [
        (enrich_run, "enqueue_top_enrichment"),
        (enrich_request, "execute_queued_enrichment_request"),
    ],
)
def test_terminal_enrichment_failure_is_not_reported_as_success(
    monkeypatch: pytest.MonkeyPatch,
    task: object,
    operation: str,
) -> None:
    observed: list[tuple[str, str]] = []
    request_id = uuid4()
    if task is enrich_run:
        _allow_enrichment_run(monkeypatch)
    elif task is enrich_request:
        _allow_enrichment_request(monkeypatch)

    class Gateway:
        def close(self) -> None:
            return None

    monkeypatch.setattr(tasks, "get_worker_settings", WorkerSettings.for_test)
    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: True)
    monkeypatch.setattr(tasks, "ApolloGateway", lambda settings: Gateway())
    monkeypatch.setattr(
        tasks,
        "_enrichment_dependencies",
        lambda settings: (None, None, None, None),
    )
    result: object = FailedEnrichment(request_id=request_id)
    if task is enrich_run:
        result = [result]
    monkeypatch.setattr(tasks, operation, lambda *args, **kwargs: result)
    monkeypatch.setattr(
        tasks,
        "_record_provider_outcome",
        lambda endpoint, outcome: observed.append((endpoint, outcome)),
    )

    task.run(str(request_id), str(uuid4()), str(uuid4()))

    assert observed == [("people_enrichment", "provider_error")]


@pytest.mark.parametrize(
    ("poll_result", "expected_outcome", "expected_countdown"),
    [
        (17, "retry_scheduled", 17),
        (FailedEnrichment(request_id=uuid4()), "provider_error", None),
        (None, "success", None),
    ],
)
def test_poll_wrapper_reports_persisted_outcome_accurately(
    monkeypatch: pytest.MonkeyPatch,
    poll_result: int | FailedEnrichment | None,
    expected_outcome: str,
    expected_countdown: int | None,
) -> None:
    observed: dict[str, object] = {}

    class Gateway:
        def close(self) -> None:
            observed["closed"] = True

    monkeypatch.setattr(tasks, "get_worker_settings", WorkerSettings.for_test)
    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: True)
    monkeypatch.setattr(tasks, "ApolloGateway", lambda settings: Gateway())
    monkeypatch.setattr(
        tasks,
        "_enrichment_dependencies",
        lambda settings: (None, None, None, None),
    )
    monkeypatch.setattr(
        tasks,
        "poll_enrichment_request",
        lambda *args, **kwargs: poll_result,
    )
    monkeypatch.setattr(
        tasks,
        "_record_provider_outcome",
        lambda endpoint, outcome: observed.update(outcome=(endpoint, outcome)),
    )
    monkeypatch.setattr(
        poll_enrichment_result,
        "apply_async",
        lambda *, args, countdown: observed.update(
            scheduled_args=args, countdown=countdown
        ),
    )

    poll_enrichment_result.run(str(uuid4()), str(uuid4()), str(uuid4()))

    assert observed["closed"] is True
    assert observed["outcome"] == ("enrichment_poll", expected_outcome)
    assert observed.get("countdown") == expected_countdown


def test_maintenance_tasks_are_isolated_on_a_dedicated_worker_and_queue() -> None:
    assert celery_app.conf.beat_schedule == {}
    assert "maintenance.reconcile_expired_snapshots" in maintenance_celery_app.tasks
    assert "maintenance.expire_contact_points" in maintenance_celery_app.tasks
    assert "maintenance.execute_privacy_deletion" in maintenance_celery_app.tasks
    assert "maintenance.resume_privacy_deletions" in maintenance_celery_app.tasks
    assert "maintenance.recover_sourcing_dispatches" in maintenance_celery_app.tasks
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
    assert str(contact_entry["schedule"]) == "<crontab: 0 2 * * * (m/h/dM/MY/d)>"
    privacy_entry = maintenance_celery_app.conf.beat_schedule[
        "privacy-deletion-resumption"
    ]
    assert privacy_entry["task"] == resume_privacy_deletions.name
    assert str(privacy_entry["schedule"]) == "<crontab: 0 2 * * * (m/h/dM/MY/d)>"
    dispatch_entry = maintenance_celery_app.conf.beat_schedule[
        "sourcing-dispatch-recovery"
    ]
    assert dispatch_entry["task"] == "maintenance.recover_sourcing_dispatches"
    assert str(dispatch_entry["schedule"]) == "<crontab: * * * * * (m/h/dM/MY/d)>"
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


def test_privacy_deletion_task_retries_indefinitely_when_cleanup_is_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id, tenant_id = uuid4(), uuid4()
    observed: dict[str, object] = {}

    class RetryRequested(RuntimeError):
        pass

    monkeypatch.setattr(
        privacy_tasks,
        "get_maintenance_settings",
        privacy_tasks.MaintenanceSettings.for_test,
    )
    monkeypatch.setattr(
        privacy_tasks,
        "_run_privacy_deletion",
        lambda *_args: False,
    )

    def retry(**kwargs: object) -> None:
        observed.update(kwargs)
        raise RetryRequested

    monkeypatch.setattr(execute_privacy_deletion, "retry", retry)

    with pytest.raises(RetryRequested):
        execute_privacy_deletion.run(str(request_id), str(tenant_id))

    assert observed == {"countdown": 60, "max_retries": None}


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

    settings = WorkerSettings.for_test()
    tasks._enrichment_dependencies(settings)

    assert received["aws_access_key_id"] == "test-writer-key"
    assert received["aws_secret_access_key"] == "test-writer-secret"


def test_daily_maintenance_worker_uses_delete_only_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, object] = {}
    tenant_id, run_id, snapshot_id = uuid4(), uuid4(), uuid4()
    reference = f"{tenant_id}/{run_id}/apollo/request"
    claims = [[(snapshot_id, tenant_id, reference)], []]
    deleted_versions: list[str | None] = []
    completed: list[object] = []

    class DeleteOnlyObjectStore:
        def delete_object(self, **kwargs: object) -> None:
            deleted_versions.append(kwargs.get("VersionId"))  # type: ignore[arg-type]

        def list_object_versions(self, **kwargs: object) -> dict[str, object]:
            key = str(kwargs["Prefix"])
            return {
                "Versions": [{"Key": key, "VersionId": "historical"}],
                "DeleteMarkers": [{"Key": key, "VersionId": "marker"}],
                "IsTruncated": False,
            }

    class EmptySession:
        def __init__(self, engine: object) -> None:
            del engine

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def execute(self, *args: object, **kwargs: object) -> SimpleNamespace:
            del args, kwargs
            rows = claims.pop(0)
            return SimpleNamespace(all=lambda: rows)

        def scalar(self, statement: object, *args: object, **kwargs: object) -> None:
            del args, kwargs
            completed.append(statement)

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
    assert deleted_versions == ["historical", "marker"]
    assert len(completed) == 1
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

    monkeypatch.setattr(tasks, "get_worker_settings", lambda: object())
    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: True)
    monkeypatch.setattr(tasks, "disable_provider", lambda *args: None)

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

    monkeypatch.setattr(tasks, "get_worker_settings", lambda: object())
    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: True)
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
