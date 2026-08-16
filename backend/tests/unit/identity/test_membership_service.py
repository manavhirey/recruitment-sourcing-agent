import json
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.database import Base
from app.identity.models import (
    IdentityIdempotencyKey,
    Membership,
    MembershipInvitation,
    User,
)
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


def test_invitation_retry_returns_original_token_without_duplicate_row(
    session: Session,
) -> None:
    tenant = TenantService(session).provision("agency", owner_claims())
    owner = session.scalar(select(User).where(User.oidc_subject == "oidc|owner-1"))
    assert owner is not None
    service = MembershipService(session, b"invitation-key")

    first = service.invite(
        tenant_id=tenant.id,
        intended_email="recruiter@agency.test",
        role=Role.RECRUITER,
        created_by_user_id=owner.id,
        idempotency_key="invite-recruiter-1",
    )
    second = service.invite(
        tenant_id=tenant.id,
        intended_email="recruiter@agency.test",
        role=Role.RECRUITER,
        created_by_user_id=owner.id,
        idempotency_key="invite-recruiter-1",
    )

    assert second[0].id == first[0].id
    assert second[1] == first[1]
    assert session.scalar(select(func.count()).select_from(MembershipInvitation)) == 1
    assert session.scalar(select(func.count()).select_from(IdentityIdempotencyKey)) == 1
    record = session.scalar(select(IdentityIdempotencyKey))
    assert record is not None
    assert record.key_hmac != b"invite-recruiter-1"
    assert first[1] not in json.dumps(record.response_payload)


def test_invitation_key_reuse_with_different_request_is_rejected(
    session: Session,
) -> None:
    tenant = TenantService(session).provision("agency", owner_claims())
    owner = session.scalar(select(User).where(User.oidc_subject == "oidc|owner-1"))
    assert owner is not None
    service = MembershipService(session, b"invitation-key")
    service.invite(
        tenant_id=tenant.id,
        intended_email="first@agency.test",
        role=Role.RECRUITER,
        created_by_user_id=owner.id,
        idempotency_key="invite-key-reuse",
    )

    with pytest.raises(IdentityError, match="idempotency_conflict"):
        service.invite(
            tenant_id=tenant.id,
            intended_email="second@agency.test",
            role=Role.ADMIN,
            created_by_user_id=owner.id,
            idempotency_key="invite-key-reuse",
        )


def test_invitation_key_is_scoped_to_the_creating_actor(session: Session) -> None:
    tenant = TenantService(session).provision("agency", owner_claims())
    owner = session.scalar(select(User).where(User.oidc_subject == "oidc|owner-1"))
    assert owner is not None
    second_admin = User(
        oidc_subject="oidc|admin-2",
        email="admin@agency.test",
        display_name="Second Admin",
    )
    session.add(second_admin)
    session.flush()
    service = MembershipService(session, b"invitation-key")

    first = service.invite(
        tenant_id=tenant.id,
        intended_email="first@agency.test",
        role=Role.RECRUITER,
        created_by_user_id=owner.id,
        idempotency_key="shared-client-key",
    )
    second = service.invite(
        tenant_id=tenant.id,
        intended_email="second@agency.test",
        role=Role.RECRUITER,
        created_by_user_id=second_admin.id,
        idempotency_key="shared-client-key",
    )

    assert first[0].id != second[0].id
    assert first[1] != second[1]


def test_new_invitation_supersedes_prior_unclaimed_link_for_same_email(
    session: Session,
) -> None:
    tenant = TenantService(session).provision("agency", owner_claims())
    owner = session.scalar(select(User).where(User.oidc_subject == "oidc|owner-1"))
    assert owner is not None
    service = MembershipService(session, b"invitation-key")
    issued_at = datetime(2026, 8, 15, tzinfo=UTC)

    first_invitation, first_token = service.invite(
        tenant_id=tenant.id,
        intended_email="Recruiter@Agency.test",
        role=Role.RECRUITER,
        created_by_user_id=owner.id,
        now=issued_at,
        idempotency_key="invite-recruiter-old",
    )
    second_invitation, second_token = service.invite(
        tenant_id=tenant.id,
        intended_email="recruiter@agency.test",
        role=Role.ADMIN,
        created_by_user_id=owner.id,
        now=issued_at + timedelta(minutes=1),
        idempotency_key="invite-recruiter-new",
    )
    claims = IdentityClaims(
        subject="oidc|recruiter-1",
        email="recruiter@agency.test",
        name="Recruiter",
        email_verified=True,
    )

    with pytest.raises(IdentityError, match="invitation_invalid"):
        service.claim_invite(
            first_token,
            claims,
            now=issued_at + timedelta(minutes=2),
        )
    membership = service.claim_invite(
        second_token,
        claims,
        now=issued_at + timedelta(minutes=2),
    )

    assert first_invitation.expires_at == issued_at + timedelta(minutes=1)
    assert second_invitation.claimed_at is not None
    assert membership.role is Role.ADMIN


