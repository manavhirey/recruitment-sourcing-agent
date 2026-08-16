from collections.abc import Callable
from typing import cast

from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]
from redis import Redis
from sqlalchemy import create_engine, text

from app.core.config import get_maintenance_settings
from app.core.telemetry import (
    install_logging_defaults,
    install_task_telemetry,
    start_metrics_server_from_env,
)

settings = get_maintenance_settings()
install_logging_defaults()
celery_app = Celery(
    "recruitment_sourcing_maintenance",
    broker=settings.redis_url,
    include=("app.sourcing.maintenance_tasks", "app.privacy.tasks"),
)
celery_app.conf.update(
    accept_content=["json"],
    result_serializer="json",
    task_acks_late=True,
    task_acks_on_failure_or_timeout=False,
    task_ignore_result=True,
    task_reject_on_worker_lost=True,
    task_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_routes={"maintenance.*": {"queue": "maintenance"}},
    beat_schedule={
        "snapshot-reference-reconciliation": {
            "task": "maintenance.reconcile_expired_snapshots",
            "schedule": crontab(hour=2, minute=0),
        },
        "contact-point-expiration": {
            "task": "maintenance.expire_contact_points",
            "schedule": crontab(hour=2, minute=0),
        },
        "privacy-deletion-resumption": {
            "task": "maintenance.resume_privacy_deletions",
            "schedule": crontab(hour=2, minute=0),
        },
        "sourcing-dispatch-recovery": {
            "task": "maintenance.recover_sourcing_dispatches",
            "schedule": crontab(minute="*"),
        },
    },
)

from app.privacy import tasks as _privacy_tasks  # noqa: F401
from app.sourcing import dispatch_recovery as _dispatch_recovery  # noqa: F401
from app.sourcing import maintenance_tasks as _maintenance_tasks  # noqa: F401

metrics_redis = Redis.from_url(settings.redis_url, socket_timeout=2)
metrics_engine = create_engine(
    settings.maintenance_database_url, pool_pre_ping=True, pool_timeout=2
)


def durable_stuck_count(stage: str) -> int:
    with metrics_engine.connect() as connection:
        return int(
            connection.scalar(
                text("SELECT maintenance_stuck_run_count(:stage)"),
                {"stage": stage},
            )
            or 0
        )


def stuck_probe(stage: str) -> Callable[[], int]:
    def probe() -> int:
        return durable_stuck_count(stage)

    return probe


maintenance_metrics = install_task_telemetry(
    celery_app,
    hmac_key=settings.telemetry_hmac_key.get_secret_value().encode(),
    queue_depth_probes={
        "maintenance": lambda: cast(int, metrics_redis.llen("maintenance"))
    },
    stuck_run_probes={
        stage: stuck_probe(stage)
        for stage in ("queued", "sourcing", "matching", "enriching")
    },
)
start_metrics_server_from_env(maintenance_metrics)
