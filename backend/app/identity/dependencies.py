from collections.abc import Callable
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.identity.models import Membership, User
from app.identity.schemas import IdentityClaims, RequestContext, Role


class Verifier(Protocol):
    def verify(self, token: str) -> IdentityClaims: ...


bearer_scheme = HTTPBearer(auto_error=False)


def apply_tenant_context(session: Session, tenant_id: UUID) -> None:
    session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )


def get_token_verifier(request: Request) -> Verifier:
    return request.app.state.token_verifier


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_idempotency_key(request: Request) -> str:
    idempotency_key = request.headers.get("Idempotency-Key")
    if (
        idempotency_key is None
        or not idempotency_key.strip()
        or len(idempotency_key) > 255
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "idempotency_key_required"},
        )
    return idempotency_key


def get_identity_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    verifier: Annotated[Verifier, Depends(get_token_verifier)],
) -> IdentityClaims:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return verifier.verify(credentials.credentials)


def get_request_context(
    request: Request,
    claims: Annotated[IdentityClaims, Depends(get_identity_claims)],
    session: Annotated[Session, Depends(get_db)],
) -> RequestContext:
    tenant_header = request.headers.get("X-Tenant-ID")
    try:
        tenant_id = UUID(tenant_header) if tenant_header else None
    except ValueError:
        tenant_id = None
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "tenant_required"},
        )

    apply_tenant_context(session, tenant_id)
    membership = session.scalar(
        select(Membership)
        .join(User, User.id == Membership.user_id)
        .where(
            Membership.tenant_id == tenant_id,
            User.oidc_subject == claims.subject,
            Membership.active.is_(True),
        )
    )
    if membership is None:
        metrics = getattr(request.app.state, "metrics", None)
        if metrics is not None:
            metrics.cross_tenant_denials.inc()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "tenant_not_found"},
        )

    allowed_client_ids = (
        frozenset(UUID(value) for value in membership.allowed_client_ids)
        if membership.allowed_client_ids is not None
        else None
    )
    context = RequestContext(
        tenant_id=tenant_id,
        user_id=membership.user_id,
        role=membership.role,
        allowed_client_ids=allowed_client_ids,
    )
    request.state.request_context = context
    return context


def require_role(*roles: Role) -> Callable[..., RequestContext]:
    def role_dependency(
        context: Annotated[RequestContext, Depends(get_request_context)],
    ) -> RequestContext:
        if context.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "forbidden"},
            )
        return context

    return role_dependency
