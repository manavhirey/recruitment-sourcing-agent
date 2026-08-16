from collections.abc import Callable, Iterable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_maintenance_settings
from app.maintenance_worker import celery_app


@dataclass(frozen=True)
class DispatchClaim:
    run_id: UUID
    tenant_id: UUID
    user_id: UUID
    claim_token: UUID
    dispatch_key: str


@dataclass(frozen=True)
class RecoveryResult:
    published: int
    failed: int


def recover_claimed_dispatches(
    claims: Iterable[DispatchClaim],
    *,
    publish: Callable[[DispatchClaim], None],
    complete: Callable[[DispatchClaim], None],
    release: Callable[[DispatchClaim], None],
) -> RecoveryResult:
    published = 0
    failed = 0
    for claim in claims:
        try:
            publish(claim)
        except Exception:  # noqa: BLE001 - broker clients expose varied exceptions
            release(claim)
            failed += 1
            continue
        complete(claim)
        published += 1
    return RecoveryResult(published=published, failed=failed)


def _claims(session: Session, *, batch_size: int) -> list[DispatchClaim]:
    rows = session.execute(
        text(
            "SELECT run_id, tenant_id, user_id, claim_token, dispatch_key "
            "FROM maintenance_claim_pending_sourcing_dispatches(:batch_size)"
        ),
        {"batch_size": batch_size},
    ).all()
    return [DispatchClaim(*row) for row in rows]


def _finish_claim(database_url: str, claim: DispatchClaim, function: str) -> None:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            session.scalar(
                text(f"SELECT {function}(:run_id, :claim_token)"),
                {"run_id": claim.run_id, "claim_token": claim.claim_token},
            )
            session.commit()
    finally:
        engine.dispose()


def recover_pending_dispatches(
    database_url: str,
    publish: Callable[[DispatchClaim], None],
    *,
    batch_size: int = 100,
) -> RecoveryResult:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            claims = _claims(session, batch_size=batch_size)
            session.commit()
    finally:
        engine.dispose()

    return recover_claimed_dispatches(
        claims,
        publish=publish,
        complete=lambda claim: _finish_claim(
            database_url, claim, "maintenance_complete_sourcing_dispatch"
        ),
        release=lambda claim: _finish_claim(
            database_url, claim, "maintenance_release_sourcing_dispatch"
        ),
    )


def _publish_sourcing_plan(claim: DispatchClaim) -> None:
    celery_app.send_task(
        "sourcing.plan_run",
        args=(
            str(claim.run_id),
            str(claim.tenant_id),
            str(claim.user_id),
            "plan",
        ),
        task_id=claim.dispatch_key,
    )


@celery_app.task(name="maintenance.recover_sourcing_dispatches", shared=False)
def recover_sourcing_dispatches() -> None:
    settings = get_maintenance_settings()
    recover_pending_dispatches(
        settings.maintenance_database_url,
        _publish_sourcing_plan,
    )
