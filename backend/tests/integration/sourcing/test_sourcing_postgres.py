import os
import queue
import threading
from collections.abc import Generator
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.audit.models import AuditEvent  # noqa: F401
from app.audit.service import AuditService
from app.clients import models as client_models  # noqa: F401
from app.core.database import Base
from app.identity import models as identity_models  # noqa: F401
from app.identity.dependencies import apply_tenant_context
from app.identity.schemas import RequestContext, Role
from app.jobs import models as job_models  # noqa: F401
from app.providers.base import (
    ProviderPerson,
    ProviderQuery,
    ProviderTemporaryError,
    SearchPage,
)
from app.sourcing.models import (  # noqa: F401
    RunCandidate,
    RunCheckpoint,
    SourcingRun,
    TenantNotification,
    UsageBudget,
    UsageLedger,
)
from app.sourcing.service import SourcingError, SourcingService
from app.sourcing.state_machine import RunState
from app.sourcing.tasks import execute_plan_run, execute_source_run

OWNER_DATABASE_URL = os.getenv("TASK8_OWNER_DATABASE_URL")
API_DATABASE_URL = os.getenv("TASK8_API_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not OWNER_DATABASE_URL or not API_DATABASE_URL,
    reason="Task 8 PostgreSQL URLs are not configured",
)

_TENANT_TABLES = (
    "sourcing_runs",
    "run_checkpoints",
    "run_candidates",
    "usage_budgets",
    "usage_ledger",
    "tenant_notifications",
    "audit_events",
)


@pytest.fixture(scope="module")
def owner_engine() -> Generator[Engine, None, None]:
    assert OWNER_DATABASE_URL is not None
    engine = create_engine(OWNER_DATABASE_URL)
    _purge_task8_tenants(engine)
    try:
        yield engine
    finally:
        _purge_task8_tenants(engine)
        engine.dispose()


def _purge_task8_tenants(engine: Engine) -> None:
    """Remove only this module's durable fixtures between test invocations."""
    tables = set(inspect(engine).get_table_names())
    if "tenants" not in tables:
        return
    with engine.begin() as connection:
        user_ids: list[UUID] = []
        if "jobs" in tables:
            user_ids = list(
                connection.scalars(
                    text(
                        "SELECT DISTINCT jobs.owner_user_id FROM jobs "
                        "JOIN tenants ON tenants.id = jobs.tenant_id "
                        "WHERE tenants.slug LIKE 'task8-%'"
                    )
                )
            )
        if "audit_events" in tables:
            connection.execute(
                text(
                    "ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only"
                )
            )
            connection.execute(
                text(
                    "DELETE FROM audit_events WHERE tenant_id IN "
                    "(SELECT id FROM tenants WHERE slug LIKE 'task8-%')"
                )
            )
        connection.execute(text("DELETE FROM tenants WHERE slug LIKE 'task8-%'"))
        if "audit_events" in tables:
            connection.execute(
                text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only")
            )
        if user_ids:
            connection.execute(
                text("DELETE FROM users WHERE id = ANY(:user_ids)"),
                {"user_ids": user_ids},
            )


def _alembic_config() -> Config:
    return Config("alembic.ini")


def test_0005_migration_upgrade_downgrade_and_model_parity(
    owner_engine: Engine,
) -> None:
    command.downgrade(_alembic_config(), "base")
    command.upgrade(_alembic_config(), "0004_candidates")
    assert not set(_TENANT_TABLES).intersection(inspect(owner_engine).get_table_names())

    command.upgrade(_alembic_config(), "head")

    with owner_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0009_crm"
        )
        assert set(_TENANT_TABLES).issubset(inspect(connection).get_table_names())
        differences = compare_metadata(
            MigrationContext.configure(connection), Base.metadata
        )
    assert differences == []


