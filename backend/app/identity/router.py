import json
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, derive_identity_hmac_key
from app.core.database import get_db
from app.identity.dependencies import (
    apply_tenant_context,
    get_app_settings,
    get_idempotency_key,
    get_identity_claims,
    get_request_context,
    require_role,
)
from app.identity.models import Membership, User
from app.identity.schemas import (
    IdentityClaims,
    InvitationClaim,
    InvitationCreate,
    InvitationResponse,
    MemberResponse,
    MembershipResponse,
    MeResponse,
    RequestContext,
    Role,
    RoleUpdate,
)
from app.identity.service import IdentityError, MembershipResult, MembershipService

router = APIRouter(prefix="/api/v1", tags=["identity"])
manager_context = require_role(Role.OWNER, Role.ADMIN)
_INVITATION_CLAIM_MAX_BYTES = 256


async def get_invitation_claim(request: Request) -> InvitationClaim:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
        "application/json"
    ):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"code": "invitation_claim_content_type_invalid"},
        )
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            too_large = int(declared) > _INVITATION_CLAIM_MAX_BYTES
        except ValueError:
            too_large = True
        if too_large:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"code": "invitation_claim_too_large"},
            )
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > _INVITATION_CLAIM_MAX_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"code": "invitation_claim_too_large"},
            )
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks))
        return InvitationClaim.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invitation_claim_invalid"},
        ) from None


def _allowed_client_ids(membership: Membership) -> frozenset[UUID] | None:
    if membership.allowed_client_ids is None:
        return None
    return frozenset(UUID(value) for value in membership.allowed_client_ids)


def _membership_response(membership: MembershipResult) -> MembershipResponse:
    return MembershipResponse(
        membership_id=membership.membership_id,
        tenant_id=membership.tenant_id,
        user_id=membership.user_id,
        role=membership.role,
        active=membership.active,
    )


def _membership_service(session: Session, settings: Settings) -> MembershipService:
    return MembershipService(
        session,
        derive_identity_hmac_key(settings),
    )


def _raise_identity_error(error: IdentityError) -> NoReturn:
    status_code = {
        "invitation_invalid": status.HTTP_404_NOT_FOUND,
        "invitation_email_mismatch": status.HTTP_403_FORBIDDEN,
        "invitation_role_invalid": status.HTTP_400_BAD_REQUEST,
        "idempotency_key_invalid": status.HTTP_400_BAD_REQUEST,
        "idempotency_actor_required": status.HTTP_400_BAD_REQUEST,
        "idempotency_conflict": status.HTTP_409_CONFLICT,
        "idempotency_result_missing": status.HTTP_409_CONFLICT,
        "last_owner_required": status.HTTP_409_CONFLICT,
        "membership_already_active": status.HTTP_409_CONFLICT,
    }.get(error.code, status.HTTP_400_BAD_REQUEST)
    raise HTTPException(status_code=status_code, detail={"code": error.code}) from error


@router.get("/me", response_model=MeResponse)
def me(
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
) -> MeResponse:
    user = session.get(User, context.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "tenant_not_found"},
        )
    return MeResponse(
        **context.model_dump(),
        display_name=user.display_name,
        email=user.email,
    )


@router.get("/members", response_model=list[MemberResponse])
def list_members(
    context: Annotated[RequestContext, Depends(manager_context)],
    session: Annotated[Session, Depends(get_db)],
) -> list[MemberResponse]:
    rows = session.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.tenant_id == context.tenant_id)
        .order_by(User.email)
    ).all()
    return [
        MemberResponse(
            membership_id=membership.id,
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=membership.role,
            allowed_client_ids=_allowed_client_ids(membership),
            active=membership.active,
        )
        for membership, user in rows
    ]


@router.post(
    "/membership-invitations",
    response_model=InvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_invitation(
    body: InvitationCreate,
    context: Annotated[RequestContext, Depends(manager_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> InvitationResponse:
    try:
        invitation, token = _membership_service(session, settings).invite(
            tenant_id=context.tenant_id,
            intended_email=str(body.email),
            role=body.role,
            created_by_user_id=context.user_id,
            idempotency_key=idempotency_key,
        )
    except IdentityError as error:
        _raise_identity_error(error)
    return InvitationResponse(
        invitation_id=invitation.id,
        token=token,
        expires_at=invitation.expires_at,
    )


@router.post(
    "/membership-invitations/claim",
    response_model=MembershipResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["token"],
                        "properties": {
                            "token": {
                                "type": "string",
                                "minLength": 80,
                                "maxLength": 80,
                            }
                        },
                    }
                }
            },
        }
    },
)
def claim_invitation(
    body: Annotated[InvitationClaim, Depends(get_invitation_claim)],
    claims: Annotated[IdentityClaims, Depends(get_identity_claims)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> MembershipResponse:
    try:
        service = _membership_service(session, settings)
        tenant_id = service.invitation_tenant_id(body.token)
        apply_tenant_context(session, tenant_id)
        membership = service.claim_invite(
            body.token, claims, idempotency_key=idempotency_key
        )
    except IdentityError as error:
        _raise_identity_error(error)
    return _membership_response(membership)


@router.patch("/members/{membership_id}/role", response_model=MembershipResponse)
def change_member_role(
    membership_id: UUID,
    body: RoleUpdate,
    context: Annotated[RequestContext, Depends(manager_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> MembershipResponse:
    if body.role is Role.OWNER:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "role_invalid"},
        )
    membership = session.scalar(
        select(Membership).where(
            Membership.id == membership_id,
            Membership.tenant_id == context.tenant_id,
        )
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "member_not_found"},
        )
    if membership.role is Role.OWNER and context.role is not Role.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden"},
        )
    try:
        result = _membership_service(session, settings).change_role(
            membership,
            body.role,
            idempotency_key=idempotency_key,
            actor_key=str(context.user_id),
        )
    except IdentityError as error:
        _raise_identity_error(error)
    return _membership_response(result)


@router.delete("/members/{membership_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_member(
    membership_id: UUID,
    context: Annotated[RequestContext, Depends(manager_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> Response:
    membership = session.scalar(
        select(Membership).where(
            Membership.id == membership_id,
            Membership.tenant_id == context.tenant_id,
        )
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "member_not_found"},
        )
    if membership.role is Role.OWNER and context.role is not Role.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "forbidden"},
        )
    try:
        _membership_service(session, settings).deactivate(
            membership,
            idempotency_key=idempotency_key,
            actor_key=str(context.user_id),
        )
    except IdentityError as error:
        _raise_identity_error(error)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
