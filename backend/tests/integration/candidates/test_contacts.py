import base64
import inspect
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent  # noqa: F401
from app.candidates.contacts import (
    ContactCipher,
    ContactService,
    expire_due_contacts,
    reveal_candidate_contact,
)
from app.candidates.models import (
    Candidate,
    ContactPoint,
    DuplicateSuggestion,
    SourceIdentity,
)
from app.clients.models import ClientCompany
from app.core.database import Base
from app.identity.models import Tenant, User
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardVersion
from app.providers.base import ProviderContact
from app.sourcing.models import RunCandidate, SourcingRun
from app.sourcing.state_machine import RunState


def _cipher() -> ContactCipher:
    return ContactCipher(base64.b64encode(b"c" * 32).decode(), b"contact-lookup")


def _context(tenant_id) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, user_id=uuid4(), role=Role.OWNER)


def _candidate(session: Session, tenant_id, name: str) -> Candidate:
    candidate = Candidate(
        tenant_id=tenant_id,
        full_name=name,
        normalized_name=name.casefold(),
    )
    session.add(candidate)
    session.flush()
    return candidate


def test_contact_service_stores_only_ciphertext_and_extends_retention_on_reveal() -> (
    None
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    observed_at = datetime(2026, 8, 16, 12, tzinfo=UTC)
    with Session(engine) as session:
        tenant = Tenant(slug=f"contact-{uuid4()}")
        session.add(tenant)
        session.flush()
        candidate = _candidate(session, tenant.id, "Priya Sharma")
        service = ContactService(session, _cipher())

        result = service.store(
            _context(tenant.id),
            candidate.id,
            ProviderContact(
                kind="email",
                value="Priya@Example.com",
                verification_state="verified",
                observed_at=observed_at,
            ),
        )
        session.flush()

        point = result.contact_point
        assert point.value_ciphertext != b"priya@example.com"
        assert point.expires_at == observed_at + timedelta(days=180)
        assert not hasattr(point, "value")
        revealed = service.reveal(
            _context(tenant.id), point.id, used_at=observed_at + timedelta(days=10)
        )
        assert revealed == "priya@example.com"
        assert point.expires_at == observed_at + timedelta(days=190)

    engine.dispose()


def test_contact_retention_does_not_extend_for_observation_but_does_for_verification() -> (
    None
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as session:
        tenant = Tenant(slug=f"retention-{uuid4()}")
        session.add(tenant)
        session.flush()
        candidate = _candidate(session, tenant.id, "Priya Sharma")
        service = ContactService(session, _cipher())
        context = _context(tenant.id)
        first = service.store(
            context,
            candidate.id,
            ProviderContact(
                kind="email",
                value="priya@example.com",
                verification_state="unverified",
                observed_at=observed_at,
            ),
            processed_at=observed_at,
        ).contact_point
        original_expiry = first.expires_at

        service.store(
            context,
            candidate.id,
            ProviderContact(
                kind="email",
                value="priya@example.com",
                verification_state="unverified",
                observed_at=observed_at + timedelta(days=10),
            ),
            processed_at=observed_at + timedelta(days=10),
        )
        assert first.expires_at == original_expiry

        service.store(
            context,
            candidate.id,
            ProviderContact(
                kind="email",
                value="priya@example.com",
                verification_state="verified",
                observed_at=observed_at + timedelta(days=20),
            ),
            processed_at=observed_at + timedelta(days=20),
        )
        assert first.expires_at == observed_at + timedelta(days=200)

    engine.dispose()


def test_contact_reveal_is_allowed_before_but_not_at_or_after_utc_deadline() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as session:
        tenant = Tenant(slug=f"deadline-{uuid4()}")
        session.add(tenant)
        session.flush()
        candidate = _candidate(session, tenant.id, "Priya Sharma")
        service = ContactService(session, _cipher())
        context = _context(tenant.id)
        point = service.store(
            context,
            candidate.id,
            ProviderContact(
                kind="email",
                value="priya@example.com",
                verification_state="verified",
                observed_at=observed_at,
            ),
            processed_at=observed_at,
        ).contact_point
        deadline = point.expires_at

        assert (
            service.reveal(
                context, point.id, used_at=deadline - timedelta(microseconds=1)
            )
            == "priya@example.com"
        )
        point.expires_at = deadline
        with pytest.raises(LookupError, match="expired"):
            service.reveal(context, point.id, used_at=deadline)
        assert point.lookup_hmac is None
        assert point.value_ciphertext is None
        assert point.encrypted_data_key is None

    engine.dispose()


def test_expired_contact_cannot_be_revived_and_daily_reconciliation_erases_keys() -> (
    None
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as session:
        tenant = Tenant(slug=f"erase-{uuid4()}")
        session.add(tenant)
        session.flush()
        candidate = _candidate(session, tenant.id, "Priya Sharma")
        service = ContactService(session, _cipher())
        context = _context(tenant.id)
        point = service.store(
            context,
            candidate.id,
            ProviderContact(
                kind="email",
                value="priya@example.com",
                verification_state="verified",
                observed_at=observed_at,
            ),
            processed_at=observed_at,
        ).contact_point
        deadline = point.expires_at

        resolution = service.store(
            context,
            candidate.id,
            ProviderContact(
                kind="email",
                value="priya@example.com",
                verification_state="unverified",
                observed_at=deadline,
            ),
            processed_at=deadline,
        )
        assert resolution.accepted is False
        assert point.value_ciphertext is None
        assert point.lookup_hmac is None

        second = service.store(
            context,
            candidate.id,
            ProviderContact(
                kind="phone",
                value="+1 212 555 0112",
                verification_state="verified",
                observed_at=observed_at,
            ),
            processed_at=observed_at,
        ).contact_point
        assert expire_due_contacts(session, now=second.expires_at) == 1
        assert second.value_ciphertext is None
        assert second.encrypted_data_key is None
        assert second.lookup_hmac is None

    engine.dispose()


def test_verified_email_merges_same_tenant_candidate_without_name_or_provider_conflict() -> (
    None
):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(slug=f"merge-{uuid4()}")
        session.add(tenant)
        session.flush()
        first = _candidate(session, tenant.id, "Priya Sharma")
        second = _candidate(session, tenant.id, "Priya Sharma")
        service = ContactService(session, _cipher())
        context = _context(tenant.id)
        contact = ProviderContact(
            kind="email",
            value="priya@example.com",
            verification_state="verified",
        )

        service.store(context, first.id, contact)
        result = service.store(context, second.id, contact)
        session.flush()

        assert result.candidate_id == first.id
        assert session.get(Candidate, second.id) is None
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 1

    engine.dispose()


def test_verified_email_merge_reencrypts_source_contacts_for_target_aad() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(slug=f"merge-contacts-{uuid4()}")
        session.add(tenant)
        session.flush()
        target = _candidate(session, tenant.id, "Priya Sharma")
        source = _candidate(session, tenant.id, "Priya Sharma")
        service = ContactService(session, _cipher())
        context = _context(tenant.id)
        service.store(
            context,
            target.id,
            ProviderContact(
                kind="email",
                value="priya@example.com",
                verification_state="verified",
            ),
        )
        service.store(
            context,
            source.id,
            ProviderContact(
                kind="phone",
                value="+1 212 555 0112",
                verification_state="verified",
            ),
        )

        service.store(
            context,
            source.id,
            ProviderContact(
                kind="email",
                value="priya@example.com",
                verification_state="verified",
            ),
        )
        phone = session.scalar(
            select(ContactPoint).where(
                ContactPoint.candidate_id == target.id,
                ContactPoint.kind == "phone",
            )
        )

        assert session.get(Candidate, source.id) is None
        assert phone is not None
        assert service.reveal(context, phone.id) == "+12125550112"

    engine.dispose()


def test_candidate_merge_delegates_sourcing_membership_reconciliation() -> None:
    class RecordingCoordinator:
        def __init__(self) -> None:
            self.calls: list[tuple[object, object, object]] = []

        def merge_candidate_memberships(
            self, tenant_id: object, source_id: object, target_id: object
        ) -> None:
            self.calls.append((tenant_id, source_id, target_id))

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    coordinator = RecordingCoordinator()
    with Session(engine) as session:
        tenant = Tenant(slug=f"merge-boundary-{uuid4()}")
        session.add(tenant)
        session.flush()
        target = _candidate(session, tenant.id, "Priya Sharma")
        source = _candidate(session, tenant.id, "Priya Sharma")
        service = ContactService(
            session,
            _cipher(),
            merge_coordinator=coordinator,
        )
        context = _context(tenant.id)
        contact = ProviderContact(
            kind="email",
            value="priya@example.com",
            verification_state="verified",
        )

        service.store(context, target.id, contact)
        service.store(context, source.id, contact)

        assert coordinator.calls == [(tenant.id, source.id, target.id)]
        merge_source = inspect.getsource(ContactService._merge_candidate)
        assert "RunCandidate" not in merge_source
        assert "app.sourcing" not in merge_source

    engine.dispose()


def test_verified_email_conflict_creates_suggestion_instead_of_merging() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(slug=f"conflict-{uuid4()}")
        session.add(tenant)
        session.flush()
        first = _candidate(session, tenant.id, "Priya Sharma")
        second = _candidate(session, tenant.id, "Sam Lee")
        service = ContactService(session, _cipher())
        context = _context(tenant.id)
        contact = ProviderContact(
            kind="email",
            value="shared@example.com",
            verification_state="verified",
        )

        service.store(context, first.id, contact)
        result = service.store(context, second.id, contact)
        session.flush()

        assert result.candidate_id == second.id
        assert session.get(Candidate, second.id) is not None
        assert (
            session.scalar(select(func.count()).select_from(DuplicateSuggestion)) == 1
        )

    engine.dispose()


def test_verified_email_provider_id_conflict_creates_suggestion() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(slug=f"provider-conflict-{uuid4()}")
        session.add(tenant)
        session.flush()
        first = _candidate(session, tenant.id, "Priya Sharma")
        second = _candidate(session, tenant.id, "Priya Sharma")
        session.add_all(
            (
                SourceIdentity(
                    tenant_id=tenant.id,
                    candidate_id=first.id,
                    provider="apollo",
                    provider_person_id="apollo-first",
                    source_timestamp=datetime.now(UTC),
                    confidence=1,
                ),
                SourceIdentity(
                    tenant_id=tenant.id,
                    candidate_id=second.id,
                    provider="apollo",
                    provider_person_id="apollo-second",
                    source_timestamp=datetime.now(UTC),
                    confidence=1,
                ),
            )
        )
        service = ContactService(session, _cipher())
        context = _context(tenant.id)
        contact = ProviderContact(
            kind="email",
            value="shared@example.com",
            verification_state="verified",
        )

        service.store(context, first.id, contact)
        result = service.store(context, second.id, contact)
        session.flush()

        assert result.candidate_id == second.id
        assert session.get(Candidate, second.id) is not None
        assert (
            session.scalar(select(func.count()).select_from(DuplicateSuggestion)) == 1
        )

    engine.dispose()


def test_verified_email_never_matches_across_tenants() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first_tenant = Tenant(slug=f"first-{uuid4()}")
        second_tenant = Tenant(slug=f"second-{uuid4()}")
        session.add_all((first_tenant, second_tenant))
        session.flush()
        first = _candidate(session, first_tenant.id, "Priya Sharma")
        second = _candidate(session, second_tenant.id, "Priya Sharma")
        service = ContactService(session, _cipher())
        contact = ProviderContact(
            kind="email",
            value="priya@example.com",
            verification_state="verified",
        )

        service.store(_context(first_tenant.id), first.id, contact)
        result = service.store(_context(second_tenant.id), second.id, contact)
        session.flush()

        assert result.candidate_id == second.id
        assert session.scalar(select(func.count()).select_from(Candidate)) == 2
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 2

    engine.dispose()


def test_reveal_candidate_contact_checks_client_authorization() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(slug=f"reveal-{uuid4()}")
        user = User(
            oidc_subject=f"oidc|{uuid4()}",
            email="owner@example.test",
            display_name="Owner",
        )
        session.add_all((tenant, user))
        session.flush()
        client = ClientCompany(
            tenant_id=tenant.id, name="Client", normalized_name="client"
        )
        session.add(client)
        session.flush()
        job = Job(
            tenant_id=tenant.id,
            client_id=client.id,
            owner_user_id=user.id,
            title="Product Manager",
            job_description="Job",
        )
        session.add(job)
        session.flush()
        scorecard = ScorecardVersion(
            tenant_id=tenant.id,
            job_id=job.id,
            version=1,
            target_titles=[],
            seniority=[],
            locations=[],
            industry_code="technology",
            suggested_adjacent_industries=[],
            uncertainties=[],
            extraction_status="ready",
            confirmed_by_user_id=user.id,
            confirmed_at=datetime.now(UTC),
        )
        session.add(scorecard)
        session.flush()
        job.current_scorecard_id = scorecard.id
        candidate = _candidate(session, tenant.id, "Priya Sharma")
        run = SourcingRun(
            tenant_id=tenant.id,
            job_id=job.id,
            scorecard_version_id=scorecard.id,
            started_by_user_id=user.id,
            state=RunState.READY,
            current_stage=RunState.READY.value,
        )
        session.add(run)
        session.flush()
        run_candidate = RunCandidate(
            tenant_id=tenant.id,
            run_id=run.id,
            candidate_id=candidate.id,
            scorecard_version_id=scorecard.id,
            match_score=90,
            classification="main",
        )
        session.add(run_candidate)
        session.flush()
        cipher = _cipher()
        allowed = RequestContext(
            tenant_id=tenant.id,
            user_id=user.id,
            role=Role.RECRUITER,
            allowed_client_ids=frozenset({client.id}),
        )
        contact = (
            ContactService(session, cipher)
            .store(
                allowed,
                candidate.id,
                ProviderContact(
                    kind="email",
                    value="priya@example.com",
                    verification_state="verified",
                ),
            )
            .contact_point
        )

        assert (
            reveal_candidate_contact(
                session,
                cipher,
                allowed,
                run_candidate.id,
                contact.id,
                authorization_hmac_key=b"authorization",
            )
            == "priya@example.com"
        )
        denied = allowed.model_copy(update={"allowed_client_ids": frozenset({uuid4()})})
        with pytest.raises(LookupError, match="not found"):
            reveal_candidate_contact(
                session,
                cipher,
                denied,
                run_candidate.id,
                contact.id,
                authorization_hmac_key=b"authorization",
            )

    engine.dispose()