def test_every_new_tenant_table_forces_rls_with_using_and_check(
    owner_engine: Engine,
) -> None:
    with owner_engine.connect() as connection:
        flags = connection.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname = ANY(:tables) ORDER BY relname"
            ),
            {"tables": list(_TENANT_TABLES)},
        ).all()
        policies = connection.execute(
            text(
                "SELECT tablename, qual, with_check FROM pg_policies "
                "WHERE tablename = ANY(:tables) ORDER BY tablename"
            ),
            {"tables": list(_TENANT_TABLES)},
        ).all()

    assert flags == sorted((table, True, True) for table in _TENANT_TABLES)
    assert [policy.tablename for policy in policies] == sorted(_TENANT_TABLES)
    assert all(policy.qual and policy.with_check for policy in policies)


def test_with_check_rejects_wrong_tenant_insert_on_every_new_table(
    owner_engine: Engine,
) -> None:
    assert API_DATABASE_URL is not None
    visible_tenant = uuid4()
    wrong_tenant = uuid4()
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, slug, created_at) "
                "VALUES (:visible, :visible_slug, now()), "
                "(:wrong, :wrong_slug, now())"
            ),
            {
                "visible": visible_tenant,
                "visible_slug": f"task8-visible-{visible_tenant}",
                "wrong": wrong_tenant,
                "wrong_slug": f"task8-wrong-{wrong_tenant}",
            },
        )
        connection.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                "IN SCHEMA public TO sourcing_api_test"
            )
        )

    statements = {
        "sourcing_runs": (
            "INSERT INTO sourcing_runs "
            "(id, tenant_id, job_id, scorecard_version_id, started_by_user_id, "
            "state, planned_queries, current_stage, cancellation_requested, "
            "candidate_count, matched_count, created_at, updated_at) VALUES "
            "(:id, :tenant, :parent, :parent2, :parent3, 'queued', '[]', "
            "'queued', false, 0, 0, now(), now())"
        ),
        "run_checkpoints": (
            "INSERT INTO run_checkpoints "
            "(id, tenant_id, run_id, idempotency_key, stage, status, started_at) "
            "VALUES (:id, :tenant, :parent, 'key', 'source', 'running', now())"
        ),
        "run_candidates": (
            "INSERT INTO run_candidates "
            "(id, tenant_id, run_id, candidate_id, scorecard_version_id, created_at) "
            "VALUES (:id, :tenant, :parent, :parent2, :parent3, now())"
        ),
        "usage_budgets": (
            "INSERT INTO usage_budgets "
            "(id, tenant_id, max_search_pages, created_at, updated_at) "
            "VALUES (:id, :tenant, 1, now(), now())"
        ),
        "usage_ledger": (
            "INSERT INTO usage_ledger "
            "(id, tenant_id, run_id, job_id, provider, endpoint, unit_type, "
            "reservation_key, requested_units, created_at) VALUES "
            "(:id, :tenant, :parent, :parent2, 'apollo', 'people_search', "
            "'search_pages', 'key', 1, now())"
        ),
        "tenant_notifications": (
            "INSERT INTO tenant_notifications "
            "(id, tenant_id, audience_role, code, title, message, created_at) "
            "VALUES (:id, :tenant, 'owner', 'code', 'title', 'message', now())"
        ),
        "audit_events": (
            "INSERT INTO audit_events "
            "(id, tenant_id, event_key, action, entity_type, entity_id, payload, "
            "created_at) VALUES "
            "(:id, :tenant, 'event', 'action', 'thing', :parent, '{}', now())"
        ),
    }
    api_engine = create_engine(API_DATABASE_URL)
    for statement in statements.values():
        with api_engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(visible_tenant)},
            )
            with pytest.raises(ProgrammingError, match="row-level security"):
                connection.execute(
                    text(statement),
                    {
                        "id": uuid4(),
                        "tenant": wrong_tenant,
                        "parent": uuid4(),
                        "parent2": uuid4(),
                        "parent3": uuid4(),
                    },
                )
            transaction.rollback()
    api_engine.dispose()
    with owner_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM tenants WHERE id IN (:visible, :wrong)"),
            {"visible": visible_tenant, "wrong": wrong_tenant},
        )


