import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.identity.models import (
    IdentityIdempotencyKey,
    Membership,
    MembershipInvitation,
    User,
)
from app.identity.schemas import IdentityClaims, Role
from app.identity.service import TenantService


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
