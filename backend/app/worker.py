from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]

from app.core.config import get_settings

settings = get_settings()
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
    beat_schedule={
        "snapshot-reference-reconciliation": {
            "task": "sourcing.reconcile_expired_snapshots",
            "schedule": crontab(hour=2, minute=0),
        },
        "contact-point-expiration": {
            "task": "sourcing.expire_contact_points",
            "schedule": crontab(hour=2, minute=15),
        },
    },
)
celery_app.autodiscover_tasks(("app.sourcing",), force=True)