def test_audit_events_are_append_only_even_for_table_owner(
    owner_engine: Engine,
) -> None:
    tenant_id = uuid4()
    event_id = uuid4()
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, slug, created_at) "
                "VALUES (:tenant, :slug, now())"
            ),
            {"tenant": tenant_id, "slug": f"task8-audit-{tenant_id}"},
        )
        connection.execute(
            text(
                "INSERT INTO audit_events "
                "(id, tenant_id, event_key, action, entity_type, entity_id, payload, "
                "created_at) VALUES "
                "(:id, :tenant, 'append-only', 'created', 'thing', :entity, '{}', "
                "now())"
            ),
            {"id": event_id, "tenant": tenant_id, "entity": uuid4()},
        )
    for statement in (
        "UPDATE audit_events SET action = 'changed' WHERE id = :id",
        "DELETE FROM audit_events WHERE id = :id",
    ):
        with owner_engine.connect() as connection:
            transaction = connection.begin()
            with pytest.raises(SQLAlchemyError, match="append-only"):
                connection.execute(text(statement), {"id": event_id})
            transaction.rollback()


def test_concurrent_audit_replay_appends_one_event(owner_engine: Engine) -> None:
    assert API_DATABASE_URL is not None
    tenant_id = uuid4()
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, slug, created_at) "
                "VALUES (:tenant, :slug, now())"
            ),
            {"tenant": tenant_id, "slug": f"task8-audit-replay-{tenant_id}"},
        )
        connection.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                "IN SCHEMA public TO sourcing_api_test"
            )
        )
    barrier = threading.Barrier(2)
    outcomes: queue.Queue[object] = queue.Queue()

    def record() -> None:
        engine = create_engine(API_DATABASE_URL)
        with Session(engine, expire_on_commit=False) as session:
            apply_tenant_context(session, tenant_id)
            barrier.wait(timeout=5)
            try:
                event = AuditService(session).record(
                    tenant_id=tenant_id,
                    event_key="concurrent-event",
                    action="test.recorded",
                    entity_type="test",
                    entity_id=tenant_id,
                )
                session.commit()
            except Exception as error:  # noqa: BLE001 - thread outcome is asserted
                session.rollback()
                outcomes.put(error)
            else:
                outcomes.put(event.id)
        engine.dispose()

    threads = [threading.Thread(target=record) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    results = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert not any(isinstance(result, Exception) for result in results), results
    assert len(set(results)) == 1


def _create_job(
    connection,
    *,
    tenant_id,
    user_id,
    suffix: str,
) -> tuple:
    client_id = uuid4()
    job_id = uuid4()
    scorecard_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO client_companies "
            "(id, tenant_id, name, normalized_name, created_at) "
            "VALUES (:id, :tenant, :name, :normalized, now())"
        ),
        {
            "id": client_id,
            "tenant": tenant_id,
            "name": f"Task 8 Client {suffix}",
            "normalized": f"task 8 client {suffix}",
        },
    )
    connection.execute(
        text(
            "INSERT INTO jobs "
            "(id, tenant_id, client_id, owner_user_id, title, job_description, "
            "status, draft_revision, draft_extraction_status, created_at, updated_at) "
            "VALUES (:id, :tenant, :client, :user, :title, 'Job', "
            "'awaiting_scorecard', 0, 'ready', now(), now())"
        ),
        {
            "id": job_id,
            "tenant": tenant_id,
            "client": client_id,
            "user": user_id,
            "title": f"Job {suffix}",
        },
    )
    connection.execute(
        text(
            "INSERT INTO scorecard_versions "
            "(id, tenant_id, job_id, version, target_titles, seniority, "
            "locations, industry_code, suggested_adjacent_industries, "
            "uncertainties, extraction_status, confirmed_by_user_id, confirmed_at) "
            "VALUES (:id, :tenant, :job, 1, '[\"Product Manager\"]', '[]', "
            "'[]', 'technology.fintech', '[]', '[]', 'ready', :user, now())"
        ),
        {
            "id": scorecard_id,
            "tenant": tenant_id,
            "job": job_id,
            "user": user_id,
        },
    )
    connection.execute(
        text(
            "INSERT INTO scorecard_criteria "
            "(id, tenant_id, scorecard_version_id, position, key, label, kind, "
            "evidence_required, inferred, recruiter_entered, "
            "lawful_requirement_confirmed) VALUES "
            "(:id, :tenant, :scorecard, 0, 'payments', 'Payments experience', "
            "'must_have', false, false, false, false)"
        ),
        {"id": uuid4(), "tenant": tenant_id, "scorecard": scorecard_id},
    )
    connection.execute(
        text("UPDATE jobs SET current_scorecard_id = :scorecard WHERE id = :job"),
        {"scorecard": scorecard_id, "job": job_id},
    )
    return job_id, scorecard_id


