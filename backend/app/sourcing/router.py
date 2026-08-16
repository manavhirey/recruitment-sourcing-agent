from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.identity.dependencies import (
    get_app_settings,
    get_idempotency_key,
    get_request_context,
)
from app.identity.schemas import RequestContext
from app.sourcing.models import SourcingRun, TenantNotification
from app.sourcing.schemas import (
    EnrichmentRequestResponse,
    NotificationResponse,
    RunActivityResponse,
    RunResponse,
    StartRunRequest,
)
from app.sourcing.service import SourcingError, SourcingService

router = APIRouter(tags=["sourcing"])
SourcingDispatcher = Callable[[UUID, UUID, UUID], None]
EnrichmentDispatcher = Callable[[UUID, UUID, UUID], None]


def get_sourcing_dispatcher(request: Request) -> SourcingDispatcher:
    return request.app.state.sourcing_dispatcher


def get_enrichment_dispatcher(request: Request) -> EnrichmentDispatcher:
    return request.app.state.enrichment_dispatcher


def _service(session: Session, settings: Settings) -> SourcingService:
    return SourcingService(
        session, settings.suppression_hmac_key.get_secret_value().encode()
    )


def _raise_sourcing_error(error: SourcingError) -> NoReturn:
    status_code = {
        "run_not_found": status.HTTP_404_NOT_FOUND,
        "run_candidate_not_found": status.HTTP_404_NOT_FOUND,
        "enrichment_request_not_found": status.HTTP_404_NOT_FOUND,
        "notification_not_found": status.HTTP_404_NOT_FOUND,
        "active_run_exists": status.HTTP_409_CONFLICT,
        "scorecard_required": status.HTTP_409_CONFLICT,
        "idempotency_conflict": status.HTTP_409_CONFLICT,
        "usage_reservation_conflict": status.HTTP_409_CONFLICT,
        "usage_reconciliation_conflict": status.HTTP_409_CONFLICT,
    }.get(error.code, status.HTTP_400_BAD_REQUEST)
    raise HTTPException(status_code=status_code, detail={"code": error.code}) from error


def _run_response(
    service: SourcingService, context: RequestContext, run: SourcingRun
) -> RunResponse:
    return RunResponse(
        id=run.id,
        tenant_id=run.tenant_id,
        job_id=run.job_id,
        scorecard_version_id=run.scorecard_version_id,
        state=run.state,
        current_stage=run.current_stage,
        candidate_count=run.candidate_count,
        matched_count=run.matched_count,
        cancellation_requested=run.cancellation_requested,
        budget_use=service.usage_totals(context, run.id),
        error_code=run.error_code,
        error_message=run.error_message,
        created_at=_utc(run.created_at),
        started_at=_utc(run.started_at),
        completed_at=_utc(run.completed_at),
        updated_at=_utc(run.updated_at),
    )


def _notification_response(notification: TenantNotification) -> NotificationResponse:
    return NotificationResponse(
        id=notification.id,
        run_id=notification.run_id,
        code=notification.code,
        title=notification.title,
        message=notification.message,
        acknowledged_at=_utc(notification.acknowledged_at),
        created_at=_utc(notification.created_at),
    )


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


@router.post(
    "/api/v1/jobs/{job_id}/runs",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
)
def start_run(
    job_id: UUID,
    body: StartRunRequest,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    dispatcher: Annotated[SourcingDispatcher, Depends(get_sourcing_dispatcher)],
) -> RunResponse:
    del body
    service = _service(session, settings)
    try:
        run = service.start(context, job_id, idempotency_key=idempotency_key)
        response = _run_response(service, context, run)
        session.commit()
    except SourcingError as error:
        _raise_sourcing_error(error)
    dispatcher(run.id, context.tenant_id, context.user_id)
    return response


@router.post("/api/v1/runs/{run_id}/cancel", response_model=RunResponse)
def cancel_run(
    run_id: UUID,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> RunResponse:
    service = _service(session, settings)
    try:
        run = service.cancel(context, run_id, idempotency_key=idempotency_key)
        return _run_response(service, context, run)
    except SourcingError as error:
        _raise_sourcing_error(error)


@router.get("/api/v1/runs/{run_id}", response_model=RunResponse)
def get_run(
    run_id: UUID,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> RunResponse:
    service = _service(session, settings)
    try:
        run = service.get_authorized(context, run_id)
        return _run_response(service, context, run)
    except SourcingError as error:
        _raise_sourcing_error(error)


@router.get(
    "/api/v1/runs/{run_id}/activity",
    response_model=list[RunActivityResponse],
)
def get_run_activity(
    run_id: UUID,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> list[RunActivityResponse]:
    service = _service(session, settings)
    try:
        events = service.activity(context, run_id)
    except SourcingError as error:
        _raise_sourcing_error(error)
    return [
        RunActivityResponse(
            id=event.id,
            action=event.action,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            actor_user_id=event.actor_user_id,
            payload=event.payload,
            created_at=_utc(event.created_at),
        )
        for event in events
    ]


@router.get("/api/v1/notifications", response_model=list[NotificationResponse])
def list_notifications(
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> list[NotificationResponse]:
    return [
        _notification_response(notification)
        for notification in _service(session, settings).list_notifications(context)
    ]


@router.patch(
    "/api/v1/notifications/{notification_id}",
    response_model=NotificationResponse,
)
def acknowledge_notification(
    notification_id: UUID,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> NotificationResponse:
    service = _service(session, settings)
    try:
        notification = service.acknowledge_notification(
            context,
            notification_id,
            idempotency_key=idempotency_key,
        )
    except SourcingError as error:
        _raise_sourcing_error(error)
    return _notification_response(notification)


@router.post(
    "/api/v1/job-candidates/{run_candidate_id}/enrich",
    response_model=EnrichmentRequestResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_candidate_enrichment(
    run_candidate_id: UUID,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
    dispatcher: Annotated[EnrichmentDispatcher, Depends(get_enrichment_dispatcher)],
) -> EnrichmentRequestResponse:
    service = _service(session, settings)
    try:
        enrichment, created = service.queue_on_demand_enrichment(
            context,
            run_candidate_id,
            idempotency_key=idempotency_key,
        )
        response = EnrichmentRequestResponse(
            id=enrichment.id,
            run_id=enrichment.run_id,
            status=enrichment.status,
        )
        session.commit()
    except SourcingError as error:
        _raise_sourcing_error(error)
    if created:
        dispatcher(enrichment.id, context.tenant_id, context.user_id)
    return response
