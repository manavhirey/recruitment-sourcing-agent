import threading
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit.models import AuditEvent
from app.candidates.models import Candidate
from app.clients.models import ClientCompany
from app.core.database import Base
from app.identity.models import Tenant, User
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardCriterionRecord, ScorecardVersion
from app.providers.base import ProviderPerson, ProviderQuery, SearchPage
from app.sourcing.models import (
    RunCandidate,
    RunCheckpoint,
    SourcingRun,
    TenantNotification,
    UsageBudget,
    UsageLedger,
)
from app.sourcing.state_machine import RunState
from app.sourcing.tasks import execute_match_run, execute_plan_run, execute_source_run


class HundredPersonGateway:
    def __init__(self) -> None:
        self.search_calls = 0

    def search(self, query: ProviderQuery, page: int) -> SearchPage:
        self.search_calls += 1
        return SearchPage(
            people=tuple(
                ProviderPerson(
                    provider="apollo",
                    provider_person_id=f"person-{index}",
                    full_name=f"Person {index}",
                    current_title="Product Manager",
                    current_company="Example",
                    location="New York, NY",
                    linkedin_url=f"https://linkedin.com/in/person-{index}",
                    experiences=(),
                )
                for index in range(100)
            ),
            page=page,
            next_page=None,
            total_available=100,
        )

    def close(self) -> None:
        return None


class FailingGateway:
    def __init__(
        self, *, first_page_succeeds: bool, successful_people: int = 1
    ) -> None:
        self.first_page_succeeds = first_page_succeeds
        self.successful_people = successful_people
        self.search_calls = 0

    def search(self, query: ProviderQuery, page: int) -> SearchPage:
        self.search_calls += 1
        if self.first_page_succeeds and page == 1:
            people = (
                (
                    ProviderPerson(
                        provider="apollo",
                        provider_person_id="partial-person",
                        full_name="Partial Person",
                        current_title="Product Manager",
                        current_company="Example",
                        location="New York, NY",
                        linkedin_url="https://linkedin.com/in/partial-person",
                        experiences=(),
                    ),
                )
                if self.successful_people
                else ()
            )
            return SearchPage(
                people=people,
                page=1,
                next_page=2,
                total_available=2,
            )
        from app.providers.base import ProviderTemporaryError

        raise ProviderTemporaryError("secret provider detail")

    def close(self) -> None:
        return None


