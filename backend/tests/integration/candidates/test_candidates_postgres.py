import os
import queue
import threading
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.candidates.models import Candidate, SourceIdentity
from app.candidates.service import CandidateService
from app.identity.dependencies import apply_tenant_context
from app.identity.models import Tenant
from app.identity.schemas import RequestContext, Role
from app.providers.base import ProviderPerson

OWNER_DATABASE_URL = os.getenv("TASK6_OWNER_DATABASE_URL")
API_DATABASE_URL = os.getenv("TASK6_API_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not OWNER_DATABASE_URL or not API_DATABASE_URL,
    reason="Task 6 PostgreSQL URLs are not configured",
)

_CANDIDATE_TABLES = (
    "candidates",
    "candidate_source_identities",
    "candidate_field_provenance",
    "candidate_experiences",
    "candidate_duplicate_suggestions",
)


@dataclass(frozen=True)
class PostgresCandidateScenario:
    owner_engine: Engine
    api_database_url: str
    first_context: RequestContext
    second_context: RequestContext


@pytest.fixture
def postgres_candidate_scenario() -> Generator[PostgresCandidateScenario, None, None]:
    assert OWNER_DATABASE_URL is not None
    assert API_DATABASE_URL is not None
    owner_engine = create_engine(OWNER_DATABASE_URL)
    first_id = uuid4()
    second_id = uuid4()
    with Session(owner_engine) as session:
        session.add_all(
            (
                Tenant(id=first_id, slug=f"candidate-first-{first_id}"),
                Tenant(id=second_id, slug=f"candidate-second-{second_id}"),
            )
        )
        session.commit()
    scenario = PostgresCandidateScenario(
        owner_engine=owner_engine,
        api_database_url=API_DATABASE_URL,
        first_context=RequestContext(
            tenant_id=first_id, user_id=uuid4(), role=Role.OWNER
        ),
        second_context=RequestContext(
            tenant_id=second_id, user_id=uuid4(), role=Role.OWNER
        ),
    )
    yield scenario
    with Session(owner_engine) as session:
        session.execute(
            text("DELETE FROM tenants WHERE id IN (:first_id, :second_id)"),
            {"first_id": first_id, "second_id": second_id},
        )
        session.commit()
    owner_engine.dispose()


def _person(
    *, provider: str = "apollo", provider_person_id: str = "p1"
) -> ProviderPerson:
    return ProviderPerson(
        provider=provider,
        provider_person_id=provider_person_id,
        full_name="Priya Sharma",
        current_title="Senior Product Manager",
        current_company="PayFlow",
        location="New York, United States",
        linkedin_url="https://www.linkedin.com/in/priya-sharma?trk=search",
        experiences=(),
    )


def test_candidate_tables_force_rls_with_using_and_check_policies(
    postgres_candidate_scenario: PostgresCandidateScenario,
) -> None:
    with postgres_candidate_scenario.owner_engine.connect() as connection:
        table_flags = connection.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname = ANY(:tables) ORDER BY relname"
            ),
            {"tables": list(_CANDIDATE_TABLES)},
        ).all()
        policies = connection.execute(
            text(
                "SELECT tablename, qual, with_check FROM pg_policies "
                "WHERE tablename = ANY(:tables) ORDER BY tablename"
            ),
            {"tables": list(_CANDIDATE_TABLES)},
        ).all()

    assert table_flags == sorted([(table, True, True) for table in _CANDIDATE_TABLES])
    assert len(policies) == len(_CANDIDATE_TABLES)
    assert all(policy.qual and policy.with_check for policy in policies)


def test_rls_hides_candidates_and_rejects_wrong_tenant_insert(
    postgres_candidate_scenario: PostgresCandidateScenario,
) -> None:
    scenario = postgres_candidate_scenario
    with Session(create_engine(scenario.api_database_url)) as session:
        apply_tenant_context(session, scenario.first_context.tenant_id)
        result = CandidateService(session).ingest(scenario.first_context, _person())
        session.commit()
    with Session(create_engine(scenario.api_database_url)) as session:
        apply_tenant_context(session, scenario.second_context.tenant_id)
        assert session.get(Candidate, result.candidate_id) is None
        session.add(
            Candidate(
                tenant_id=scenario.first_context.tenant_id,
                full_name="Cross Tenant",
                normalized_name="cross tenant",
            )
        )
        with pytest.raises(ProgrammingError):
            session.flush()


def test_composite_foreign_key_rejects_cross_tenant_source_link(
    postgres_candidate_scenario: PostgresCandidateScenario,
) -> None:
    scenario = postgres_candidate_scenario
    engine = create_engine(scenario.api_database_url)
    with Session(engine) as session:
        apply_tenant_context(session, scenario.first_context.tenant_id)
        first = CandidateService(session).ingest(scenario.first_context, _person())
        session.commit()
    with Session(engine) as session:
        apply_tenant_context(session, scenario.second_context.tenant_id)
        session.add(
            SourceIdentity(
                tenant_id=scenario.second_context.tenant_id,
                candidate_id=first.candidate_id,
                provider="other",
                provider_person_id="other-1",
                source_timestamp=datetime.now(UTC),
                confidence=1.0,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
    engine.dispose()


def test_partial_provider_url_uniqueness_is_enforced(
    postgres_candidate_scenario: PostgresCandidateScenario,
) -> None:
    scenario = postgres_candidate_scenario
    engine = create_engine(scenario.api_database_url)
    with Session(engine) as session:
        apply_tenant_context(session, scenario.first_context.tenant_id)
        first = CandidateService(session).ingest(scenario.first_context, _person())
        session.commit()
    with Session(engine) as session:
        apply_tenant_context(session, scenario.first_context.tenant_id)
        session.add(
            SourceIdentity(
                tenant_id=scenario.first_context.tenant_id,
                candidate_id=first.candidate_id,
                provider="apollo",
                provider_person_id="p2",
                profile_url="https://www.linkedin.com/in/priya-sharma",
                normalized_profile_url="https://www.linkedin.com/in/priya-sharma",
                source_timestamp=datetime.now(UTC),
                confidence=1.0,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
    engine.dispose()


def test_concurrent_cross_provider_url_ingestion_reuses_one_candidate(
    postgres_candidate_scenario: PostgresCandidateScenario,
) -> None:
    scenario = postgres_candidate_scenario
    barrier = threading.Barrier(2)
    outcomes: queue.Queue[object] = queue.Queue()

    def ingest(person: ProviderPerson) -> None:
        engine = create_engine(scenario.api_database_url)
        with Session(engine) as session:
            apply_tenant_context(session, scenario.first_context.tenant_id)
            barrier.wait(timeout=5)
            try:
                result = CandidateService(session).ingest(
                    scenario.first_context, person
                )
                session.commit()
            except (SQLAlchemyError, RuntimeError, ValueError) as error:
                session.rollback()
                outcomes.put(error)
            else:
                outcomes.put(result)
        engine.dispose()

    threads = (
        threading.Thread(target=ingest, args=(_person(),)),
        threading.Thread(
            target=ingest,
            args=(_person(provider="other", provider_person_id="other-1"),),
        ),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    results = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert not any(isinstance(result, Exception) for result in results), results
    assert len({result.candidate_id for result in results}) == 1  # type: ignore[union-attr]

    with Session(create_engine(scenario.api_database_url)) as session:
        apply_tenant_context(session, scenario.first_context.tenant_id)
        identities = session.scalars(select(SourceIdentity)).all()
        candidates = session.scalars(select(Candidate)).all()
    assert len(candidates) == 1
    assert {identity.provider for identity in identities} == {"apollo", "other"}
