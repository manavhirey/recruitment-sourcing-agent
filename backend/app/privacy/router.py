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
from app.privacy.models import PrivacyRequest
from app.privacy.schemas import (
    PrivacyRequestCreate,
    PrivacyRequestReject,
    PrivacyRequestResponse,
    PrivacyRequestState,
)
from app.privacy.service import PrivacyError, PrivacyService

router = APIRouter(prefix="/api/v1/privacy-requests", tags=["privacy"])


def _service(
    session: Session,
    settings: Settings,
    request: Request,
) -> PrivacyService:
    return PrivacyService(
        session,
        settings.suppression_hmac_key.get_secret_value().encode(),
        request.app.state.contact_cipher,
        key_version=settings.suppression_hmac_key_version,
    )


def _response(value: PrivacyRequest) -> PrivacyRequestResponse:
    return PrivacyRequestResponse.model_validate(value)


def _raise_privacy_error(error: PrivacyError) -> NoReturn:
    status_code = {
        "privacy_request_not_found": status.HTTP_404_NOT_FOUND,
        "candidate_not_found": status.HTTP_404_NOT_FOUND,
        "forbidden": status.HTTP_403_FORBIDDEN,
        "idempotency_conflict": status.HTTP_409_CONFLICT,
        "privacy_request_state_invalid": status.HTTP_409_CONFLICT,
    }.get(error.code, status.HTTP_400_BAD_REQUEST)
    raise HTTPException(status_code=status_code, detail={"code": error.code}) from error


@router.post(
    "",
    response_model=PrivacyRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_privacy_request(
    body: PrivacyRequestCreate,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> PrivacyRequestResponse:
    try:
        value = _service(session, settings, request).submit(
            context,
            candidate_id=body.candidate_id,
            request_type=body.request_type,
            idempotency_key=idempotency_key,
        )
    except PrivacyError as error:
        _raise_privacy_error(error)
    return _response(value)


@router.get("", response_model=list[PrivacyRequestResponse])
def list_privacy_requests(
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> list[PrivacyRequestResponse]:
    return [
        _response(item) for item in _service(session, settings, request).list(context)
    ]


@router.get("/{request_id}", response_model=PrivacyRequestResponse)
def privacy_request_status(
    request_id: UUID,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> PrivacyRequestResponse:
    try:
        value = _service(session, settings, request).get(context, request_id)
    except PrivacyError as error:
        _raise_privacy_error(error)
    return _response(value)


@router.post("/{request_id}/verify", response_model=PrivacyRequestResponse)
def verify_privacy_request(
    request_id: UUID,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> PrivacyRequestResponse:
    try:
        value = _service(session, settings, request).verify(
            context, request_id, idempotency_key=idempotency_key
        )
    except PrivacyError as error:
        _raise_privacy_error(error)
    return _response(value)


@router.post("/{request_id}/approve", response_model=PrivacyRequestResponse)
def approve_privacy_request(
    request_id: UUID,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> PrivacyRequestResponse:
    try:
        value = _service(session, settings, request).approve(
            context, request_id, idempotency_key=idempotency_key
        )
    except PrivacyError as error:
        _raise_privacy_error(error)
    return _response(value)


@router.post("/{request_id}/reject", response_model=PrivacyRequestResponse)
def reject_privacy_request(
    request_id: UUID,
    body: PrivacyRequestReject,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> PrivacyRequestResponse:
    try:
        value = _service(session, settings, request).reject(
            context,
            request_id,
            body.reason_code,
            idempotency_key=idempotency_key,
        )
    except PrivacyError as error:
        _raise_privacy_error(error)
    return _response(value)


@router.post("/{request_id}/execute", response_model=PrivacyRequestResponse)
def execute_privacy_request(
    request_id: UUID,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> PrivacyRequestResponse:
    try:
        value = _service(session, settings, request).execute(
            context, request_id, idempotency_key=idempotency_key
        )
    except PrivacyError as error:
        _raise_privacy_error(error)
    if value.state is PrivacyRequestState.EXECUTING:
        request.app.state.privacy_dispatcher(value.id, value.tenant_id)
    return _response(value)
