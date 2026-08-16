from collections.abc import Generator
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base, get_db
from app.identity.models import IdentityIdempotencyKey, Membership, User
from app.identity.schemas import IdentityClaims, Role
from app.identity.service import TenantService
from app.main import create_app


class StaticVerifier:
    def __init__(self, claims: IdentityClaims) -> None:
        self.claims = claims

    def verify(self, token: str) -> IdentityClaims:
        return self.claims


@pytest.fixture
def identity_api(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    owner_claims = IdentityClaims(
        subject="oidc|owner-1",
        email="owner@agency.test",
        name="Agency Owner",
        email_verified=True,
    )
    with Session(engine) as session:
        tenant = TenantService(session).provision("agency", owner_claims)
        tenant_id = tenant.id
        session.commit()

    app = create_app(Settings.for_test())
    verifier = StaticVerifier(owner_claims)
    app.state.token_verifier = verifier

    def database_session() -> Generator[Session, None, None]:
        with Session(engine) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    app.dependency_overrides[get_db] = database_session
    monkeypatch.setattr(
        "app.identity.dependencies.apply_tenant_context",
        lambda session, tenant_id: None,
    )
    monkeypatch.setattr(
        "app.identity.router.apply_tenant_context",
        lambda session, tenant_id: None,
    )
    with TestClient(app) as client:
        yield {
            "client": client,
            "engine": engine,
            "tenant_id": tenant_id,
            "verifier": verifier,
        }


def test_me_requires_explicit_tenant_header(identity_api: dict[str, Any]) -> None:
    response = identity_api["client"].get(
        "/api/v1/me", headers={"Authorization": "Bearer signed-token"}
    )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "tenant_required"}}


