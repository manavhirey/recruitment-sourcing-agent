from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.candidates.models import (
    Candidate,
    CandidateFieldProvenance,
    DuplicateSuggestion,
    SourceIdentity,
)
from app.candidates.service import CandidateService
from app.core.database import Base
from app.identity.models import Tenant
from app.identity.schemas import RequestContext, Role
from app.providers.base import ProviderPerson


@pytest.fixture
def candidate_session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def context(candidate_session: Session) -> RequestContext:
    tenant_id = uuid4()
    candidate_session.add(Tenant(id=tenant_id, slug=f"agency-{tenant_id}"))
    candidate_session.flush()
    return RequestContext(tenant_id=tenant_id, user_id=uuid4(), role=Role.OWNER)


@pytest.fixture
def candidate_service(candidate_session: Session) -> CandidateService:
    return CandidateService(candidate_session)


@pytest.fixture
def provider_person_factory():
    def factory(**overrides: object) -> ProviderPerson:
        values: dict[str, object] = {
            "provider": "apollo",
            "provider_person_id": "apollo-1",
            "full_name": "Priya Sharma",
            "current_title": "Senior Product Manager",
            "current_company": "PayFlow",
            "location": "New York, New York, United States",
            "linkedin_url": "https://www.linkedin.com/in/priya-sharma",
            "experiences": (),
        }
        values.update(overrides)
        return ProviderPerson(**values)  # type: ignore[arg-type]

    return factory


def test_same_provider_id_reuses_candidate(
    candidate_service: CandidateService,
    context: RequestContext,
    provider_person_factory,
) -> None:
    person = provider_person_factory(provider_person_id="apollo-1")
    first = candidate_service.ingest(context, person)
    second = candidate_service.ingest(context, person)

    assert first.candidate_id == second.candidate_id
    assert second.created is False


def test_same_provider_id_reuses_candidate_when_optional_url_is_malformed(
    candidate_service: CandidateService,
    context: RequestContext,
    provider_person_factory,
) -> None:
    first = candidate_service.ingest(context, provider_person_factory())

    second = candidate_service.ingest(
        context,
        provider_person_factory(
            linkedin_url="https://www.linkedin.com:notaport/in/priya"
        ),
    )

    assert second.candidate_id == first.candidate_id
    assert second.created is False
    assert second.matched_by == "provider_id"


def test_cross_provider_profile_url_reuses_candidate_and_retains_source_identity(
    candidate_session: Session,
    candidate_service: CandidateService,
    context: RequestContext,
    provider_person_factory,
) -> None:
    first = candidate_service.ingest(context, provider_person_factory())
    second = candidate_service.ingest(
        context,
        provider_person_factory(provider="other", provider_person_id="other-1"),
    )

    identities = candidate_session.scalars(
        select(SourceIdentity).where(SourceIdentity.candidate_id == first.candidate_id)
    ).all()
    assert second.candidate_id == first.candidate_id
    assert second.created is False
    assert {identity.provider for identity in identities} == {"apollo", "other"}


def test_fuzzy_match_creates_suggestion_without_merge(
    candidate_session: Session,
    candidate_service: CandidateService,
    context: RequestContext,
    provider_person_factory,
) -> None:
    first = provider_person_factory(provider_person_id="a", full_name="Priya Sharma")
    second = provider_person_factory(
        provider_person_id="b",
        full_name="Priya S Sharma",
        linkedin_url="https://www.linkedin.com/in/priya-s-sharma",
    )
    first_result = candidate_service.ingest(context, first)
    result = candidate_service.ingest(context, second)

    assert result.created is True
    assert result.candidate_id != first_result.candidate_id
    assert result.duplicate_suggestion_id is not None
    suggestion = candidate_session.get(
        DuplicateSuggestion, result.duplicate_suggestion_id
    )
    assert suggestion is not None
    assert suggestion.status == "pending"


def test_identity_and_suggestions_never_cross_tenants(
    candidate_session: Session,
    candidate_service: CandidateService,
    context: RequestContext,
    provider_person_factory,
) -> None:
    first = candidate_service.ingest(context, provider_person_factory())
    other_tenant_id = uuid4()
    candidate_session.add(Tenant(id=other_tenant_id, slug=f"agency-{other_tenant_id}"))
    candidate_session.flush()
    other = RequestContext(tenant_id=other_tenant_id, user_id=uuid4(), role=Role.OWNER)

    result = candidate_service.ingest(other, provider_person_factory())

    assert result.created is True
    assert result.candidate_id != first.candidate_id
    assert result.duplicate_suggestion_id is None


def test_empty_and_lower_confidence_observations_do_not_replace_display_value(
    candidate_session: Session,
    candidate_service: CandidateService,
    context: RequestContext,
    provider_person_factory,
) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    result = candidate_service.ingest(
        context,
        provider_person_factory(),
        source_timestamp=observed_at,
        confidence=0.9,
    )
    candidate_service.ingest(
        context,
        provider_person_factory(full_name="", current_title=None),
        source_timestamp=observed_at + timedelta(days=1),
        confidence=1.0,
    )
    candidate_service.ingest(
        context,
        provider_person_factory(full_name="Priya S. Sharma"),
        source_timestamp=observed_at + timedelta(days=2),
        confidence=0.8,
    )

    candidate = candidate_session.get(Candidate, result.candidate_id)
    assert candidate is not None
    assert candidate.full_name == "Priya Sharma"
    assert candidate.current_title == "Senior Product Manager"


def test_newer_equal_confidence_value_updates_display_and_provenance(
    candidate_session: Session,
    candidate_service: CandidateService,
    context: RequestContext,
    provider_person_factory,
) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    result = candidate_service.ingest(
        context,
        provider_person_factory(),
        source_timestamp=observed_at,
        confidence=0.9,
    )
    candidate_service.ingest(
        context,
        provider_person_factory(current_title="Principal Product Manager"),
        source_timestamp=observed_at + timedelta(days=1),
        confidence=0.9,
    )

    candidate = candidate_session.get(Candidate, result.candidate_id)
    current = candidate_session.scalar(
        select(CandidateFieldProvenance).where(
            CandidateFieldProvenance.candidate_id == result.candidate_id,
            CandidateFieldProvenance.field_name == "current_title",
            CandidateFieldProvenance.is_current.is_(True),
        )
    )
    assert candidate is not None
    assert candidate.current_title == "Principal Product Manager"
    assert current is not None
    assert current.provider == "apollo"
    assert current.source_timestamp.replace(tzinfo=UTC) == observed_at + timedelta(
        days=1
    )
    assert len(current.observed_value_hash) == 64


def test_repeat_ingestion_does_not_duplicate_provenance(
    candidate_session: Session,
    candidate_service: CandidateService,
    context: RequestContext,
    provider_person_factory,
) -> None:
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    person = provider_person_factory()
    candidate_service.ingest(context, person, source_timestamp=observed_at)
    candidate_service.ingest(context, person, source_timestamp=observed_at)

    assert (
        candidate_session.scalar(
            select(func.count()).select_from(CandidateFieldProvenance)
        )
        == 5
    )
