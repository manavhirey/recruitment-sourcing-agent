from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import MaintenanceSettings, get_maintenance_settings
from app.maintenance_worker import celery_app
from app.providers.snapshots import purge_snapshot_versions, validate_snapshot_reference


@celery_app.task(name="maintenance.reconcile_expired_snapshots", shared=False)
def reconcile_expired_snapshots() -> None:
    _run_snapshot_reconciliation(get_maintenance_settings())


@celery_app.task(name="maintenance.expire_contact_points", shared=False)
def expire_contact_points() -> None:
    settings = get_maintenance_settings()
    _run_contact_expiry(settings.maintenance_database_url)


def _run_contact_expiry(database_url: str) -> None:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            session.scalar(text("SELECT maintenance_erase_due_contacts()"))
            session.commit()
    finally:
        engine.dispose()


def _run_snapshot_reconciliation(settings: MaintenanceSettings) -> None:
    import boto3  # type: ignore[import-untyped]

    client = boto3.client(
        "s3",
        endpoint_url=settings.object_store_endpoint,
        aws_access_key_id=settings.object_store_delete_access_key_id.get_secret_value(),
        aws_secret_access_key=(
            settings.object_store_delete_secret_access_key.get_secret_value()
        ),
    )
    engine = create_engine(settings.maintenance_database_url, pool_pre_ping=True)
    try:
        while True:
            with Session(engine) as session:
                rows = session.execute(
                    text(
                        "SELECT snapshot_id, tenant_id, object_reference "
                        "FROM maintenance_claim_expired_snapshots(:batch_size)"
                    ),
                    {"batch_size": 100},
                ).all()
                session.commit()
            if not rows:
                break
            for snapshot_id, tenant_id, reference in rows:
                try:
                    validate_snapshot_reference(reference, tenant_id=tenant_id)
                    purge_snapshot_versions(
                        client, settings.object_store_bucket, reference
                    )
                except (FileNotFoundError, KeyError):
                    pass
                except (BotoCoreError, ClientError, OSError, TypeError, ValueError):
                    metrics = getattr(celery_app, "_platform_metrics", None)
                    if metrics is not None:
                        metrics.snapshot_expiry_failures.inc()
                    with Session(engine) as session:
                        session.scalar(
                            text(
                                "SELECT maintenance_record_snapshot_delete_failure("
                                ":snapshot_id, :error_code)"
                            ),
                            {
                                "snapshot_id": snapshot_id,
                                "error_code": "object_delete_failed",
                            },
                        )
                        session.commit()
                    continue
                with Session(engine) as session:
                    session.scalar(
                        text(
                            "SELECT maintenance_delete_claimed_snapshot(:snapshot_id)"
                        ),
                        {"snapshot_id": snapshot_id},
                    )
                    session.commit()
    finally:
        engine.dispose()
