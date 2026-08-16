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
from app.jobs.llm import ScorecardGateway
from app.jobs.models import Job
from app.jobs.schemas import (
    ConfirmedScorecard,
    ExtractionStatus,
    JobCreate,
    JobResponse,
    ScorecardConfirmation,
    ScorecardDraftResponse,
    ScorecardDraftUpdate,
    ScorecardGenerationRequest,
    ScorecardRevisionRequest,
)
from app.jobs.service import JobError, JobService

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


def get_scorecard_gateway(request: Request) -> ScorecardGateway:
    return request.app.state.scorecard_gateway


def _service(
    session: Session, settings: Settings, gateway: ScorecardGateway
) -> JobService:
    return JobService(
        session,
        settings.suppression_hmac_key.get_secret_value().encode(),
        gateway,
    )


def _job_response(job: Job) -> JobResponse:
    return JobResponse(
        id=job.id,
        tenant_id=job.tenant_id,
        client_id=job.client_id,
        owner_user_id=job.owner_user_id,
        title=job.title,
        job_description=job.job_description,
        location=job.location,
        employment_model=job.employment_model,
        status=job.status,
        draft_revision=job.draft_revision,
        extraction_status=ExtractionStatus(job.draft_extraction_status),
        extraction_warning=job.draft_extraction_warning,
        current_scorecard_id=job.current_scorecard_id,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _raise_job_error(error: JobError) -> NoReturn:
    status_code = {
        "job_not_found": status.HTTP_404_NOT_FOUND,
        "scorecard_not_found": status.HTTP_404_NOT_FOUND,
        "scorecard_revision_conflict": status.HTTP_409_CONFLICT,
        "idempotency_conflict": status.HTTP_409_CONFLICT,
    }.get(error.code, status.HTTP_400_BAD_REQUEST)
    raise HTTPException(status_code=status_code, detail={"code": error.code}) from error


@router.post("", response_model=JobResponse, status_code=status.HTTP_201_CREATED)
def create_job(
    body: JobCreate,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    gateway: Annotated[ScorecardGateway, Depends(get_scorecard_gateway)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> JobResponse:
    service = _service(session, settings, gateway)
    try:
        job = service.create(
            context,
            client_id=body.client_id,
            title=body.title,
            job_description=body.job_description,
            location=body.location,
            employment_model=body.employment_model,
            idempotency_key=idempotency_key,
        )
    except JobError as error:
        _raise_job_error(error)
    return _job_response(job)


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: UUID,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    gateway: Annotated[ScorecardGateway, Depends(get_scorecard_gateway)],
) -> JobResponse:
    service = _service(session, settings, gateway)
    try:
        job = service.get_authorized(context, job_id)
    except JobError as error:
        _raise_job_error(error)
    return _job_response(job)


@router.post("/{job_id}/scorecard/generate", response_model=ScorecardDraftResponse)
def generate_scorecard_draft(
    job_id: UUID,
    body: ScorecardGenerationRequest,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    gateway: Annotated[ScorecardGateway, Depends(get_scorecard_gateway)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> ScorecardDraftResponse:
    service = _service(session, settings, gateway)
    try:
        result = service.generate_draft(
            context,
            job_id,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
        )
    except JobError as error:
        _raise_job_error(error)
    return result


@router.put("/{job_id}/scorecard/draft", response_model=ScorecardDraftResponse)
def update_scorecard_draft(
    job_id: UUID,
    body: ScorecardDraftUpdate,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    gateway: Annotated[ScorecardGateway, Depends(get_scorecard_gateway)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> ScorecardDraftResponse:
    service = _service(session, settings, gateway)
    try:
        result = service.update_draft(
            context,
            job_id,
            body.draft,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
        )
    except JobError as error:
        _raise_job_error(error)
    return result


@router.post("/{job_id}/scorecard/confirm", response_model=ConfirmedScorecard)
def confirm_scorecard(
    job_id: UUID,
    body: ScorecardConfirmation,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    gateway: Annotated[ScorecardGateway, Depends(get_scorecard_gateway)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> ConfirmedScorecard:
    service = _service(session, settings, gateway)
    try:
        return service.confirm_scorecard(
            context,
            job_id,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
        )
    except JobError as error:
        _raise_job_error(error)


@router.get("/{job_id}/scorecards", response_model=list[ConfirmedScorecard])
def list_scorecard_versions(
    job_id: UUID,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    gateway: Annotated[ScorecardGateway, Depends(get_scorecard_gateway)],
) -> list[ConfirmedScorecard]:
    service = _service(session, settings, gateway)
    try:
        return service.list_versions(context, job_id)
    except JobError as error:
        _raise_job_error(error)


@router.post("/{job_id}/rescore", response_model=ConfirmedScorecard)
def rescore_job(
    job_id: UUID,
    body: ScorecardRevisionRequest,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    gateway: Annotated[ScorecardGateway, Depends(get_scorecard_gateway)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> ConfirmedScorecard:
    service = _service(session, settings, gateway)
    try:
        return service.revise_scorecard(
            context,
            job_id,
            body.draft,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
        )
    except JobError as error:
        _raise_job_error(error)
