import os
import queue
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.clients.models import ClientCompany, ClientIndustry
from app.identity.dependencies import apply_tenant_context
from app.identity.models import Tenant, User
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardVersion
from app.jobs.schemas import (
    ClientContext,
    CriterionKind,
    ScorecardCriterion,
    ScorecardDraft,
)
from app.jobs.service import JobError, JobService

OWNER_DATABASE_URL = os.getenv("TASK4_OWNER_DATABASE_URL")
API_DATABASE_URL = os.getenv("TASK4_API_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not OWNER_DATABASE_URL or not API_DATABASE_URL,
    reason="Task 4 PostgreSQL concurrency URLs are not configured",
)


@dataclass(frozen=True)
class PostgresJobScenario:
    owner_engine: Engine
    api_database_url: str
    context: RequestContext
    job_id: UUID
    initial_draft: ScorecardDraft


def _draft(title: str, key: str) -> ScorecardDraft:
    return ScorecardDraft(
        target_titles=[title],
        criteria=[
            ScorecardCriterion(
                key=key,
                label=f"{key.title()} experience",
                kind=CriterionKind.MUST_HAVE,
                source_text=f"{key} experience",
            )
        ],
        seniority=["senior"],
        minimum_years=5,
        maximum_years=12,
        locations=["India"],
        industry_code="technology.fintech",
        suggested_adjacent_industries=[],
        uncertainties=[],
    )


@pytest.fixture
def postgres_job_scenario() -> Generator[PostgresJobScenario, None, None]:
    assert OWNER_DATABASE_URL is not None
    assert API_DATABASE_URL is not None
    owner_engine = create_engine(OWNER_DATABASE_URL)
    tenant_id = uuid4()
    user_id = uuid4()
    client_id = uuid4()
    job_id = uuid4()
    initial_draft = _draft("Product Manager", "payments")
    with Session(owner_engine) as session:
        apply_tenant_context(session, tenant_id)
        session.add_all(
            (
                Tenant(id=tenant_id, slug=f"concurrency-{tenant_id}"),
                User(
                    id=user_id,
                    oidc_subject=f"oidc|{user_id}",
                    email=f"{user_id}@concurrency.test",
                    display_name="Concurrency Owner",
                ),
            )
        )
        session.flush()
        session.add(
            ClientCompany(
                id=client_id,
                tenant_id=tenant_id,
                name="Concurrency Client",
                normalized_name=f"concurrency client {client_id}",
            )
        )
        session.flush()
        session.add(
            ClientIndustry(
                tenant_id=tenant_id,
                client_id=client_id,
                industry_code="technology.fintech",
                taxonomy_version="v1",
            )
        )
        session.add(
            Job(
                id=job_id,
                tenant_id=tenant_id,
                client_id=client_id,
                owner_user_id=user_id,
                title="Product Manager",
                job_description="Hire a product manager with payments experience.",
                status="awaiting_scorecard",
                draft_payload=initial_draft.model_dump(mode="json"),
                draft_revision=1,
                draft_extraction_status="ready",
            )
        )
        session.commit()

    yield PostgresJobScenario(
        owner_engine=owner_engine,
        api_database_url=API_DATABASE_URL,
        context=RequestContext(
            tenant_id=tenant_id,
            user_id=user_id,
            role=Role.OWNER,
        ),
        job_id=job_id,
        initial_draft=initial_draft,
    )
    owner_engine.dispose()


def _wait_until_contender_blocks(owner_engine: Engine, application_name: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with owner_engine.connect() as connection:
            blocked = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE application_name = :application_name "
                    "AND wait_event_type = 'Lock'"
                ),
                {"application_name": application_name},
            )
        if blocked:
            return
        time.sleep(0.02)
    raise AssertionError("concurrent contender did not block on the job row")


def _assert_revision_conflict(outcome: object) -> None:
    assert isinstance(outcome, JobError), repr(outcome)
    assert outcome.code == "scorecard_revision_conflict"


class BlockingScorecardGateway:
    def __init__(self, draft: ScorecardDraft) -> None:
        self.draft = draft
        self.entered = threading.Event()
        self.release = threading.Event()

    def extract(
        self, job_description: str, client_context: ClientContext
    ) -> ScorecardDraft:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("test did not release the blocked scorecard gateway")
        return self.draft


