from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from app.providers.base import (
    ProviderAuthenticationError,
    ProviderRateLimited,
    ProviderTemporaryError,
)
from app.sourcing import tasks
from app.sourcing.tasks import (
    _provider_retry_countdown,
    match_run,
    plan_run,
    source_run,
)
from app.worker import celery_app


def test_worker_acknowledges_after_commit_and_limits_prefetch() -> None:
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_acks_on_failure_or_timeout is False
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]


def test_sourcing_tasks_use_late_acknowledgement_and_bounded_retries() -> None:
    for task in (plan_run, source_run, match_run):
        assert task.acks_late is True
        assert task.reject_on_worker_lost is True
        assert task.max_retries == 5
        assert OperationalError in task.autoretry_for
    assert source_run.retry_backoff is True
    assert source_run.retry_jitter is True


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

    source_run.run(str(run_id), str(tenant_id), str(user_id), "source")

    assert [(entry[0], entry[1].tenant_id, entry[2]) for entry in marked] == [
        (run_id, tenant_id, "source")
    ]