@pytest.fixture
def concurrency_scenario(owner_engine: Engine) -> dict[str, object]:
    assert API_DATABASE_URL is not None
    tenant_id = uuid4()
    user_id = uuid4()
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, slug, created_at) VALUES (:id, :slug, now())"
            ),
            {"id": tenant_id, "slug": f"task8-concurrency-{tenant_id}"},
        )
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, oidc_subject, email, display_name, created_at) "
                "VALUES (:id, :subject, :email, 'Task 8 Owner', now())"
            ),
            {
                "id": user_id,
                "subject": f"oidc|{user_id}",
                "email": f"{user_id}@example.test",
            },
        )
        first_job_id, first_scorecard_id = _create_job(
            connection,
            tenant_id=tenant_id,
            user_id=user_id,
            suffix=f"first-{tenant_id}",
        )
        second_job_id, second_scorecard_id = _create_job(
            connection,
            tenant_id=tenant_id,
            user_id=user_id,
            suffix=f"second-{tenant_id}",
        )
        connection.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                "IN SCHEMA public TO sourcing_api_test"
            )
        )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "context": RequestContext(
            tenant_id=tenant_id, user_id=user_id, role=Role.OWNER
        ),
        "first_job_id": first_job_id,
        "first_scorecard_id": first_scorecard_id,
        "second_job_id": second_job_id,
        "second_scorecard_id": second_scorecard_id,
    }


