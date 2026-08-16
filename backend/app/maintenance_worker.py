from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]

from app.core.config import get_maintenance_settings

settings = get_maintenance_settings()
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
    },
)

from app.privacy import tasks as _privacy_tasks  # noqa: F401
from app.sourcing import maintenance_tasks as _maintenance_tasks  # noqa: F401