@pytest.fixture
def sourcing_scenario() -> Generator[dict[str, Any], None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    tenant_id = uuid4()
    user_id = uuid4()
    job_id = uuid4()
    scorecard_id = uuid4()
    run_id = uuid4()
    with factory() as session:
        tenant = Tenant(id=tenant_id, slug=f"source-{tenant_id}")
        user = User(
            id=user_id,
            oidc_subject=f"oidc|{user_id}",
            email="owner@example.test",
            display_name="Owner",
        )
        client = ClientCompany(
            tenant_id=tenant_id,
            name="Client",
            normalized_name="client",
        )
        session.add_all((tenant, user, client))
        session.flush()
        job = Job(
            id=job_id,
            tenant_id=tenant_id,
            client_id=client.id,
            owner_user_id=user_id,
            title="Product Manager",
            job_description="Find a product manager.",
        )
        session.add(job)
        session.flush()
        scorecard = ScorecardVersion(
            id=scorecard_id,
            tenant_id=tenant_id,
            job_id=job_id,
            version=1,
            target_titles=["Product Manager"],
            seniority=["manager"],
            minimum_years=None,
            maximum_years=None,
            locations=["New York, NY"],
            industry_code="technology.fintech",
            suggested_adjacent_industries=[],
            uncertainties=[],
            extraction_status="ready",
            confirmed_by_user_id=user_id,
            confirmed_at=datetime(2026, 8, 16, tzinfo=UTC),
        )
        session.add(scorecard)
        session.flush()
        session.add(
            ScorecardCriterionRecord(
                tenant_id=tenant_id,
                scorecard_version_id=scorecard_id,
                position=0,
                key="payments",
                label="Payments experience",
                kind="must_have",
                evidence_required=False,
                source_text="payments experience",
                inferred=False,
                recruiter_entered=False,
                lawful_requirement_confirmed=False,
            )
        )
        job.current_scorecard_id = scorecard_id
        run = SourcingRun(
            id=run_id,
            tenant_id=tenant_id,
            job_id=job_id,
            scorecard_version_id=scorecard_id,
            started_by_user_id=user_id,
            state=RunState.SOURCING,
            planned_queries=[
                {
                    "titles": ["Product Manager"],
                    "seniorities": ["manager"],
                    "person_locations": ["New York, NY"],
                    "industry_codes": ["technology.fintech"],
                    "keywords": ["Fintech"],
                }
            ],
        )
        session.add(run)
        session.commit()

    yield {
        "factory": factory,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "run_id": run_id,
    }
    engine.dispose()


def test_replayed_source_task_does_not_duplicate_candidates(
    sourcing_scenario: dict[str, Any],
) -> None:
    gateway = HundredPersonGateway()
    scenario = sourcing_scenario
    context = RequestContext(
        tenant_id=scenario["tenant_id"],
        user_id=scenario["user_id"],
        role=Role.OWNER,
    )

    execute_source_run(
        scenario["factory"],
        scenario["run_id"],
        context,
        gateway_factory=lambda: gateway,
        idempotency_key="source:q1:p1",
    )
    execute_source_run(
        scenario["factory"],
        scenario["run_id"],
        context,
        gateway_factory=lambda: gateway,
        idempotency_key="source:q1:p1",
    )

    with scenario["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Candidate)) == 100
        assert session.scalar(select(func.count()).select_from(RunCandidate)) == 100
        assert (
            session.scalar(
                select(func.count())
                .select_from(RunCheckpoint)
                .where(
                    RunCheckpoint.run_id == scenario["run_id"],
                    RunCheckpoint.idempotency_key == "source:q1:p1",
                    RunCheckpoint.status == "completed",
                )
            )
            == 1
        )
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None
        assert run.state is RunState.MATCHING
    assert gateway.search_calls == 1


def test_source_reconciles_returned_provider_usage_receipt(
    sourcing_scenario: dict[str, Any],
) -> None:
    scenario = sourcing_scenario
    context = RequestContext(
        tenant_id=scenario["tenant_id"],
        user_id=scenario["user_id"],
        role=Role.OWNER,
    )

    class ReceiptGateway(HundredPersonGateway):
        def search(self, query: ProviderQuery, page: int) -> SearchPage:
            result = super().search(query, page)
            return SearchPage(
                people=result.people,
                page=result.page,
                next_page=result.next_page,
                total_available=result.total_available,
                provider_request_id="apollo-request-123",
                charged_units=(("estimated_credits", 3), ("search_pages", 1)),
            )

    execute_source_run(
        scenario["factory"],
        scenario["run_id"],
        context,
        gateway_factory=ReceiptGateway,
        idempotency_key="source:receipt",
    )

    with scenario["factory"]() as session:
        usage = list(
            session.scalars(
                select(UsageLedger)
                .where(UsageLedger.run_id == scenario["run_id"])
                .order_by(UsageLedger.unit_type)
            )
        )
        assert [(row.unit_type, row.charged_units) for row in usage] == [
            ("estimated_credits", 3),
            ("search_pages", 1),
        ]
        assert {row.provider_request_id for row in usage} == {"apollo-request-123"}


def test_plan_and_match_tasks_replay_through_production_checkpoints(
    sourcing_scenario: dict[str, Any],
) -> None:
    scenario = sourcing_scenario
    context = RequestContext(
        tenant_id=scenario["tenant_id"],
        user_id=scenario["user_id"],
        role=Role.OWNER,
    )
    with scenario["factory"]() as session:
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None
        run.state = RunState.QUEUED
        run.current_stage = RunState.QUEUED.value
        run.planned_queries = []
        session.commit()

    execute_plan_run(
        scenario["factory"],
        scenario["run_id"],
        context,
        idempotency_key="plan:run",
    )
    execute_plan_run(
        scenario["factory"],
        scenario["run_id"],
        context,
        idempotency_key="plan:run",
    )
    execute_source_run(
        scenario["factory"],
        scenario["run_id"],
        context,
        gateway_factory=HundredPersonGateway,
        idempotency_key="source:run",
    )
    execute_match_run(
        scenario["factory"],
        scenario["run_id"],
        context,
        idempotency_key="match:run",
    )
    execute_match_run(
        scenario["factory"],
        scenario["run_id"],
        context,
        idempotency_key="match:run",
    )

    with scenario["factory"]() as session:
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None
        assert run.planned_queries
        assert run.state is RunState.ENRICHING
        assert run.matched_count == 100
        results = list(
            session.scalars(
                select(RunCandidate).where(RunCandidate.run_id == scenario["run_id"])
            )
        )
        assert all(result.match_score is not None for result in results)
        assert all(
            result.classification in {"main", "near_match"} for result in results
        )
        assert all(result.evidence is not None for result in results)
        assert all(result.scoring_version == "matching-v1" for result in results)
        assert (
            session.scalar(
                select(func.count())
                .select_from(RunCheckpoint)
                .where(
                    RunCheckpoint.run_id == scenario["run_id"],
                    RunCheckpoint.idempotency_key.in_(("plan:run", "match:run")),
                    RunCheckpoint.status == "completed",
                )
            )
            == 2
        )


@pytest.mark.parametrize(
    ("first_page_succeeds", "expected_state", "expected_count"),
    [
        (False, RunState.FAILED, 0),
        (True, RunState.PARTIALLY_READY, 1),
    ],
)
def test_provider_failure_distinguishes_no_results_from_partial_results(
    sourcing_scenario: dict[str, Any],
    first_page_succeeds: bool,
    expected_state: RunState,
    expected_count: int,
) -> None:
    scenario = sourcing_scenario
    context = RequestContext(
        tenant_id=scenario["tenant_id"],
        user_id=scenario["user_id"],
        role=Role.OWNER,
    )

    execute_source_run(
        scenario["factory"],
        scenario["run_id"],
        context,
        gateway_factory=lambda: FailingGateway(first_page_succeeds=first_page_succeeds),
        idempotency_key="source:provider-failure",
    )

    with scenario["factory"]() as session:
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None
        assert run.state is expected_state
        assert run.candidate_count == expected_count
        assert run.error_code == "provider_search_failed"
        assert run.error_message == (
            "The sourcing provider could not complete the search."
        )


def test_successful_empty_page_then_provider_failure_is_partially_ready(
    sourcing_scenario: dict[str, Any],
) -> None:
    scenario = sourcing_scenario
    context = RequestContext(
        tenant_id=scenario["tenant_id"],
        user_id=scenario["user_id"],
        role=Role.OWNER,
    )

    execute_source_run(
        scenario["factory"],
        scenario["run_id"],
        context,
        gateway_factory=lambda: FailingGateway(
            first_page_succeeds=True,
            successful_people=0,
        ),
        idempotency_key="source:empty-page-then-failure",
    )

    with scenario["factory"]() as session:
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None
        assert run.state is RunState.PARTIALLY_READY
        assert run.candidate_count == 0
        assert run.error_code == "provider_search_failed"
        assert "secret" not in run.error_message
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.run_id == run.id,
                    AuditEvent.action == "sourcing_run.source_completed",
                )
            )
            == 1
        )


