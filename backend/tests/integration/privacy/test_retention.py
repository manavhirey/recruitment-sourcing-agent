import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.candidates.contacts import ContactCipher, ContactService
from app.candidates.models import Candidate
from app.clients.models import ClientCompany  # noqa: F401
from app.core.database import Base
from app.identity.models import Tenant
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardVersion  # noqa: F401
from app.providers.base import ProviderContact


def _cipher() -> ContactCipher:
    return ContactCipher(base64.b64encode(b"r" * 32).decode(), b"retention-lookup")


def test_provider_shorter_retention_wins_and_use_never_extends_its_ceiling() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as session:
        tenant = Tenant(slug=f"retention-short-{uuid4()}")
        session.add(tenant)
        session.flush()
        candidate = Candidate(
            tenant_id=tenant.id,
            full_name="Priya Sharma",
            normalized_name="priya sharma",
        )
        session.add(candidate)
        session.flush()
        context = RequestContext(
            tenant_id=tenant.id,
            user_id=uuid4(),
            role=Role.OWNER,
        )
        service = ContactService(session, _cipher())
        point = service.store(
            context,
            candidate.id,
            ProviderContact(
                kind="email",
                value="priya@example.test",
                verification_state="verified",
                observed_at=observed_at,
                retention_days=30,
            ),
            processed_at=observed_at,
        ).contact_point

        assert point.retention_days == 30
        assert point.expires_at == observed_at + timedelta(days=30)
        assert (
            service.reveal(
                context,
                point.id,
                used_at=observed_at + timedelta(days=10),
            )
            == "priya@example.test"
        )
        assert point.expires_at == observed_at + timedelta(days=40)

    engine.dispose()


def test_later_shorter_provider_policy_uses_verified_timestamp_not_observation() -> (
    None
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    verified_at = datetime(2026, 1, 1, tzinfo=UTC)
    observed_again_at = verified_at + timedelta(days=40)
    with Session(engine) as session:
        tenant = Tenant(slug=f"retention-shorten-{uuid4()}")
        session.add(tenant)
        session.flush()
        candidate = Candidate(
            tenant_id=tenant.id,
            full_name="Shorter Policy",
            normalized_name="shorter policy",
        )
        session.add(candidate)
        session.flush()
        context = RequestContext(
            tenant_id=tenant.id,
            user_id=uuid4(),
            role=Role.OWNER,
        )
        service = ContactService(session, _cipher())
        point = service.store(
            context,
            candidate.id,
            ProviderContact(
                kind="email",
                value="shorter@example.test",
                verification_state="verified",
                observed_at=verified_at,
            ),
            processed_at=verified_at,
        ).contact_point

        service.store(
            context,
            candidate.id,
            ProviderContact(
                kind="email",
                value="shorter@example.test",
                verification_state="unverified",
                observed_at=observed_again_at,
                retention_days=30,
            ),
            processed_at=observed_again_at,
        )

        assert point.retention_days == 30
        assert point.expires_at == verified_at + timedelta(days=30)

    engine.dispose()


def test_older_observation_still_shortens_provider_policy_without_replacing_value() -> (
    None
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    verified_at = datetime(2026, 1, 10, tzinfo=UTC)
    older_observation = verified_at - timedelta(days=5)
    with Session(engine) as session:
        tenant = Tenant(slug=f"retention-older-{uuid4()}")
        session.add(tenant)
        session.flush()
        candidate = Candidate(
            tenant_id=tenant.id,
            full_name="Older Policy",
            normalized_name="older policy",
        )
        session.add(candidate)
        session.flush()
        context = RequestContext(
            tenant_id=tenant.id,
            user_id=uuid4(),
            role=Role.OWNER,
        )
        service = ContactService(session, _cipher())
        point = service.store(
            context,
            candidate.id,
            ProviderContact(
                kind="email",
                value="older@example.test",
                verification_state="verified",
                observed_at=verified_at,
            ),
            processed_at=verified_at,
        ).contact_point

        service.store(
            context,
            candidate.id,
            ProviderContact(
                kind="email",
                value="older@example.test",
                verification_state="unverified",
                observed_at=older_observation,
                retention_days=30,
            ),
            processed_at=verified_at,
        )

        assert point.observed_at == verified_at
        assert point.retention_days == 30
        assert point.expires_at == verified_at + timedelta(days=30)

    engine.dispose()


def test_platform_contact_retention_is_exactly_180_days_with_null_fallback() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    observed_at = datetime(2026, 2, 1, tzinfo=UTC)
    with Session(engine) as session:
        tenant = Tenant(slug=f"retention-default-{uuid4()}")
        session.add(tenant)
        session.flush()
        candidate = Candidate(
            tenant_id=tenant.id,
            full_name="Null Timestamp",
            normalized_name="null timestamp",
        )
        session.add(candidate)
        session.flush()
        context = RequestContext(
            tenant_id=tenant.id,
            user_id=uuid4(),
            role=Role.OWNER,
        )
        point = (
            ContactService(session, _cipher())
            .store(
                context,
                candidate.id,
                ProviderContact(
                    kind="phone",
                    value="+1 212 555 0112",
                    observed_at=observed_at,
                    retention_days=365,
                ),
                processed_at=observed_at,
            )
            .contact_point
        )

        assert point.retention_days == 180
        assert point.last_verified_at is None
        assert point.last_used_at is None
        assert point.expires_at == observed_at + timedelta(days=180)

    engine.dispose()