def test_concurrent_start_and_checkpoint_replay_create_one_result(
    concurrency_scenario: dict[str, object],
) -> None:
    assert API_DATABASE_URL is not None
    scenario = concurrency_scenario
    barrier = threading.Barrier(2)
    outcomes: queue.Queue[object] = queue.Queue()

    def start() -> None:
        engine = create_engine(API_DATABASE_URL)
        with Session(engine) as session:
            apply_tenant_context(session, scenario["tenant_id"])
            barrier.wait(timeout=5)
            try:
                run = SourcingService(session, b"test-suppression-key").start(
                    scenario["context"],
                    scenario["first_job_id"],
                    idempotency_key="concurrent-start",
                )
                created_run_id = run.id
                session.commit()
            except Exception as error:  # noqa: BLE001 - thread outcome is asserted
                session.rollback()
                outcomes.put(error)
            else:
                outcomes.put(created_run_id)
        engine.dispose()

    threads = [threading.Thread(target=start) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    results = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert not any(isinstance(result, Exception) for result in results), results
    assert len(set(results)) == 1
    run_id = results[0]

    plan_barrier = threading.Barrier(2)
    plan_outcomes: queue.Queue[object] = queue.Queue()

    def plan() -> None:
        engine = create_engine(API_DATABASE_URL)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        plan_barrier.wait(timeout=5)
        try:
            execute_plan_run(
                factory,
                run_id,
                scenario["context"],
                idempotency_key="concurrent-plan",
            )
        except Exception as error:  # noqa: BLE001 - thread outcome is asserted
            plan_outcomes.put(error)
        else:
            plan_outcomes.put("ok")
        engine.dispose()

    threads = [threading.Thread(target=plan) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    plan_results = [plan_outcomes.get_nowait(), plan_outcomes.get_nowait()]
    assert plan_results == ["ok", "ok"]

    engine = create_engine(API_DATABASE_URL)
    with Session(engine) as session:
        apply_tenant_context(session, scenario["tenant_id"])
        run = session.get(SourcingRun, run_id)
        assert run is not None
        assert run.state is RunState.SOURCING
        assert session.scalar(select(func.count()).select_from(SourcingRun)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(RunCheckpoint)
                .where(RunCheckpoint.idempotency_key == "concurrent-plan")
            )
            == 1
        )
    engine.dispose()


def test_concurrent_distinct_start_keys_enforce_one_active_run(
    concurrency_scenario: dict[str, object],
) -> None:
    assert API_DATABASE_URL is not None
    scenario = concurrency_scenario
    barrier = threading.Barrier(2)
    outcomes: queue.Queue[object] = queue.Queue()

    def start(key: str) -> None:
        engine = create_engine(API_DATABASE_URL)
        with Session(engine, expire_on_commit=False) as session:
            apply_tenant_context(session, scenario["tenant_id"])
            barrier.wait(timeout=5)
            try:
                run = SourcingService(session, b"test-suppression-key").start(
                    scenario["context"],
                    scenario["first_job_id"],
                    idempotency_key=key,
                )
                session.commit()
            except SourcingError as error:
                session.rollback()
                outcomes.put(error.code)
            else:
                outcomes.put(run.id)
        engine.dispose()

    threads = (
        threading.Thread(target=start, args=("active-start-one",)),
        threading.Thread(target=start, args=("active-start-two",)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    results = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert sum(isinstance(result, UUID) for result in results) == 1
    assert "active_run_exists" in results


def test_transactional_budget_reservation_serializes_across_runs(
    concurrency_scenario: dict[str, object],
) -> None:
    assert API_DATABASE_URL is not None
    scenario = concurrency_scenario
    owner_url = OWNER_DATABASE_URL
    assert owner_url is not None
    owner = create_engine(owner_url)
    run_ids = (uuid4(), uuid4())
    with owner.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sourcing_runs "
                "(id, tenant_id, job_id, scorecard_version_id, started_by_user_id, "
                "state, planned_queries, current_stage, cancellation_requested, "
                "candidate_count, matched_count, created_at, updated_at) VALUES "
                "(:first, :tenant, :first_job, :first_scorecard, :user, 'sourcing', "
                "'[]', 'sourcing', false, 0, 0, now(), now()), "
                "(:second, :tenant, :second_job, :second_scorecard, :user, "
                "'sourcing', '[]', 'sourcing', false, 0, 0, now(), now())"
            ),
            {
                "first": run_ids[0],
                "second": run_ids[1],
                "tenant": scenario["tenant_id"],
                "user": scenario["user_id"],
                "first_job": scenario["first_job_id"],
                "first_scorecard": scenario["first_scorecard_id"],
                "second_job": scenario["second_job_id"],
                "second_scorecard": scenario["second_scorecard_id"],
            },
        )
        connection.execute(
            text(
                "INSERT INTO usage_budgets "
                "(id, tenant_id, max_search_pages, created_at, updated_at) "
                "VALUES (:id, :tenant, 1, now(), now())"
            ),
            {"id": uuid4(), "tenant": scenario["tenant_id"]},
        )
    owner.dispose()
    barrier = threading.Barrier(2)
    outcomes: queue.Queue[str] = queue.Queue()

    def reserve(run_id) -> None:
        engine = create_engine(API_DATABASE_URL)
        with Session(engine) as session:
            apply_tenant_context(session, scenario["tenant_id"])
            barrier.wait(timeout=5)
            try:
                SourcingService(session, b"test-suppression-key").reserve_usage(
                    scenario["context"],
                    run_id,
                    provider="apollo",
                    endpoint="people_search",
                    reservation_key=f"reservation-{run_id}",
                    requested_units={"search_pages": 1},
                )
            except SourcingError as error:
                outcomes.put(error.code)
            else:
                outcomes.put("reserved")
            session.commit()
        engine.dispose()

    threads = [threading.Thread(target=reserve, args=(run_id,)) for run_id in run_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted((outcomes.get_nowait(), outcomes.get_nowait())) == [
        "reserved",
        "usage_budget_exhausted",
    ]


def test_cancellation_and_plan_transition_race_finishes_cancelled(
    concurrency_scenario: dict[str, object],
) -> None:
    assert API_DATABASE_URL is not None
    scenario = concurrency_scenario
    engine = create_engine(API_DATABASE_URL)
    with Session(engine) as session:
        apply_tenant_context(session, scenario["tenant_id"])
        run = SourcingService(session, b"test-suppression-key").start(
            scenario["context"],
            scenario["second_job_id"],
            idempotency_key="start-cancel-race",
        )
        run_id = run.id
        session.commit()
    engine.dispose()
    barrier = threading.Barrier(2)
    outcomes: queue.Queue[object] = queue.Queue()

    def plan() -> None:
        worker_engine = create_engine(API_DATABASE_URL)
        factory = sessionmaker(bind=worker_engine, expire_on_commit=False)
        barrier.wait(timeout=5)
        try:
            execute_plan_run(
                factory,
                run_id,
                scenario["context"],
                idempotency_key="plan-cancel-race",
            )
        except Exception as error:  # noqa: BLE001 - thread outcome is asserted
            outcomes.put(error)
        else:
            outcomes.put("planned")
        worker_engine.dispose()

    def cancel() -> None:
        cancel_engine = create_engine(API_DATABASE_URL)
        with Session(cancel_engine) as session:
            apply_tenant_context(session, scenario["tenant_id"])
            barrier.wait(timeout=5)
            try:
                SourcingService(session, b"test-suppression-key").cancel(
                    scenario["context"],
                    run_id,
                    idempotency_key="cancel-race",
                )
                session.commit()
            except Exception as error:  # noqa: BLE001 - thread outcome is asserted
                session.rollback()
                outcomes.put(error)
            else:
                outcomes.put("cancelled")
        cancel_engine.dispose()

    threads = (threading.Thread(target=plan), threading.Thread(target=cancel))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    results = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert not any(isinstance(result, Exception) for result in results), results

    engine = create_engine(API_DATABASE_URL)
    with Session(engine) as session:
        apply_tenant_context(session, scenario["tenant_id"])
        run = session.get(SourcingRun, run_id)
        assert run is not None
        assert run.state is RunState.CANCELLED
        assert run.cancellation_requested is True
    engine.dispose()


def _create_postgres_source_run(
    scenario: dict[str, object], idempotency_key: str
) -> tuple[Engine, sessionmaker[Session], UUID]:
    assert API_DATABASE_URL is not None
    engine = create_engine(API_DATABASE_URL)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        apply_tenant_context(session, scenario["tenant_id"])
        run = SourcingService(session, b"test-suppression-key").start(
            scenario["context"],
            scenario["first_job_id"],
            idempotency_key=idempotency_key,
        )
        run.state = RunState.SOURCING
        run.current_stage = RunState.SOURCING.value
        run.planned_queries = [
            {
                "titles": ["Product Manager"],
                "seniorities": [],
                "person_locations": [],
                "industry_codes": ["technology.fintech"],
                "keywords": [],
            }
        ]
        run_id = run.id
        session.commit()
    return engine, factory, run_id


def _provider_person(provider_id: str, *, shared_url: bool = False) -> ProviderPerson:
    return ProviderPerson(
        provider="apollo",
        provider_person_id=provider_id,
        full_name="Task 8 Provider Person",
        current_title="Product Manager",
        current_company="Example",
        location="New York, NY",
        linkedin_url=(
            "https://linkedin.com/in/task8-retry-shared"
            if shared_url
            else f"https://linkedin.com/in/{provider_id}"
        ),
        experiences=(),
    )


def test_cross_key_source_runs_share_one_postgres_execution_lock(
    concurrency_scenario: dict[str, object],
) -> None:
    scenario = concurrency_scenario
    engine, factory, run_id = _create_postgres_source_run(
        scenario, "start-cross-key-source"
    )
    entered = threading.Event()
    release = threading.Event()
    factory_calls = 0
    active_calls = 0
    maximum_active_calls = 0
    guard = threading.Lock()
    outcomes: queue.Queue[object] = queue.Queue()

    class BlockingGateway:
        def search(self, query: ProviderQuery, page: int) -> SearchPage:
            nonlocal active_calls, maximum_active_calls
            with guard:
                active_calls += 1
                maximum_active_calls = max(maximum_active_calls, active_calls)
            entered.set()
            try:
                if not release.wait(timeout=5):
                    raise TimeoutError("cross-key provider was not released")
                return SearchPage(
                    people=(_provider_person("cross-key-person"),),
                    page=page,
                    next_page=None,
                    total_available=1,
                )
            finally:
                with guard:
                    active_calls -= 1

        def close(self) -> None:
            return None

    def gateway_factory() -> BlockingGateway:
        nonlocal factory_calls
        with guard:
            factory_calls += 1
        return BlockingGateway()

    def source(key: str) -> None:
        try:
            execute_source_run(
                factory,
                run_id,
                scenario["context"],
                gateway_factory=gateway_factory,
                idempotency_key=key,
            )
        except Exception as error:  # noqa: BLE001 - thread outcome is asserted
            outcomes.put(error)
        else:
            outcomes.put("ok")

    first = threading.Thread(target=source, args=("source:cross-key-one",))
    second = threading.Thread(target=source, args=("source:cross-key-two",))
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    second.join(timeout=1)
    second_returned_while_first_blocked = not second.is_alive()
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive() and not second.is_alive()
    results = [outcomes.get_nowait(), outcomes.get_nowait()]

    assert second_returned_while_first_blocked
    assert not any(isinstance(result, Exception) for result in results), results
    assert factory_calls == 1
    assert maximum_active_calls == 1
    engine.dispose()


def test_postgres_retry_rehydrates_seen_provider_ids_and_caps_run_at_300(
    concurrency_scenario: dict[str, object],
) -> None:
    scenario = concurrency_scenario
    engine, factory, run_id = _create_postgres_source_run(
        scenario, "start-retry-rehydration"
    )

    class RetryGateway:
        def __init__(self, *, fail_page_three: bool) -> None:
            self.fail_page_three = fail_page_three
            self.seen: set[str] = set()
            self.restored: set[str] = set()
            self.search_pages: list[int] = []

        def restore_seen_provider_ids(self, provider_ids: set[str]) -> None:
            self.restored = set(provider_ids)
            self.seen.update(provider_ids)

        def search(self, query: ProviderQuery, page: int) -> SearchPage:
            self.search_pages.append(page)
            if self.fail_page_three and page == 3:
                raise ProviderTemporaryError("retry after completed pages")
            start = (page - 1) * 100
            available = [f"retry-person-{index}" for index in range(start, start + 100)]
            remaining = max(0, 300 - len(self.seen))
            returned_ids = available[:remaining]
            self.seen.update(returned_ids)
            return SearchPage(
                people=tuple(
                    _provider_person(provider_id, shared_url=True)
                    for provider_id in returned_ids
                ),
                page=page,
                next_page=(page + 1 if len(self.seen) < 300 else None),
                total_available=400,
            )

        def close(self) -> None:
            return None

    first_gateway = RetryGateway(fail_page_three=True)
    with pytest.raises(ProviderTemporaryError, match="retry after completed pages"):
        execute_source_run(
            factory,
            run_id,
            scenario["context"],
            gateway_factory=lambda: first_gateway,
            idempotency_key="source:retry-rehydration",
            propagate_provider_errors=True,
        )
    assert first_gateway.search_pages == [1, 2, 3]

    retry_gateway = RetryGateway(fail_page_three=False)
    execute_source_run(
        factory,
        run_id,
        scenario["context"],
        gateway_factory=lambda: retry_gateway,
        idempotency_key="source:retry-rehydration",
    )

    assert len(retry_gateway.restored) == 200
    assert retry_gateway.search_pages == [3]
    assert len(retry_gateway.seen) == 300
    with factory() as session:
        apply_tenant_context(session, scenario["tenant_id"])
        run = session.get(SourcingRun, run_id)
        assert run is not None
        assert run.state is RunState.MATCHING
        persisted_provider_ids = {
            provider_id
            for payload in session.scalars(
                select(RunCheckpoint.payload).where(
                    RunCheckpoint.run_id == run_id,
                    RunCheckpoint.stage == "source",
                    RunCheckpoint.status == "completed",
                )
            )
            if payload is not None
            for provider_id in payload.get("provider_person_ids", [])
        }
        assert len(persisted_provider_ids) == 300
    engine.dispose()