def test_me_returns_membership_scope_without_oidc_subject(
    identity_api: dict[str, Any],
) -> None:
    response = identity_api["client"].get(
        "/api/v1/me",
        headers={
            "Authorization": "Bearer signed-token",
            "X-Tenant-ID": str(identity_api["tenant_id"]),
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": str(identity_api["tenant_id"]),
        "user_id": response.json()["user_id"],
        "role": "owner",
        "allowed_client_ids": None,
        "display_name": "Agency Owner",
        "email": "owner@agency.test",
    }
    assert "subject" not in response.json()


def test_me_hides_tenant_from_authenticated_non_member(
    identity_api: dict[str, Any],
) -> None:
    response = identity_api["client"].get(
        "/api/v1/me",
        headers={
            "Authorization": "Bearer signed-token",
            "X-Tenant-ID": "d1c89c7c-441f-49fd-9843-c4edfe8e5942",
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "tenant_not_found"}}


def test_mutation_requires_idempotency_key(identity_api: dict[str, Any]) -> None:
    response = identity_api["client"].post(
        "/api/v1/membership-invitations",
        headers={
            "Authorization": "Bearer signed-token",
            "X-Tenant-ID": str(identity_api["tenant_id"]),
        },
        json={"email": "recruiter@agency.test", "role": "recruiter"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "idempotency_key_required"}}


def test_owner_can_invite_and_verified_matching_identity_can_claim(
    identity_api: dict[str, Any],
) -> None:
    tenant_id = str(identity_api["tenant_id"])
    invitation_response = identity_api["client"].post(
        "/api/v1/membership-invitations",
        headers={
            "Authorization": "Bearer signed-token",
            "X-Tenant-ID": tenant_id,
            "Idempotency-Key": "invite-recruiter-1",
        },
        json={"email": "recruiter@agency.test", "role": "recruiter"},
    )
    assert invitation_response.status_code == 201
    invitation_retry = identity_api["client"].post(
        "/api/v1/membership-invitations",
        headers={
            "Authorization": "Bearer signed-token",
            "X-Tenant-ID": tenant_id,
            "Idempotency-Key": "invite-recruiter-1",
        },
        json={"email": "recruiter@agency.test", "role": "recruiter"},
    )
    assert invitation_retry.json() == invitation_response.json()

    identity_api["verifier"].claims = IdentityClaims(
        subject="oidc|recruiter-1",
        email="recruiter@agency.test",
        name="Recruiter",
        email_verified=True,
    )
    claim_response = identity_api["client"].post(
        f"/api/v1/membership-invitations/{invitation_response.json()['token']}/claim",
        headers={
            "Authorization": "Bearer signed-token",
            "Idempotency-Key": "claim-recruiter-1",
        },
    )
    claim_retry = identity_api["client"].post(
        f"/api/v1/membership-invitations/{invitation_response.json()['token']}/claim",
        headers={
            "Authorization": "Bearer signed-token",
            "Idempotency-Key": "claim-recruiter-1",
        },
    )

    assert claim_response.status_code == 200
    assert claim_retry.json() == claim_response.json()
    assert claim_response.json()["role"] == "recruiter"
    with Session(identity_api["engine"]) as session:
        recruiter = session.scalar(
            select(User).where(User.oidc_subject == "oidc|recruiter-1")
        )
        assert recruiter is not None
        membership = session.scalar(
            select(Membership).where(Membership.user_id == recruiter.id)
        )
        assert membership is not None
        assert membership.active is True
        assert (
            session.scalar(select(func.count()).select_from(IdentityIdempotencyKey))
            == 2
        )


def test_role_and_deactivation_retries_do_not_repeat_committed_effects(
    identity_api: dict[str, Any],
) -> None:
    tenant_id = str(identity_api["tenant_id"])
    invite = identity_api["client"].post(
        "/api/v1/membership-invitations",
        headers={
            "Authorization": "Bearer signed-token",
            "X-Tenant-ID": tenant_id,
            "Idempotency-Key": "invite-member-for-mutations",
        },
        json={"email": "member@agency.test", "role": "recruiter"},
    )
    identity_api["verifier"].claims = IdentityClaims(
        subject="oidc|member-1",
        email="member@agency.test",
        name="Member",
        email_verified=True,
    )
    claimed = identity_api["client"].post(
        f"/api/v1/membership-invitations/{invite.json()['token']}/claim",
        headers={
            "Authorization": "Bearer signed-token",
            "Idempotency-Key": "claim-member-for-mutations",
        },
    )
    membership_id = claimed.json()["membership_id"]
    identity_api["verifier"].claims = IdentityClaims(
        subject="oidc|owner-1",
        email="owner@agency.test",
        name="Agency Owner",
        email_verified=True,
    )
    headers = {
        "Authorization": "Bearer signed-token",
        "X-Tenant-ID": tenant_id,
    }

    first_role_change = identity_api["client"].patch(
        f"/api/v1/members/{membership_id}/role",
        headers={**headers, "Idempotency-Key": "set-member-admin"},
        json={"role": "admin"},
    )
    identity_api["client"].patch(
        f"/api/v1/members/{membership_id}/role",
        headers={**headers, "Idempotency-Key": "set-member-recruiter"},
        json={"role": "recruiter"},
    )
    role_retry = identity_api["client"].patch(
        f"/api/v1/members/{membership_id}/role",
        headers={**headers, "Idempotency-Key": "set-member-admin"},
        json={"role": "admin"},
    )

    assert first_role_change.json()["role"] == "admin"
    assert role_retry.json() == first_role_change.json()
    with Session(identity_api["engine"]) as session:
        membership = session.get(Membership, UUID(membership_id))
        assert membership is not None
        assert membership.role is Role.RECRUITER

    first_deactivation = identity_api["client"].delete(
        f"/api/v1/members/{membership_id}",
        headers={**headers, "Idempotency-Key": "deactivate-member"},
    )
    with Session(identity_api["engine"]) as session:
        membership = session.get(Membership, UUID(membership_id))
        assert membership is not None
        membership.active = True
        session.commit()
    deactivation_retry = identity_api["client"].delete(
        f"/api/v1/members/{membership_id}",
        headers={**headers, "Idempotency-Key": "deactivate-member"},
    )

    assert first_deactivation.status_code == 204
    assert deactivation_retry.status_code == 204
    with Session(identity_api["engine"]) as session:
        membership = session.get(Membership, UUID(membership_id))
        assert membership is not None
        assert membership.active is True
