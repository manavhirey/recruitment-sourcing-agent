import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app.core.errors import AppError
from app.identity.models import (
    IdentityIdempotencyKey,
    Membership,
    MembershipInvitation,
    Tenant,
    User,
)
from app.identity.schemas import IdentityClaims, Role


class IdentityError(AppError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def normalize_email(email: str) -> str:
    return email.strip().casefold()


@dataclass(frozen=True)
class MembershipResult:
    membership_id: UUID
    tenant_id: UUID
    user_id: UUID
    role: Role
    active: bool

    @classmethod
    def from_membership(cls, membership: Membership) -> "MembershipResult":
        return cls(
            membership_id=membership.id,
            tenant_id=membership.tenant_id,
            user_id=membership.user_id,
            role=membership.role,
            active=membership.active,
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "MembershipResult":
        return cls(
            membership_id=UUID(payload["membership_id"]),
            tenant_id=UUID(payload["tenant_id"]),
            user_id=UUID(payload["user_id"]),
            role=Role(payload["role"]),
            active=bool(payload["active"]),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "membership_id": str(self.membership_id),
            "tenant_id": str(self.tenant_id),
            "user_id": str(self.user_id),
            "role": self.role.value,
            "active": self.active,
        }


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
        idempotency_key: str | None = None,
    ) -> tuple[MembershipInvitation, str]:
        if role is Role.OWNER:
            raise IdentityError("invitation_role_invalid")
        normalized_email = normalize_email(intended_email)
        idempotency_record = None
        if idempotency_key is not None:
            idempotency_record = self._begin_idempotent_mutation(
                tenant_id=tenant_id,
                actor_key=str(created_by_user_id),
                operation="create_membership_invitation",
                idempotency_key=idempotency_key,
                request_payload={"email": normalized_email, "role": role.value},
            )
            if idempotency_record.response_payload is not None:
                invitation = self._session.get(
                    MembershipInvitation,
                    UUID(idempotency_record.response_payload["invitation_id"]),
                )
                if invitation is None:
                    raise IdentityError("idempotency_result_missing")
                set_committed_value(
                    invitation,
                    "expires_at",
                    datetime.fromisoformat(
                        idempotency_record.response_payload["expires_at"]
                    ),
                )
                return invitation, self._invitation_token(
                    tenant_id, created_by_user_id, idempotency_key
                )

        issued_at = now or datetime.now(UTC)
        bearer_token = (
            self._invitation_token(tenant_id, created_by_user_id, idempotency_key)
            if idempotency_key is not None
            else f"{tenant_id}.{secrets.token_urlsafe(32)}"
        )
        invitation = MembershipInvitation(
            tenant_id=tenant_id,
            token_hmac=self._token_hmac(bearer_token),
            intended_email=normalized_email,
            role=role,
            created_by_user_id=created_by_user_id,
            expires_at=issued_at + timedelta(days=7),
        )
        self._session.add(invitation)
        self._session.flush()
        if idempotency_record is not None:
            self._complete_idempotent_mutation(
                idempotency_record,
                {
                    "invitation_id": str(invitation.id),
                    "expires_at": invitation.expires_at.isoformat(),
                },
            )
        return invitation, bearer_token

    def claim_invite(
        self,
        token: str,
        claims: IdentityClaims,
        *,
        now: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> MembershipResult:
        tenant_id = self.invitation_tenant_id(token)
        idempotency_record = None
        if idempotency_key is not None:
            idempotency_record = self._begin_idempotent_mutation(
                tenant_id=tenant_id,
                actor_key=claims.subject,
                operation="claim_membership_invitation",
                idempotency_key=idempotency_key,
                request_payload={"token_hmac": self._token_hmac(token).hex()},
            )
            if idempotency_record.response_payload is not None:
                return MembershipResult.from_payload(
                    idempotency_record.response_payload
                )

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
        result = MembershipResult.from_membership(membership)
        if idempotency_record is not None:
            self._complete_idempotent_mutation(idempotency_record, result.to_payload())
        return result

    def change_role(
        self,
        membership: Membership,
        role: Role,
        *,
        idempotency_key: str | None = None,
        actor_key: str | None = None,
    ) -> MembershipResult:
        idempotency_record = self._membership_mutation_record(
            membership=membership,
            operation="change_membership_role",
            request_payload={"role": role.value},
            idempotency_key=idempotency_key,
            actor_key=actor_key,
        )
        if (
            idempotency_record is not None
            and idempotency_record.response_payload is not None
        ):
            return MembershipResult.from_payload(idempotency_record.response_payload)
        if membership.role is Role.OWNER and role is not Role.OWNER:
            self._ensure_another_active_owner(membership)
        membership.role = role
        self._session.flush()
        result = MembershipResult.from_membership(membership)
        if idempotency_record is not None:
            self._complete_idempotent_mutation(idempotency_record, result.to_payload())
        return result

    def deactivate(
        self,
        membership: Membership,
        *,
        idempotency_key: str | None = None,
        actor_key: str | None = None,
    ) -> MembershipResult:
        idempotency_record = self._membership_mutation_record(
            membership=membership,
            operation="deactivate_membership",
            request_payload={},
            idempotency_key=idempotency_key,
            actor_key=actor_key,
        )
        if (
            idempotency_record is not None
            and idempotency_record.response_payload is not None
        ):
            return MembershipResult.from_payload(idempotency_record.response_payload)
        if membership.role is Role.OWNER:
            self._ensure_another_active_owner(membership)
        membership.active = False
        self._session.flush()
        result = MembershipResult.from_membership(membership)
        if idempotency_record is not None:
            self._complete_idempotent_mutation(idempotency_record, result.to_payload())
        return result

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

    def _invitation_token(
        self, tenant_id: UUID, actor_id: UUID, idempotency_key: str
    ) -> str:
        secret = hmac.digest(
            self._hmac_key,
            (
                f"membership-invitation\0{tenant_id}\0{actor_id}\0{idempotency_key}"
            ).encode(),
            hashlib.sha256,
        )
        encoded_secret = base64.urlsafe_b64encode(secret).rstrip(b"=").decode()
        return f"{tenant_id}.{encoded_secret}"

    def _membership_mutation_record(
        self,
        *,
        membership: Membership,
        operation: str,
        request_payload: dict[str, Any],
        idempotency_key: str | None,
        actor_key: str | None,
    ) -> IdentityIdempotencyKey | None:
        if idempotency_key is None:
            return None
        if actor_key is None:
            raise IdentityError("idempotency_actor_required")
        return self._begin_idempotent_mutation(
            tenant_id=membership.tenant_id,
            actor_key=actor_key,
            operation=f"{operation}:{membership.id}",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )

    def _begin_idempotent_mutation(
        self,
        *,
        tenant_id: UUID,
        actor_key: str,
        operation: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
    ) -> IdentityIdempotencyKey:
        if not idempotency_key.strip() or len(idempotency_key) > 255:
            raise IdentityError("idempotency_key_invalid")
        key_hmac = self._namespaced_hmac("idempotency-key", idempotency_key)
        actor_hmac = self._namespaced_hmac("idempotency-actor", actor_key)
        request_hmac = hashlib.sha256(
            json.dumps(
                request_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).digest()

        if self._session.get_bind().dialect.name == "postgresql":
            lock_id = int.from_bytes(key_hmac[:8], "big", signed=True)
            self._session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": lock_id},
            )

        record = self._session.scalar(
            select(IdentityIdempotencyKey)
            .where(
                IdentityIdempotencyKey.tenant_id == tenant_id,
                IdentityIdempotencyKey.actor_hmac == actor_hmac,
                IdentityIdempotencyKey.operation == operation,
                IdentityIdempotencyKey.key_hmac == key_hmac,
            )
            .with_for_update()
        )
        if record is not None:
            if not hmac.compare_digest(record.request_hmac, request_hmac):
                raise IdentityError("idempotency_conflict")
            return record

        record = IdentityIdempotencyKey(
            id=uuid4(),
            tenant_id=tenant_id,
            actor_hmac=actor_hmac,
            operation=operation,
            key_hmac=key_hmac,
            request_hmac=request_hmac,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def _complete_idempotent_mutation(
        self,
        record: IdentityIdempotencyKey,
        response_payload: dict[str, Any],
    ) -> None:
        record.response_payload = response_payload
        self._session.flush()

    def begin_idempotent_mutation(
        self,
        *,
        tenant_id: UUID,
        actor_key: str,
        operation: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
    ) -> IdentityIdempotencyKey:
        return self._begin_idempotent_mutation(
            tenant_id=tenant_id,
            actor_key=actor_key,
            operation=operation,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )

    def complete_idempotent_mutation(
        self,
        record: IdentityIdempotencyKey,
        response_payload: dict[str, Any],
    ) -> None:
        self._complete_idempotent_mutation(record, response_payload)

    def _namespaced_hmac(self, namespace: str, value: str) -> bytes:
        return hmac.digest(
            self._hmac_key,
            f"{namespace}\0{value}".encode(),
            hashlib.sha256,
        )

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
