from typing import cast

from celery import Celery  # type: ignore[import-untyped]
from redis import Redis

from app.core.config import get_worker_settings
from app.core.telemetry import (
    install_logging_defaults,
    install_task_telemetry,
    start_metrics_server_from_env,
)

settings = get_worker_settings()
install_logging_defaults()
celery_app = Celery(
    "recruitment_sourcing",
    broker=settings.redis_url,
    include=("app.sourcing.tasks",),
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
    beat_schedule={},
)
celery_app.autodiscover_tasks(("app.sourcing",), force=True)
metrics_redis = Redis.from_url(settings.redis_url, socket_timeout=2)
worker_metrics = install_task_telemetry(
    celery_app,
    hmac_key=settings.telemetry_hmac_key.get_secret_value().encode(),
    queue_depth_probes={"sourcing": lambda: cast(int, metrics_redis.llen("sourcing"))},
)
start_metrics_server_from_env(worker_metrics)
