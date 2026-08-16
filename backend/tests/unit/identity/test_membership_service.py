from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.database import Base
from app.identity.models import Membership, MembershipInvitation, User
from app.identity.schemas import IdentityClaims, Role
from app.identity.service import IdentityError, MembershipService, TenantService


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session


def owner_claims() -> IdentityClaims:
    return IdentityClaims(
        subject="oidc|owner-1",
        email="Owner@Agency.test",
        name="Agency Owner",
        email_verified=True,
    )


def test_provision_creates_normalized_owner_identity_and_membership(
    session: Session,
) -> None:
    tenant = TenantService(session).provision("agency", owner_claims())

    owner = session.scalar(select(User).where(User.oidc_subject == "oidc|owner-1"))
    membership = session.scalar(
        select(Membership).where(Membership.tenant_id == tenant.id)
    )

    assert tenant.slug == "agency"
    assert owner is not None
    assert owner.email == "owner@agency.test"
    assert membership is not None
    assert membership.user_id == owner.id
    assert membership.role is Role.OWNER


def test_invitation_stores_only_hmac_and_expires_after_seven_days(
    session: Session,
) -> None:
    tenant = TenantService(session).provision("agency", owner_claims())
    owner = session.scalar(select(User).where(User.oidc_subject == "oidc|owner-1"))
    assert owner is not None
    now = datetime(2026, 8, 15, tzinfo=UTC)

    invitation, bearer_token = MembershipService(session, b"invitation-key").invite(
        tenant_id=tenant.id,
        intended_email="Recruiter@Agency.test",
        role=Role.RECRUITER,
        created_by_user_id=owner.id,
        now=now,
    )

    assert invitation.intended_email == "recruiter@agency.test"
    assert invitation.expires_at == now + timedelta(days=7)
    assert bearer_token.encode() not in invitation.token_hmac
    assert session.scalar(select(MembershipInvitation)) is invitation


def test_claim_invitation_requires_verified_matching_email_and_consumes_once(
    session: Session,
) -> None:
    tenant = TenantService(session).provision("agency", owner_claims())
    owner = session.scalar(select(User).where(User.oidc_subject == "oidc|owner-1"))
    assert owner is not None
    service = MembershipService(session, b"invitation-key")
    now = datetime.now(UTC)
    invitation, token = service.invite(
        tenant_id=tenant.id,
        intended_email="recruiter@agency.test",
        role=Role.RECRUITER,
        created_by_user_id=owner.id,
        now=now,
    )
    claims = IdentityClaims(
        subject="oidc|recruiter-1",
        email="recruiter@agency.test",
        name="Recruiter",
        email_verified=True,
    )

    membership = service.claim_invite(token, claims, now=now)

    assert membership.tenant_id == tenant.id
    assert membership.role is Role.RECRUITER
    assert invitation.claimed_at == now
    with pytest.raises(IdentityError, match="invitation_invalid"):
        service.claim_invite(token, claims, now=now)


@pytest.mark.parametrize(
    "claims",
    [
        IdentityClaims(
            subject="oidc|recruiter-1",
            email="recruiter@agency.test",
            name="Recruiter",
            email_verified=False,
        ),
        IdentityClaims(
            subject="oidc|recruiter-1",
            email="other@agency.test",
            name="Recruiter",
            email_verified=True,
        ),
    ],
)
def test_claim_invitation_rejects_unverified_or_different_email(
    session: Session, claims: IdentityClaims
) -> None:
    tenant = TenantService(session).provision("agency", owner_claims())
    owner = session.scalar(select(User).where(User.oidc_subject == "oidc|owner-1"))
    assert owner is not None
    service = MembershipService(session, b"invitation-key")
    _, token = service.invite(
        tenant_id=tenant.id,
        intended_email="recruiter@agency.test",
        role=Role.RECRUITER,
        created_by_user_id=owner.id,
    )

    with pytest.raises(IdentityError, match="invitation_email_mismatch"):
        service.claim_invite(token, claims)


def test_last_active_owner_cannot_be_demoted_or_deactivated(session: Session) -> None:
    tenant = TenantService(session).provision("agency", owner_claims())
    owner_membership = session.scalar(
        select(Membership).where(Membership.tenant_id == tenant.id)
    )
    assert owner_membership is not None
    service = MembershipService(session, b"invitation-key")

    with pytest.raises(IdentityError, match="last_owner_required"):
        service.change_role(owner_membership, Role.ADMIN)
    with pytest.raises(IdentityError, match="last_owner_required"):
        service.deactivate(owner_membership)
