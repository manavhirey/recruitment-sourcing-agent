import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.identity.models import Membership, MembershipInvitation, Tenant, User
from app.identity.schemas import IdentityClaims, Role


class IdentityError(AppError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def normalize_email(email: str) -> str:
    return email.strip().casefold()


class TenantService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def provision(self, slug: str, owner_claims: IdentityClaims) -> Tenant:
        tenant = Tenant(id=uuid4(), slug=slug.strip().casefold())
        if self._session.get_bind().dialect.name == "postgresql":
            self._session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant.id)},
            )
        owner = User(
            id=uuid4(),
            oidc_subject=owner_claims.subject,
            email=normalize_email(str(owner_claims.email)),
            display_name=owner_claims.name,
        )
        self._session.add_all((tenant, owner))
        self._session.flush()
        membership = Membership(
            tenant_id=tenant.id,
            user_id=owner.id,
            role=Role.OWNER,
        )
        self._session.add(membership)
        self._session.flush()
        return tenant


class MembershipService:
    def __init__(self, session: Session, hmac_key: bytes) -> None:
        self._session = session
        self._hmac_key = hmac_key

    def invite(
        self,
        *,
        tenant_id: UUID,
        intended_email: str,
        role: Role,
        created_by_user_id: UUID,
        now: datetime | None = None,
    ) -> tuple[MembershipInvitation, str]:
        if role is Role.OWNER:
            raise IdentityError("invitation_role_invalid")
        issued_at = now or datetime.now(UTC)
        bearer_token = f"{tenant_id}.{secrets.token_urlsafe(32)}"
        invitation = MembershipInvitation(
            tenant_id=tenant_id,
            token_hmac=self._token_hmac(bearer_token),
            intended_email=normalize_email(intended_email),
            role=role,
            created_by_user_id=created_by_user_id,
            expires_at=issued_at + timedelta(days=7),
        )
        self._session.add(invitation)
        self._session.flush()
        return invitation, bearer_token

    def claim_invite(
        self,
        token: str,
        claims: IdentityClaims,
        *,
        now: datetime | None = None,
    ) -> Membership:
        tenant_id = self.invitation_tenant_id(token)
        invitation = self._session.scalar(
            select(MembershipInvitation)
            .where(
                MembershipInvitation.tenant_id == tenant_id,
                MembershipInvitation.token_hmac == self._token_hmac(token),
            )
            .with_for_update()
        )
        if invitation is None or invitation.claimed_at is not None:
            raise IdentityError("invitation_invalid")

        claimed_at = now or datetime.now(UTC)
        expires_at = invitation.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= claimed_at:
            raise IdentityError("invitation_invalid")
        if (
            not claims.email_verified
            or normalize_email(str(claims.email)) != invitation.intended_email
        ):
            raise IdentityError("invitation_email_mismatch")

        user = self._session.scalar(
            select(User).where(User.oidc_subject == claims.subject)
        )
        if user is None:
            user = User(
                oidc_subject=claims.subject,
                email=normalize_email(str(claims.email)),
                display_name=claims.name,
            )
            self._session.add(user)
            self._session.flush()
        else:
            user.email = normalize_email(str(claims.email))
            user.display_name = claims.name

        membership = self._session.scalar(
            select(Membership).where(
                Membership.tenant_id == invitation.tenant_id,
                Membership.user_id == user.id,
            )
        )
        if membership is None:
            membership = Membership(
                tenant_id=invitation.tenant_id,
                user_id=user.id,
                role=invitation.role,
            )
            self._session.add(membership)
        else:
            membership.role = invitation.role
            membership.active = True

        invitation.claimed_at = claimed_at
        invitation.claimed_by_user_id = user.id
        self._session.flush()
        return membership

    def change_role(self, membership: Membership, role: Role) -> Membership:
        if membership.role is Role.OWNER and role is not Role.OWNER:
            self._ensure_another_active_owner(membership)
        membership.role = role
        self._session.flush()
        return membership

    def deactivate(self, membership: Membership) -> Membership:
        if membership.role is Role.OWNER:
            self._ensure_another_active_owner(membership)
        membership.active = False
        self._session.flush()
        return membership

    @staticmethod
    def invitation_tenant_id(token: str) -> UUID:
        try:
            tenant_value, secret = token.split(".", 1)
            if not secret:
                raise ValueError
            return UUID(tenant_value)
        except (ValueError, AttributeError) as error:
            raise IdentityError("invitation_invalid") from error

    def _token_hmac(self, token: str) -> bytes:
        return hmac.digest(self._hmac_key, token.encode(), hashlib.sha256)

    def _ensure_another_active_owner(self, membership: Membership) -> None:
        active_owner_ids = self._session.scalars(
            select(Membership.id)
            .where(
                Membership.tenant_id == membership.tenant_id,
                Membership.role == Role.OWNER,
                Membership.active.is_(True),
            )
            .with_for_update()
        ).all()
        if not any(owner_id != membership.id for owner_id in active_owner_ids):
            raise IdentityError("last_owner_required")
