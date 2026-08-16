from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.identity.dependencies import (
    apply_tenant_context,
    get_app_settings,
    get_identity_claims,
    get_request_context,
    require_role,
)
from app.identity.models import Membership, User
from app.identity.schemas import (
    IdentityClaims,
    InvitationCreate,
    InvitationResponse,
    MemberResponse,
    MembershipResponse,
    MeResponse,
    RequestContext,
    Role,
    RoleUpdate,
)
from app.identity.service import IdentityError, MembershipService

router = APIRouter(prefix="/api/v1", tags=["identity"])
manager_context = require_role(Role.OWNER, Role.ADMIN)


def _allowed_client_ids(membership: Membership) -> frozenset[UUID] | None:
    if membership.allowed_client_ids is None:
        return None
    return frozenset(UUID(value) for value in membership.allowed_client_ids)


def _membership_response(membership: Membership) -> MembershipResponse:
    return MembershipResponse(
        membership_id=membership.id,
        tenant_id=membership.tenant_id,
        user_id=membership.user_id,
        role=membership.role,
        active=membership.active,
    )


def _membership_service(session: Session, settings: Settings) -> MembershipService:
    return MembershipService(
        session,
        settings.suppression_hmac_key.get_secret_value().encode(),
    )


def _raise_identity_error(error: IdentityError) -> NoReturn:
    status_code = {
        "invitation_invalid": status.HTTP_404_NOT_FOUND,
        "invitation_email_mismatch": status.HTTP_403_FORBIDDEN,
        "invitation_role_invalid": status.HTTP_400_BAD_REQUEST,
        "last_owner_required": status.HTTP_409_CONFLICT,
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
) -> InvitationResponse:
    try:
        invitation, token = _membership_service(session, settings).invite(
            tenant_id=context.tenant_id,
            intended_email=str(body.email),
            role=body.role,
            created_by_user_id=context.user_id,
        )
    except IdentityError as error:
        _raise_identity_error(error)
    return InvitationResponse(
        invitation_id=invitation.id,
        token=token,
        expires_at=invitation.expires_at,
    )


@router.post(
    "/membership-invitations/{token}/claim",
    response_model=MembershipResponse,
)
def claim_invitation(
    token: str,
    claims: Annotated[IdentityClaims, Depends(get_identity_claims)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> MembershipResponse:
    try:
        service = _membership_service(session, settings)
        tenant_id = service.invitation_tenant_id(token)
        apply_tenant_context(session, tenant_id)
        membership = service.claim_invite(token, claims)
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
        _membership_service(session, settings).change_role(membership, body.role)
    except IdentityError as error:
        _raise_identity_error(error)
    return _membership_response(membership)


@router.delete("/members/{membership_id}", status_code=status.HTTP_204_NO_CONTENT)
def deactivate_member(
    membership_id: UUID,
    context: Annotated[RequestContext, Depends(manager_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
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
        _membership_service(session, settings).deactivate(membership)
    except IdentityError as error:
        _raise_identity_error(error)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
