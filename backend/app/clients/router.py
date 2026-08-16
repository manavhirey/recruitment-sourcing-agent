from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.clients.models import ClientCompany
from app.clients.schemas import (
    ClientAdjacencyUpdate,
    ClientCreate,
    ClientGrantCreate,
    ClientGrantResponse,
    ClientIndustriesUpdate,
    ClientResponse,
)
from app.clients.service import ClientError, ClientService
from app.core.config import Settings
from app.core.database import get_db
from app.identity.dependencies import (
    get_app_settings,
    get_idempotency_key,
    get_request_context,
    require_role,
)
from app.identity.schemas import RequestContext, Role

router = APIRouter(prefix="/api/v1/clients", tags=["clients"])
manager_context = require_role(Role.OWNER, Role.ADMIN)


def _service(session: Session, settings: Settings) -> ClientService:
    return ClientService(
        session, settings.suppression_hmac_key.get_secret_value().encode()
    )


def _client_response(service: ClientService, client: ClientCompany) -> ClientResponse:
    return ClientResponse(
        id=client.id,
        tenant_id=client.tenant_id,
        name=client.name,
        industry_codes=service.industries_for(client),
        adjacent_industries=service.adjacencies_for(client),
    )


def _raise_client_error(error: ClientError) -> NoReturn:
    status_code = {
        "client_not_found": status.HTTP_404_NOT_FOUND,
        "recruiter_not_found": status.HTTP_404_NOT_FOUND,
        "client_name_invalid": status.HTTP_400_BAD_REQUEST,
        "industry_code_invalid": status.HTTP_400_BAD_REQUEST,
        "client_industry_not_assigned": status.HTTP_400_BAD_REQUEST,
        "industry_adjacency_invalid": status.HTTP_400_BAD_REQUEST,
        "idempotency_key_invalid": status.HTTP_400_BAD_REQUEST,
        "idempotency_conflict": status.HTTP_409_CONFLICT,
    }.get(error.code, status.HTTP_400_BAD_REQUEST)
    raise HTTPException(status_code=status_code, detail={"code": error.code}) from error


@router.get("", response_model=list[ClientResponse])
def list_clients(
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> list[ClientResponse]:
    service = _service(session, settings)
    return [
        _client_response(service, client) for client in service.list_authorized(context)
    ]


@router.post("", response_model=ClientResponse, status_code=status.HTTP_201_CREATED)
def create_client(
    body: ClientCreate,
    context: Annotated[RequestContext, Depends(manager_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> ClientResponse:
    service = _service(session, settings)
    try:
        client = service.create(
            context,
            name=body.name,
            industry_codes=body.industry_codes,
            idempotency_key=idempotency_key,
        )
    except ClientError as error:
        _raise_client_error(error)
    return _client_response(service, client)


@router.get("/{client_id}", response_model=ClientResponse)
def get_client(
    client_id: UUID,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> ClientResponse:
    service = _service(session, settings)
    try:
        client = service.get_authorized(context, client_id)
    except ClientError as error:
        _raise_client_error(error)
    return _client_response(service, client)


@router.put("/{client_id}/industries", response_model=ClientResponse)
def update_client_industries(
    client_id: UUID,
    body: ClientIndustriesUpdate,
    context: Annotated[RequestContext, Depends(manager_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> ClientResponse:
    service = _service(session, settings)
    try:
        client = service.update_industries(
            context, client_id, body.industry_codes, idempotency_key
        )
    except ClientError as error:
        _raise_client_error(error)
    return _client_response(service, client)


@router.put("/{client_id}/adjacent-industries", response_model=ClientResponse)
def approve_client_adjacency(
    client_id: UUID,
    body: ClientAdjacencyUpdate,
    context: Annotated[RequestContext, Depends(manager_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> ClientResponse:
    service = _service(session, settings)
    try:
        client = service.approve_adjacency(
            context,
            client_id,
            body.industry_code,
            body.adjacent_industry_code,
            idempotency_key,
        )
    except ClientError as error:
        _raise_client_error(error)
    return _client_response(service, client)


@router.post("/{client_id}/grants", response_model=ClientGrantResponse)
def grant_client_access(
    client_id: UUID,
    body: ClientGrantCreate,
    context: Annotated[RequestContext, Depends(manager_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> ClientGrantResponse:
    service = _service(session, settings)
    try:
        grant = service.grant_access(
            context, client_id, body.membership_id, idempotency_key
        )
    except ClientError as error:
        _raise_client_error(error)
    return ClientGrantResponse(
        client_id=grant.client_id, membership_id=grant.membership_id
    )