def test_source_reserves_budget_before_the_provider_call(
    sourcing_scenario: dict[str, Any],
) -> None:
    scenario = sourcing_scenario
    context = RequestContext(
        tenant_id=scenario["tenant_id"],
        user_id=scenario["user_id"],
        role=Role.OWNER,
    )
    with scenario["factory"]() as session:
        session.add(
            UsageBudget(
                tenant_id=scenario["tenant_id"],
                max_search_pages=0,
                max_enrichments=0,
                max_estimated_credits=0,
            )
        )
        session.commit()
    gateway = HundredPersonGateway()

    execute_source_run(
        scenario["factory"],
        scenario["run_id"],
        context,
        gateway_factory=lambda: gateway,
        idempotency_key="source:budgeted",
    )

    with scenario["factory"]() as session:
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None
        assert run.state is RunState.PARTIALLY_READY
        assert run.error_code == "usage_budget_exhausted"
        assert (
            session.scalar(
                select(func.count())
                .select_from(TenantNotification)
                .where(TenantNotification.run_id == run.id)
            )
            == 2
        )
    assert gateway.search_calls == 0


def test_concurrent_source_replay_uses_one_run_scoped_gateway(
    sourcing_scenario: dict[str, Any],
) -> None:
    scenario = sourcing_scenario
    context = RequestContext(
        tenant_id=scenario["tenant_id"],
        user_id=scenario["user_id"],
        role=Role.OWNER,
    )
    entered = threading.Event()
    release = threading.Event()
    factory_calls = 0
    outcomes: list[object] = []

    class SlowGateway(HundredPersonGateway):
        def search(self, query: ProviderQuery, page: int) -> SearchPage:
            entered.set()
            assert release.wait(timeout=5)
            return super().search(query, page)

    def gateway_factory() -> SlowGateway:
        nonlocal factory_calls
        factory_calls += 1
        return SlowGateway()

    def source() -> None:
        try:
            execute_source_run(
                scenario["factory"],
                scenario["run_id"],
                context,
                gateway_factory=gateway_factory,
                idempotency_key="source:concurrent",
            )
        except Exception as error:  # noqa: BLE001 - thread outcome is asserted below
            outcomes.append(error)
        else:
            outcomes.append("ok")

    first = threading.Thread(target=source)
    second = threading.Thread(target=source)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    second.join(timeout=2)
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive() and not second.is_alive()
    assert outcomes == ["ok", "ok"]
    assert factory_calls == 1

    with scenario["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(RunCandidate)) == 100
        assert (
            session.scalar(
                select(func.count())
                .select_from(RunCheckpoint)
                .where(
                    RunCheckpoint.run_id == scenario["run_id"],
                    RunCheckpoint.idempotency_key == "source:concurrent",
                )
            )
            == 1
        )


def test_one_gateway_scope_spans_all_queries_and_stops_at_300_unique_people(
    sourcing_scenario: dict[str, Any],
) -> None:
    scenario = sourcing_scenario
    context = RequestContext(
        tenant_id=scenario["tenant_id"],
        user_id=scenario["user_id"],
        role=Role.OWNER,
    )
    with scenario["factory"]() as session:
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None
        run.planned_queries = [
            {
                "titles": [f"Product Manager {query_number}"],
                "seniorities": [],
                "person_locations": [],
                "industry_codes": ["technology.fintech"],
                "keywords": [],
            }
            for query_number in range(4)
        ]
        session.commit()
    factory_calls = 0

    class QueryScopedGateway(HundredPersonGateway):
        def search(self, query: ProviderQuery, page: int) -> SearchPage:
            self.search_calls += 1
            prefix = query.titles[0].rsplit(" ", 1)[-1]
            return SearchPage(
                people=tuple(
                    ProviderPerson(
                        provider="apollo",
                        provider_person_id=f"q{prefix}-person-{index}",
                        full_name=f"Query {prefix} Person {index}",
                        current_title="Product Manager",
                        current_company="Example",
                        location="New York, NY",
                        linkedin_url=(
                            f"https://linkedin.com/in/q{prefix}-person-{index}"
                        ),
                        experiences=(),
                    )
                    for index in range(100)
                ),
                page=page,
                next_page=None,
                total_available=100,
            )

    gateway = QueryScopedGateway()

    def gateway_factory() -> QueryScopedGateway:
        nonlocal factory_calls
        factory_calls += 1
        return gateway

    execute_source_run(
        scenario["factory"],
        scenario["run_id"],
        context,
        gateway_factory=gateway_factory,
        idempotency_key="source:all-queries",
    )

    assert factory_calls == 1
    assert gateway.search_calls == 3
    with scenario["factory"]() as session:
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None
        assert run.candidate_count == 300
        assert run.state is RunState.MATCHING
