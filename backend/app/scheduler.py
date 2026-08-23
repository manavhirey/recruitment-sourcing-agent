from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]

from app.core.config import get_scheduler_settings
from app.core.telemetry import install_logging_defaults

# Beat publishes task names only. It has broker access and no database/object/provider
# capability; task implementations live exclusively in the maintenance worker.
settings = get_scheduler_settings()
install_logging_defaults()
celery_app = Celery("recruitment_sourcing_scheduler", broker=settings.redis_url)
celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
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