def test_generate_does_not_lock_job_during_extraction_and_rechecks_revision(
    postgres_job_scenario: PostgresJobScenario,
) -> None:
    scenario = postgres_job_scenario
    generated_draft = _draft("AI Product Manager", "artificial_intelligence")
    recruiter_draft = _draft("Senior Product Manager", "platform")
    gateway = BlockingScorecardGateway(generated_draft)
    generation_engine = create_engine(scenario.api_database_url)
    mutation_engine = create_engine(scenario.api_database_url)
    generation_outcomes: queue.Queue[object] = queue.Queue()
    mutation_outcomes: queue.Queue[object] = queue.Queue()
    mutation_finished = threading.Event()

    def generate() -> None:
        with Session(generation_engine) as session:
            apply_tenant_context(session, scenario.context.tenant_id)
            try:
                JobService(
                    session,
                    b"test-suppression-key",
                    scorecard_gateway=gateway,
                ).generate_draft(
                    scenario.context,
                    scenario.job_id,
                    expected_revision=1,
                    idempotency_key="blocked-generation",
                )
                session.commit()
            except (JobError, SQLAlchemyError) as error:
                session.rollback()
                generation_outcomes.put(error)
            else:
                generation_outcomes.put("succeeded")

    def mutate() -> None:
        try:
            with Session(mutation_engine) as session:
                apply_tenant_context(session, scenario.context.tenant_id)
                try:
                    JobService(session, b"test-suppression-key").update_draft(
                        scenario.context,
                        scenario.job_id,
                        recruiter_draft,
                        expected_revision=1,
                        idempotency_key="edit-during-generation",
                    )
                    session.commit()
                except (JobError, SQLAlchemyError) as error:
                    session.rollback()
                    mutation_outcomes.put(error)
                else:
                    mutation_outcomes.put("succeeded")
        finally:
            mutation_finished.set()

    generation_thread = threading.Thread(target=generate)
    mutation_thread = threading.Thread(target=mutate)
    generation_thread.start()
    assert gateway.entered.wait(timeout=5)
    mutation_thread.start()
    mutation_completed_while_gateway_blocked = mutation_finished.wait(timeout=10)
    gateway.release.set()
    generation_thread.join(timeout=15)
    mutation_thread.join(timeout=15)

    assert mutation_completed_while_gateway_blocked
    assert not generation_thread.is_alive()
    assert not mutation_thread.is_alive()
    assert mutation_outcomes.get_nowait() == "succeeded"
    _assert_revision_conflict(generation_outcomes.get_nowait())
    with Session(scenario.owner_engine) as session:
        apply_tenant_context(session, scenario.context.tenant_id)
        job = session.get(Job, scenario.job_id)
        assert job is not None
        assert job.draft_revision == 2
        expected_payload = recruiter_draft.model_dump(mode="json")
        expected_payload["criteria"][0]["source_text"] = None
        expected_payload["criteria"][0]["recruiter_entered"] = True
        assert job.draft_payload == expected_payload
    generation_engine.dispose()
    mutation_engine.dispose()


def test_concurrent_draft_update_returns_revision_conflict(
    postgres_job_scenario: PostgresJobScenario,
) -> None:
    scenario = postgres_job_scenario
    contender_name = f"task4-update-{uuid4()}"
    contender_engine = create_engine(
        scenario.api_database_url,
        connect_args={"application_name": contender_name},
    )
    outcomes: queue.Queue[object] = queue.Queue()
    winner_draft = _draft("Principal Product Manager", "strategy")
    contender_draft = _draft("Senior Product Manager", "platform")

    def contend() -> None:
        with Session(contender_engine) as session:
            apply_tenant_context(session, scenario.context.tenant_id)
            try:
                JobService(session, b"test-suppression-key").update_draft(
                    scenario.context,
                    scenario.job_id,
                    contender_draft,
                    expected_revision=1,
                    idempotency_key="concurrent-contender-update",
                )
                session.commit()
            except (JobError, SQLAlchemyError) as error:
                session.rollback()
                outcomes.put(error)
            else:
                outcomes.put("succeeded")

    winner_engine = create_engine(scenario.api_database_url)
    with Session(winner_engine) as winner:
        apply_tenant_context(winner, scenario.context.tenant_id)
        job = winner.scalar(
            select(Job).where(Job.id == scenario.job_id).with_for_update()
        )
        assert job is not None
        job.draft_payload = winner_draft.model_dump(mode="json")
        job.draft_revision = 2
        winner.flush()
        thread = threading.Thread(target=contend)
        thread.start()
        _wait_until_contender_blocks(scenario.owner_engine, contender_name)
        winner.commit()
        thread.join(timeout=5)

    assert not thread.is_alive()
    _assert_revision_conflict(outcomes.get_nowait())
    with Session(scenario.owner_engine) as session:
        apply_tenant_context(session, scenario.context.tenant_id)
        job = session.get(Job, scenario.job_id)
        assert job is not None
        assert job.draft_revision == 2
        assert job.draft_payload == winner_draft.model_dump(mode="json")
    winner_engine.dispose()
    contender_engine.dispose()


def test_concurrent_confirmation_returns_revision_conflict_not_uniqueness_error(
    postgres_job_scenario: PostgresJobScenario,
) -> None:
    scenario = postgres_job_scenario
    contender_name = f"task4-confirm-{uuid4()}"
    contender_engine = create_engine(
        scenario.api_database_url,
        connect_args={"application_name": contender_name},
    )
    outcomes: queue.Queue[object] = queue.Queue()

    def contend() -> None:
        with Session(contender_engine) as session:
            apply_tenant_context(session, scenario.context.tenant_id)
            try:
                JobService(session, b"test-suppression-key").confirm_scorecard(
                    scenario.context,
                    scenario.job_id,
                    expected_revision=1,
                    idempotency_key="concurrent-contender-confirm",
                )
                session.commit()
            except (JobError, SQLAlchemyError) as error:
                session.rollback()
                outcomes.put(error)
            else:
                outcomes.put("succeeded")

    winner_engine = create_engine(scenario.api_database_url)
    with Session(winner_engine) as winner:
        apply_tenant_context(winner, scenario.context.tenant_id)
        JobService(winner, b"test-suppression-key").confirm_scorecard(
            scenario.context,
            scenario.job_id,
            expected_revision=1,
            idempotency_key="concurrent-winner-confirm",
        )
        thread = threading.Thread(target=contend)
        thread.start()
        _wait_until_contender_blocks(scenario.owner_engine, contender_name)
        winner.commit()
        thread.join(timeout=5)

    assert not thread.is_alive()
    _assert_revision_conflict(outcomes.get_nowait())
    with Session(scenario.owner_engine) as session:
        apply_tenant_context(session, scenario.context.tenant_id)
        job = session.get(Job, scenario.job_id)
        assert job is not None
        assert job.draft_revision == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(ScorecardVersion)
                .where(ScorecardVersion.job_id == scenario.job_id)
            )
            == 1
        )
    winner_engine.dispose()
    contender_engine.dispose()