def test_invitation_rejects_an_existing_active_member_email(session: Session) -> None:
    tenant = TenantService(session).provision("agency", owner_claims())
    owner = session.scalar(select(User).where(User.oidc_subject == "oidc|owner-1"))
    assert owner is not None

    with pytest.raises(IdentityError, match="membership_already_active"):
        MembershipService(session, b"invitation-key").invite(
            tenant_id=tenant.id,
            intended_email=owner.email,
            role=Role.RECRUITER,
            created_by_user_id=owner.id,
        )


def test_claim_cannot_demote_an_existing_active_owner(session: Session) -> None:
    tenant = TenantService(session).provision("agency", owner_claims())
    owner = session.scalar(select(User).where(User.oidc_subject == "oidc|owner-1"))
    assert owner is not None
    service = MembershipService(session, b"invitation-key")
    _, token = service.invite(
        tenant_id=tenant.id,
        intended_email="future-owner-email@agency.test",
        role=Role.RECRUITER,
        created_by_user_id=owner.id,
    )
    owner.email = "future-owner-email@agency.test"
    session.flush()

    with pytest.raises(IdentityError, match="invitation_invalid"):
        service.claim_invite(
            token,
            IdentityClaims(
                subject=owner.oidc_subject,
                email=owner.email,
                name=owner.display_name,
                email_verified=True,
            ),
        )

    membership = session.scalar(
        select(Membership).where(
            Membership.tenant_id == tenant.id,
            Membership.user_id == owner.id,
        )
    )
    assert membership is not None
    assert membership.role is Role.OWNER
    assert membership.active is True


@pytest.mark.parametrize("mutation", ["change_role", "deactivate"])
def test_membership_mutation_invalidates_outstanding_invitation(
    session: Session, mutation: str
) -> None:
    tenant = TenantService(session).provision("agency", owner_claims())
    owner = session.scalar(select(User).where(User.oidc_subject == "oidc|owner-1"))
    assert owner is not None
    service = MembershipService(session, b"invitation-key")
    issued_at = datetime(2026, 8, 15, tzinfo=UTC)
    invitation, token = service.invite(
        tenant_id=tenant.id,
        intended_email="recruiter@agency.test",
        role=Role.ADMIN,
        created_by_user_id=owner.id,
        now=issued_at,
    )
    invited_user = User(
        oidc_subject="oidc|recruiter-1",
        email="recruiter@agency.test",
        display_name="Recruiter",
    )
    session.add(invited_user)
    session.flush()
    membership = Membership(
        tenant_id=tenant.id,
        user_id=invited_user.id,
        role=Role.RECRUITER,
    )
    session.add(membership)
    session.flush()

    if mutation == "change_role":
        service.change_role(membership, Role.ADMIN)
    else:
        service.deactivate(membership)

    assert invitation.expires_at <= datetime.now(UTC)
    with pytest.raises(IdentityError, match="invitation_invalid"):
        service.claim_invite(
            token,
            IdentityClaims(
                subject=invited_user.oidc_subject,
                email=invited_user.email,
                name=invited_user.display_name,
                email_verified=True,
            ),
        )


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


def test_claim_retry_returns_original_success_after_response_loss(
    session: Session,
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
        idempotency_key="invite-recruiter-1",
    )
    claims = IdentityClaims(
        subject="oidc|recruiter-1",
        email="recruiter@agency.test",
        name="Recruiter",
        email_verified=True,
    )

    first = service.claim_invite(token, claims, idempotency_key="claim-recruiter-1")
    persisted_membership = session.get(Membership, first.membership_id)
    assert persisted_membership is not None
    persisted_membership.role = Role.ADMIN
    session.flush()
    retry = service.claim_invite(token, claims, idempotency_key="claim-recruiter-1")

    assert retry.membership_id == first.membership_id
    assert retry.role is Role.RECRUITER
    assert retry.active is True


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
