import hashlib
import threading
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.identity.models import (
    IdentityIdempotencyKey,
    Membership,
    MembershipInvitation,
    User,
)
from app.identity.schemas import IdentityClaims, Role
from app.identity.service import MembershipService, TenantService


def test_tenant_row_policy_hides_other_tenant(session, tenant_factory) -> None:
    current_user, table_owner = session.execute(
        text(
            "SELECT current_user, "
            "(SELECT tableowner FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename = 'tenants')"
        )
    ).one()
    assert current_user == "sourcing_api_test"
    assert table_owner != current_user

    first = tenant_factory(slug="first")
    second = tenant_factory(slug="second")
    session.commit()

    session.execute(
        text("SELECT set_config('app.tenant_id', :value, true)"),
        {"value": str(first.id)},
    )
    visible = (
        session.execute(text("SELECT id FROM tenants ORDER BY slug")).scalars().all()
    )

    assert visible == [first.id]
    assert second.id not in visible


def test_child_row_policies_hide_memberships_and_invitations_from_other_tenant(
    session, owner_session, tenant_factory
) -> None:
    first = tenant_factory(slug="first-child")
    second = tenant_factory(slug="second-child")
    first_user = User(
        id=uuid4(),
        oidc_subject="oidc|first-user",
        email="first@agency.test",
        display_name="First User",
    )
    second_user = User(
        id=uuid4(),
        oidc_subject="oidc|second-user",
        email="second@agency.test",
        display_name="Second User",
    )
    first_membership = Membership(
        id=uuid4(), tenant_id=first.id, user_id=first_user.id, role=Role.OWNER
    )
    second_membership = Membership(
        id=uuid4(), tenant_id=second.id, user_id=second_user.id, role=Role.OWNER
    )
    first_invitation = MembershipInvitation(
        id=uuid4(),
        tenant_id=first.id,
        token_hmac=hashlib.sha256(b"first").digest(),
        intended_email="invitee@first.test",
        role=Role.RECRUITER,
        created_by_user_id=first_user.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    second_invitation = MembershipInvitation(
        id=uuid4(),
        tenant_id=second.id,
        token_hmac=hashlib.sha256(b"second").digest(),
        intended_email="invitee@second.test",
        role=Role.RECRUITER,
        created_by_user_id=second_user.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    first_idempotency = IdentityIdempotencyKey(
        id=uuid4(),
        tenant_id=first.id,
        actor_hmac=hashlib.sha256(b"first-actor").digest(),
        operation="test",
        key_hmac=hashlib.sha256(b"first-key").digest(),
        request_hmac=hashlib.sha256(b"first-request").digest(),
        response_payload={"status": "first"},
    )
    second_idempotency = IdentityIdempotencyKey(
        id=uuid4(),
        tenant_id=second.id,
        actor_hmac=hashlib.sha256(b"second-actor").digest(),
        operation="test",
        key_hmac=hashlib.sha256(b"second-key").digest(),
        request_hmac=hashlib.sha256(b"second-request").digest(),
        response_payload={"status": "second"},
    )
    owner_session.add_all((first_user, second_user))
    owner_session.flush()
    owner_session.add_all(
        (
            first_membership,
            second_membership,
            first_invitation,
            second_invitation,
            first_idempotency,
            second_idempotency,
        )
    )
    owner_session.commit()

    session.execute(
        text("SELECT set_config('app.tenant_id', :value, true)"),
        {"value": str(first.id)},
    )
    membership_ids = session.execute(text("SELECT id FROM memberships")).scalars().all()
    invitation_ids = (
        session.execute(text("SELECT id FROM membership_invitations")).scalars().all()
    )
    idempotency_ids = (
        session.execute(text("SELECT id FROM identity_idempotency_keys"))
        .scalars()
        .all()
    )

    assert membership_ids == [first_membership.id]
    assert invitation_ids == [first_invitation.id]
    assert idempotency_ids == [first_idempotency.id]


def test_migration_owner_can_provision_through_forced_policies(owner_engine) -> None:
    migration_role = "sourcing_migration_test"
    migration_password = "sourcing-migration-test"
    with owner_engine.begin() as connection:
        connection.execute(
            text(f"CREATE ROLE {migration_role} LOGIN PASSWORD '{migration_password}'")
        )
        connection.execute(text(f"ALTER TABLE tenants OWNER TO {migration_role}"))
        connection.execute(text(f"ALTER TABLE users OWNER TO {migration_role}"))
        connection.execute(text(f"ALTER TABLE memberships OWNER TO {migration_role}"))

    migration_url = make_url(str(owner_engine.url)).set(
        username=migration_role, password=migration_password
    )
    migration_engine = create_engine(migration_url)
    try:
        with Session(migration_engine) as migration_session, migration_session.begin():
            tenant = TenantService(migration_session).provision(
                "provisioned",
                IdentityClaims(
                    subject="oidc|provisioned-owner",
                    email="owner@provisioned.test",
                    name="Provisioned Owner",
                    email_verified=True,
                ),
            )
            provisioned_slug = tenant.slug
        assert provisioned_slug == "provisioned"
    finally:
        migration_engine.dispose()
        with owner_engine.begin() as connection:
            connection.execute(text("ALTER TABLE memberships OWNER TO postgres"))
            connection.execute(text("ALTER TABLE users OWNER TO postgres"))
            connection.execute(text("ALTER TABLE tenants OWNER TO postgres"))
            connection.execute(
                text(
                    "DELETE FROM memberships WHERE tenant_id IN "
                    "(SELECT id FROM tenants WHERE slug = 'provisioned')"
                )
            )
            connection.execute(
                text("DELETE FROM users WHERE oidc_subject = 'oidc|provisioned-owner'")
            )
            connection.execute(text("DELETE FROM tenants WHERE slug = 'provisioned'"))
            connection.execute(text(f"DROP ROLE {migration_role}"))


def test_concurrent_invites_leave_only_one_current_link(
    owner_engine, owner_session: Session
) -> None:
    suffix = uuid4().hex
    tenant = TenantService(owner_session).provision(
        f"concurrent-invites-{suffix[:8]}",
        IdentityClaims(
            subject=f"oidc|concurrent-owner-{suffix}",
            email="owner@concurrent.test",
            name="Concurrent Owner",
            email_verified=True,
        ),
    )
    owner = owner_session.scalar(select(User).where(User.oidc_subject.endswith(suffix)))
    assert owner is not None
    tenant_id = tenant.id
    owner_id = owner.id
    owner_session.commit()

    barrier = threading.Barrier(2)
    failures: list[BaseException] = []
    invitation_ids = []

    class OverlappingMembershipService(MembershipService):
        def _lock_invitation_email(
            self, tenant_id: UUID, normalized_email: str
        ) -> None:
            barrier.wait(timeout=5)
            super()._lock_invitation_email(tenant_id, normalized_email)

    def issue_invitation(index: int) -> None:
        factory = sessionmaker(bind=owner_engine, expire_on_commit=False)
        try:
            with factory.begin() as worker_session:
                invitation, _ = OverlappingMembershipService(
                    worker_session, b"invitation-key"
                ).invite(
                    tenant_id=tenant_id,
                    intended_email="Recruiter@Concurrent.test",
                    role=Role.RECRUITER if index == 0 else Role.ADMIN,
                    created_by_user_id=owner_id,
                    idempotency_key=f"concurrent-invite-{index}",
                )
                invitation_ids.append(invitation.id)
        except Exception as error:  # noqa: BLE001 - worker errors are asserted centrally
            failures.append(error)

    threads = [
        threading.Thread(target=issue_invitation, args=(index,)) for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert len(invitation_ids) == 2
    current_count = owner_session.scalar(
        select(func.count())
        .select_from(MembershipInvitation)
        .where(
            MembershipInvitation.tenant_id == tenant_id,
            MembershipInvitation.intended_email == "recruiter@concurrent.test",
            MembershipInvitation.claimed_at.is_(None),
            MembershipInvitation.expires_at > datetime.now(UTC),
        )
    )

    assert current_count == 1
