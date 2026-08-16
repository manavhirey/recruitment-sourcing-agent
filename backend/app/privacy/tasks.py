from uuid import UUID

from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import MaintenanceSettings, get_maintenance_settings
from app.maintenance_worker import celery_app
from app.providers.snapshots import validate_snapshot_reference


@celery_app.task(
    bind=True,
    name="maintenance.execute_privacy_deletion",
    shared=False,
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute_privacy_deletion(self: object, request_id: str, tenant_id: str) -> None:
    completed = _run_privacy_deletion(
        get_maintenance_settings(),
        UUID(request_id),
        UUID(tenant_id),
    )
    if not completed:
        raise self.retry(countdown=60, max_retries=None)  # type: ignore[attr-defined]


@celery_app.task(name="maintenance.resume_privacy_deletions", shared=False)
def resume_privacy_deletions() -> None:
    settings = get_maintenance_settings()
    engine = create_engine(settings.maintenance_database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            due = session.execute(
                text(
                    "SELECT request_id, tenant_id "
                    "FROM privacy_due_deletions(:batch_size)"
                ),
                {"batch_size": 100},
            ).all()
            session.commit()
    finally:
        engine.dispose()
    for request_id, tenant_id in due:
        execute_privacy_deletion.delay(str(request_id), str(tenant_id))


def _run_privacy_deletion(
    settings: MaintenanceSettings,
    request_id: UUID,
    tenant_id: UUID,
) -> bool:
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
                        "SELECT target_id, tenant_id, object_reference "
                        "FROM privacy_claim_deletion_snapshots("
                        ":request_id, :tenant_id, :batch_size)"
                    ),
                    {
                        "request_id": request_id,
                        "tenant_id": tenant_id,
                        "batch_size": 50,
                    },
                ).all()
                session.commit()
            if not rows:
                with Session(engine) as session:
                    completed = bool(
                        session.scalar(
                            text(
                                "SELECT privacy_finalize_deletion("
                                ":request_id, :tenant_id)"
                            ),
                            {"request_id": request_id, "tenant_id": tenant_id},
                        )
                    )
                    session.commit()
                return completed
            failure = False
            for target_id, target_tenant_id, reference in rows:
                try:
                    if target_tenant_id != tenant_id:
                        raise ValueError("snapshot target tenant mismatch")
                    validate_snapshot_reference(reference, tenant_id=tenant_id)
                    client.delete_object(
                        Bucket=settings.object_store_bucket,
                        Key=reference,
                    )
                except (FileNotFoundError, KeyError):
                    # S3 deletion is idempotent; an already-missing object is erased.
                    pass
                except (BotoCoreError, ClientError, OSError, ValueError):
                    failure = True
                    with Session(engine) as session:
                        session.scalar(
                            text(
                                "SELECT privacy_mark_deletion_snapshot_failed("
                                ":target_id, :error_code)"
                            ),
                            {
                                "target_id": target_id,
                                "error_code": "object_delete_failed",
                            },
                        )
                        session.commit()
                    continue
                with Session(engine) as session:
                    session.scalar(
                        text(
                            "SELECT privacy_mark_deletion_snapshot_deleted(:target_id)"
                        ),
                        {"target_id": target_id},
                    )
                    session.commit()
            if failure:
                return False
    finally:
        engine.dispose()
