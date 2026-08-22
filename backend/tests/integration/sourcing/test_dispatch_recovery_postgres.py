import os
import queue
import threading
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session

from alembic import command
from app.candidates.models import Candidate
from app.clients.models import ClientCompany
from app.core.database import Base
from app.identity.models import Tenant, User
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardVersion
from app.sourcing import tasks
from app.sourcing.dispatch_recovery import (
    recover_pending_dispatches,
    recover_pending_enrichment_dispatches,
    recover_pending_enrichment_retries,
)
from app.sourcing.enrichment import DeferredEnrichment
from app.sourcing.models import (
    EnrichmentRequest,
    EnrichmentRetryDispatch,
    RunCandidate,
    SourcingRun,
)
from app.sourcing.service import SourcingService
from app.sourcing.state_machine import RunState

OWNER_DATABASE_URL = os.getenv("TASK12_OWNER_DATABASE_URL")
API_DATABASE_URL = os.getenv("TASK12_API_DATABASE_URL")
MAINTENANCE_DATABASE_URL = os.getenv("TASK12_MAINTENANCE_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not OWNER_DATABASE_URL or not API_DATABASE_URL or not MAINTENANCE_DATABASE_URL,
    reason="Task 12 PostgreSQL URLs are not configured",
)


def _config() -> Config:
    return Config("alembic.ini")


def _cleanup(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
        )
        connection.execute(
            text(
                "ALTER TABLE crm_acceptance_cohorts "
                "DISABLE TRIGGER crm_acceptance_cohorts_append_only"
            )
        )
        connection.execute(
            text(
                "DELETE FROM audit_events WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug LIKE 'task12-dispatch-%')"
            )
        )
        connection.execute(
            text("DELETE FROM tenants WHERE slug LIKE 'task12-dispatch-%'")
        )
        connection.execute(
            text("DELETE FROM users WHERE oidc_subject LIKE 'task12-dispatch|%'")
        )
        connection.execute(
            text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only")
        )
        connection.execute(
            text(
                "ALTER TABLE crm_acceptance_cohorts "
                "ENABLE TRIGGER crm_acceptance_cohorts_append_only"
            )
        )


def _grant_api_test_access(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE "
                "ON enrichment_retry_dispatches TO sourcing_api_test"
            )
        )


@pytest.fixture(scope="module")
def owner_engine() -> Generator[Engine, None, None]:
    assert OWNER_DATABASE_URL is not None
    engine = create_engine(OWNER_DATABASE_URL)
    with engine.begin() as connection:
        connection.execute(
            text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE "
                "rolname = 'sourcing_maintenance') THEN CREATE ROLE "
                "sourcing_maintenance LOGIN PASSWORD "
                "'sourcing-maintenance-test'; END IF; END $$"
            )
        )
        connection.execute(
            text(
                "GRANT CONNECT ON DATABASE sourcing_test TO sourcing_maintenance; "
                "GRANT USAGE ON SCHEMA public TO sourcing_maintenance"
            )
        )
    command.upgrade(_config(), "head")
    _grant_api_test_access(engine)
    _cleanup(engine)
    try:
        yield engine
    finally:
        _cleanup(engine)
        engine.dispose()


def _seed_run(engine: Engine) -> SourcingRun:
    marker = uuid4()
    tenant = Tenant(id=uuid4(), slug=f"task12-dispatch-{marker}")
    user = User(
        id=uuid4(),
        oidc_subject=f"task12-dispatch|{marker}",
        email=f"{marker}@example.test",
        display_name="Dispatch Test",
    )
    with Session(engine, expire_on_commit=False) as session:
        session.add_all((tenant, user))
        session.flush()
        client = ClientCompany(
            tenant_id=tenant.id,
            name="Dispatch Client",
            normalized_name=f"dispatch-client-{marker}",
        )
        session.add(client)
        session.flush()
        job = Job(
            tenant_id=tenant.id,
            client_id=client.id,
            owner_user_id=user.id,
            title="Dispatch Recovery",
            job_description="Recover a committed sourcing dispatch.",
        )
        session.add(job)
        session.flush()
        scorecard = ScorecardVersion(
            tenant_id=tenant.id,
            job_id=job.id,
            version=1,
            target_titles=["Engineer"],
            seniority=[],
            locations=[],
            industry_code="technology.software",
            suggested_adjacent_industries=[],
            uncertainties=[],
            extraction_status="ready",
            confirmed_by_user_id=user.id,
            confirmed_at=datetime.now(UTC),
        )
        session.add(scorecard)
        session.flush()
        run = SourcingRun(
            tenant_id=tenant.id,
            job_id=job.id,
            scorecard_version_id=scorecard.id,
            started_by_user_id=user.id,
            state=RunState.QUEUED,
            dispatch_pending=True,
        )
        session.add(run)
        session.commit()
        return run


def _seed_pending_enrichment(engine: Engine) -> EnrichmentRequest:
    run = _seed_run(engine)
    with Session(engine, expire_on_commit=False) as session:
        stored_run = session.get(SourcingRun, run.id)
        assert stored_run is not None
        stored_run.dispatch_pending = False
        request = EnrichmentRequest(
            tenant_id=run.tenant_id,
            run_id=run.id,
            provider="apollo",
            candidate_ids=[str(uuid4())],
            reservation_key=f"dispatch-recovery-{uuid4()}",
            status="queued",
            dispatch_pending=True,
            dispatch_requested_by_user_id=run.started_by_user_id,
        )
        session.add(request)
        session.commit()
        return request


def _seed_enrichment_retry(
    engine: Engine,
    *,
    status: str = "pending",
    generation: int = 1,
    claimed_at: datetime | None = None,
) -> EnrichmentRetryDispatch:
    run = _seed_run(engine)
    with Session(engine, expire_on_commit=False) as session:
        retry = EnrichmentRetryDispatch(
            tenant_id=run.tenant_id,
            run_id=run.id,
            generation=generation,
            status=status,
            state_fingerprint="0" * 64,
            task_id=f"enrich-run-retry:{run.id}:{generation}",
            requested_by_user_id=run.started_by_user_id,
            candidate_limit=50,
            not_before=datetime.now(UTC) - timedelta(minutes=1),
            claim_token=uuid4() if claimed_at is not None else None,
            claimed_at=claimed_at,
        )
        session.add(retry)
        session.commit()
        return retry


def _seed_active_on_demand_request(
    engine: Engine,
) -> tuple[EnrichmentRequest, UUID, RequestContext]:
    request = _seed_pending_enrichment(engine)
    with Session(engine, expire_on_commit=False) as session:
        run = session.get(SourcingRun, request.run_id)
        assert run is not None
        candidate = Candidate(
            id=UUID(request.candidate_ids[0]),
            tenant_id=request.tenant_id,
            full_name="Dispatch Recovery Candidate",
            normalized_name="dispatch recovery candidate",
        )
        session.add(candidate)
        session.flush()
        row = RunCandidate(
            tenant_id=request.tenant_id,
            run_id=run.id,
            candidate_id=candidate.id,
            scorecard_version_id=run.scorecard_version_id,
            match_score=1,
            classification="main",
            enrichment_status="pending",
        )
        session.add(row)
        session.commit()
        context = RequestContext(
            tenant_id=request.tenant_id,
            user_id=run.started_by_user_id,
            role=Role.OWNER,
        )
        return request, row.id, context


def _claim_one() -> tuple[UUID, UUID, UUID, UUID, str]:
    assert MAINTENANCE_DATABASE_URL is not None
    engine = create_engine(MAINTENANCE_DATABASE_URL)
    try:
        with Session(engine) as session:
            row = session.execute(
                text(
                    "SELECT run_id, tenant_id, user_id, claim_token, dispatch_key "
                    "FROM maintenance_claim_pending_sourcing_dispatches(1)"
                )
            ).one()
            session.commit()
            return tuple(row)  # type: ignore[return-value]
    finally:
        engine.dispose()


def _claim_one_enrichment() -> tuple[UUID, UUID, UUID, UUID, str]:
    assert MAINTENANCE_DATABASE_URL is not None
    engine = create_engine(MAINTENANCE_DATABASE_URL)
    try:
        with Session(engine) as session:
            row = session.execute(
                text(
                    "SELECT request_id, tenant_id, user_id, claim_token, "
                    "dispatch_key FROM "
                    "maintenance_claim_pending_enrichment_dispatches(1)"
                )
            ).one()
            session.commit()
            return tuple(row)  # type: ignore[return-value]
    finally:
        engine.dispose()


def _claim_one_enrichment_retry() -> tuple[object, ...] | None:
    assert MAINTENANCE_DATABASE_URL is not None
    engine = create_engine(MAINTENANCE_DATABASE_URL)
    try:
        with Session(engine) as session:
            row = session.execute(
                text(
                    "SELECT tenant_id, run_id, generation, user_id, "
                    "candidate_limit, task_id, claim_token FROM "
                    "maintenance_claim_pending_enrichment_retries(1)"
                )
            ).one_or_none()
            session.commit()
            return tuple(row) if row is not None else None
    finally:
        engine.dispose()


def _complete_enrichment_retry_claim(claim: tuple[object, ...]) -> None:
    assert MAINTENANCE_DATABASE_URL is not None
    engine = create_engine(MAINTENANCE_DATABASE_URL)
    try:
        with Session(engine) as session:
            completed = session.scalar(
                text(
                    "SELECT maintenance_complete_enrichment_retry_publish("
                    ":tenant_id, :run_id, :generation, :claim_token)"
                ),
                {
                    "tenant_id": claim[0],
                    "run_id": claim[1],
                    "generation": claim[2],
                    "claim_token": claim[6],
                },
            )
            session.commit()
            assert completed is True
    finally:
        engine.dispose()


def _terminalize_enrichment_retry(engine: Engine, run_id: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE enrichment_retry_dispatches SET status = 'completed' "
                "WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        )


def test_0012_upgrade_downgrade_upgrade_and_model_parity(
    owner_engine: Engine,
) -> None:
    command.downgrade(_config(), "0011_sourcing_dispatch_recovery")
    enrichment_columns = {
        column["name"]
        for column in inspect(owner_engine).get_columns("enrichment_requests")
    }
    assert "dispatch_pending" not in enrichment_columns
    command.upgrade(_config(), "head")
    _grant_api_test_access(owner_engine)

    with owner_engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0017_enrich_dispatch_deadlines"
        )
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )


def test_0016_upgrade_downgrade_upgrade_and_model_parity(
    owner_engine: Engine,
) -> None:
    command.downgrade(_config(), "0015_tenant_acceptance_fks")
    assert "enrichment_retry_dispatches" not in inspect(owner_engine).get_table_names()
    command.upgrade(_config(), "head")
    _grant_api_test_access(owner_engine)

    with owner_engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0017_enrich_dispatch_deadlines"
        )
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )


def test_0017_upgrade_downgrade_upgrade_and_model_parity(
    owner_engine: Engine,
) -> None:
    command.downgrade(_config(), "0016_enrichment_retry_dispatch")
    with owner_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0016_enrichment_retry_dispatch"
        )
    command.upgrade(_config(), "head")
    _grant_api_test_access(owner_engine)

    with owner_engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0017_enrich_dispatch_deadlines"
        )
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )


def test_dispatch_functions_cross_forced_rls_without_table_grants(
    owner_engine: Engine,
) -> None:
    first, second = _seed_run(owner_engine), _seed_run(owner_engine)
    assert first.tenant_id != second.tenant_id
    assert MAINTENANCE_DATABASE_URL is not None
    maintenance_engine = create_engine(MAINTENANCE_DATABASE_URL)
    with Session(maintenance_engine) as session:
        claims = session.execute(
            text(
                "SELECT run_id, tenant_id FROM "
                "maintenance_claim_pending_sourcing_dispatches(100)"
            )
        ).all()
        session.commit()
    maintenance_engine.dispose()

    with owner_engine.connect() as connection:
        rls = connection.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'sourcing_runs'"
            )
        ).one()
        table_grants = connection.execute(
            text(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'sourcing_maintenance' "
                "AND table_name = 'sourcing_runs'"
            )
        ).all()
        routines = set(
            connection.scalars(
                text(
                    "SELECT routine_name FROM information_schema.role_routine_grants "
                    "WHERE grantee = 'sourcing_maintenance' "
                    "AND routine_name LIKE 'maintenance_%sourcing_dispatch%'"
                )
            )
        )
    assert rls == (True, True)
    assert table_grants == []
    assert routines == {
        "maintenance_claim_pending_sourcing_dispatches",
        "maintenance_complete_sourcing_dispatch",
        "maintenance_release_sourcing_dispatch",
    }
    assert {row.run_id for row in claims} == {first.id, second.id}

    assert API_DATABASE_URL is not None
    api_engine = create_engine(API_DATABASE_URL)
    with Session(api_engine) as session, pytest.raises(ProgrammingError):
        session.execute(
            text("SELECT * FROM maintenance_claim_pending_sourcing_dispatches(1)")
        ).all()
    api_engine.dispose()


def test_concurrent_claimers_receive_distinct_runs(owner_engine: Engine) -> None:
    expected = {_seed_run(owner_engine).id, _seed_run(owner_engine).id}
    barrier = threading.Barrier(2)
    outcomes: queue.Queue[object] = queue.Queue()

    def claim() -> None:
        barrier.wait(timeout=5)
        try:
            outcomes.put(_claim_one()[0])
        except Exception as error:  # noqa: BLE001 - thread result is asserted
            outcomes.put(error)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    claimed = {outcomes.get_nowait(), outcomes.get_nowait()}
    assert not any(isinstance(item, Exception) for item in claimed), claimed
    assert claimed == expected


def test_enrichment_dispatch_functions_have_no_table_grants_and_api_cannot_call(
    owner_engine: Engine,
) -> None:
    request = _seed_pending_enrichment(owner_engine)
    claimed = _claim_one_enrichment()
    assert claimed[0] == request.id
    assert claimed[4] == f"enrichment-request-{request.id}"

    with owner_engine.connect() as connection:
        table_grants = connection.execute(
            text(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'sourcing_maintenance' "
                "AND table_name = 'enrichment_requests'"
            )
        ).all()
        routines = set(
            connection.scalars(
                text(
                    "SELECT routine_name FROM information_schema.role_routine_grants "
                    "WHERE grantee = 'sourcing_maintenance' "
                    "AND routine_name LIKE 'maintenance_%enrichment_dispatch%'"
                )
            )
        )
    assert table_grants == []
    assert routines == {
        "maintenance_claim_pending_enrichment_dispatches",
        "maintenance_complete_enrichment_dispatch",
        "maintenance_release_enrichment_dispatch",
    }

    assert API_DATABASE_URL is not None
    api_engine = create_engine(API_DATABASE_URL)
    with Session(api_engine) as session, pytest.raises(ProgrammingError):
        session.execute(
            text("SELECT * FROM maintenance_claim_pending_enrichment_dispatches(1)")
        ).all()
    api_engine.dispose()


def test_concurrent_claimers_receive_distinct_enrichment_requests(
    owner_engine: Engine,
) -> None:
    expected = {
        _seed_pending_enrichment(owner_engine).id,
        _seed_pending_enrichment(owner_engine).id,
    }
    barrier = threading.Barrier(2)
    outcomes: queue.Queue[object] = queue.Queue()

    def claim() -> None:
        barrier.wait(timeout=5)
        try:
            outcomes.put(_claim_one_enrichment()[0])
        except Exception as error:  # noqa: BLE001 - thread result is asserted
            outcomes.put(error)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    claimed = {outcomes.get_nowait(), outcomes.get_nowait()}
    assert not any(isinstance(item, Exception) for item in claimed), claimed
    assert claimed == expected


def test_concurrent_browser_intents_bind_one_request_and_one_publisher(
    owner_engine: Engine,
) -> None:
    request, run_candidate_id, context = _seed_active_on_demand_request(owner_engine)
    barrier = threading.Barrier(2)
    outcomes: queue.Queue[object] = queue.Queue()

    def queue_intent(suffix: str) -> None:
        try:
            barrier.wait(timeout=5)
            with Session(owner_engine, expire_on_commit=False) as session:
                outcome = SourcingService(
                    session, b"dispatch-recovery"
                ).queue_on_demand_enrichment(
                    context,
                    run_candidate_id,
                    idempotency_key=f"concurrent-browser-{suffix}",
                )
                session.commit()
                outcomes.put((outcome.request.id, outcome.claim_token))
        except Exception as error:  # noqa: BLE001 - thread result is asserted
            outcomes.put(error)

    threads = [
        threading.Thread(target=queue_intent, args=(suffix,))
        for suffix in ("first", "second")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    observed = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert not any(isinstance(item, Exception) for item in observed), observed
    requests = [item for item in observed if isinstance(item, tuple)]
    assert {item[0] for item in requests} == {request.id}
    assert sum(item[1] is not None for item in requests) == 1


def test_periodic_recovery_handles_no_client_retry_and_crash_after_publish(
    owner_engine: Engine,
) -> None:
    assert MAINTENANCE_DATABASE_URL is not None
    run = _seed_run(owner_engine)
    first_claim = _claim_one()
    assert first_claim[0] == run.id
    first_key = first_claim[4]

    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sourcing_runs SET dispatch_claimed_at = :stale "
                "WHERE id = :run_id"
            ),
            {"stale": datetime.now(UTC) - timedelta(minutes=6), "run_id": run.id},
        )

    published: list[str] = [first_key]
    result = recover_pending_dispatches(
        MAINTENANCE_DATABASE_URL,
        lambda claim: published.append(claim.dispatch_key),
    )

    assert result.published == 1
    assert published == [f"sourcing-plan-{run.id}"] * 2
    with Session(owner_engine) as session:
        recovered = session.get(SourcingRun, run.id)
        assert recovered is not None
        assert recovered.dispatch_pending is False
        assert recovered.dispatch_claimed_at is None
        assert recovered.dispatch_claim_token is None


def test_enrichment_periodic_recovery_handles_no_client_retry_and_crash(
    owner_engine: Engine,
) -> None:
    assert MAINTENANCE_DATABASE_URL is not None
    request = _seed_pending_enrichment(owner_engine)
    first_claim = _claim_one_enrichment()
    assert first_claim[0] == request.id
    first_key = first_claim[4]

    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE enrichment_requests SET dispatch_claimed_at = :stale "
                "WHERE id = :request_id"
            ),
            {
                "stale": datetime.now(UTC) - timedelta(minutes=6),
                "request_id": request.id,
            },
        )

    published: list[str] = [first_key]
    result = recover_pending_enrichment_dispatches(
        MAINTENANCE_DATABASE_URL,
        lambda claim: published.append(claim.dispatch_key),
    )

    assert result.published == 1
    assert published == [f"enrichment-request-{request.id}"] * 2
    with Session(owner_engine) as session:
        recovered = session.get(EnrichmentRequest, request.id)
        assert recovered is not None
        assert recovered.dispatch_pending is False
        assert recovered.dispatch_claimed_at is None
        assert recovered.dispatch_claim_token is None


def test_enrichment_retry_functions_cross_rls_without_table_grants(
    owner_engine: Engine,
) -> None:
    first = _seed_enrichment_retry(owner_engine)
    second = _seed_enrichment_retry(owner_engine)

    assert API_DATABASE_URL is not None
    api_engine = create_engine(API_DATABASE_URL)
    with Session(api_engine) as session:
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(first.tenant_id)},
        )
        visible = session.scalars(
            text("SELECT run_id FROM enrichment_retry_dispatches")
        ).all()
        session.rollback()
    assert visible == [first.run_id]

    with owner_engine.connect() as connection:
        rls = connection.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE relname = 'enrichment_retry_dispatches'"
            )
        ).one()
        table_grants = connection.execute(
            text(
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'sourcing_maintenance' "
                "AND table_name = 'enrichment_retry_dispatches'"
            )
        ).all()
        routines = set(
            connection.scalars(
                text(
                    "SELECT routine_name FROM "
                    "information_schema.role_routine_grants "
                    "WHERE grantee = 'sourcing_maintenance' "
                    "AND routine_name LIKE 'maintenance_%enrichment_retr%'"
                )
            )
        )
    assert rls == (True, True)
    assert table_grants == []
    assert routines == {
        "maintenance_claim_pending_enrichment_retries",
        "maintenance_complete_enrichment_retry_publish",
        "maintenance_release_enrichment_retry_publish",
    }

    with Session(api_engine) as session, pytest.raises(ProgrammingError):
        session.execute(
            text("SELECT * FROM maintenance_claim_pending_enrichment_retries(1)")
        ).all()
    api_engine.dispose()

    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE enrichment_retry_dispatches SET status = 'completed' "
                "WHERE run_id IN (:first_run, :second_run)"
            ),
            {"first_run": first.run_id, "second_run": second.run_id},
        )


def test_concurrent_retry_publishers_claim_one_generation_once(
    owner_engine: Engine,
) -> None:
    retry = _seed_enrichment_retry(owner_engine)
    barrier = threading.Barrier(2)
    outcomes: queue.Queue[object] = queue.Queue()

    def claim() -> None:
        barrier.wait(timeout=5)
        try:
            outcomes.put(_claim_one_enrichment_retry())
        except Exception as error:  # noqa: BLE001 - thread result is asserted
            outcomes.put(error)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    claimed = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert not any(isinstance(item, Exception) for item in claimed), claimed
    claims = [item for item in claimed if isinstance(item, tuple)]
    assert len(claims) == 1
    assert claims[0][1] == retry.run_id
    assert sum(item is None for item in claimed) == 1
    _complete_enrichment_retry_claim(claims[0])
    _terminalize_enrichment_retry(owner_engine, retry.run_id)


def test_expired_retry_delivery_claim_is_reclaimed(
    owner_engine: Engine,
) -> None:
    retry = _seed_enrichment_retry(
        owner_engine,
        status="claimed",
        claimed_at=datetime.now(UTC) - timedelta(minutes=16),
    )

    claim = _claim_one_enrichment_retry()

    assert claim is not None
    assert claim[1] == retry.run_id
    assert claim[6] != retry.claim_token
    _complete_enrichment_retry_claim(claim)
    _terminalize_enrichment_retry(owner_engine, retry.run_id)


@pytest.mark.parametrize("initial_status", ["pending", "published"])
def test_periodic_recovery_publishes_committed_retry_without_client_activity(
    owner_engine: Engine,
    initial_status: str,
) -> None:
    assert MAINTENANCE_DATABASE_URL is not None
    retry = _seed_enrichment_retry(owner_engine, status=initial_status)
    published: list[object] = []

    result = recover_pending_enrichment_retries(
        MAINTENANCE_DATABASE_URL,
        published.append,
    )

    assert result.published == 1
    assert result.failed == 0
    assert len(published) == 1
    claim = published[0]
    assert claim.run_id == retry.run_id
    assert claim.generation == retry.generation
    assert claim.dispatch_key == retry.task_id
    with Session(owner_engine) as session:
        recovered = session.get(
            EnrichmentRetryDispatch,
            (retry.tenant_id, retry.run_id),
        )
        assert recovered is not None
        assert recovered.status == "published"
        assert recovered.claim_token is None
        assert recovered.claimed_at is None
    _terminalize_enrichment_retry(owner_engine, retry.run_id)


def test_duplicate_generation_is_serialized_across_rate_limit_pause(
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry = _seed_enrichment_retry(owner_engine, status="published")
    provider_entered = threading.Event()
    release_provider = threading.Event()
    provider_calls: list[UUID] = []
    published: list[object] = []
    outcomes: queue.Queue[object] = queue.Queue()

    class Gateway:
        def close(self) -> None:
            return None

    def rate_limited(*args: object, **kwargs: object) -> list[DeferredEnrichment]:
        provider_calls.append(retry.run_id)
        provider_entered.set()
        assert release_provider.wait(timeout=5)
        return [DeferredEnrichment(retry_after_seconds=30)]

    monkeypatch.setattr(tasks, "is_provider_enabled", lambda *args: True)
    monkeypatch.setattr(
        tasks,
        "get_worker_settings",
        lambda: SimpleNamespace(webhook_base_url="https://callback.test"),
    )
    monkeypatch.setattr(tasks, "_enrichment_dependencies", lambda *args: (None,) * 4)
    monkeypatch.setattr(tasks, "ApolloGateway", lambda *args: Gateway())
    monkeypatch.setattr(tasks, "enqueue_top_enrichment", rate_limited)
    monkeypatch.setattr(
        tasks,
        "_publish_enrichment_retry",
        lambda *args: published.append(args[-1]) or True,
    )
    monkeypatch.setattr(tasks, "_record_provider_outcome", lambda *args: None)

    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE enrichment_retry_dispatches SET not_before = :not_before "
                "WHERE tenant_id = :tenant_id AND run_id = :run_id"
            ),
            {
                "not_before": datetime.now(UTC) + timedelta(minutes=1),
                "tenant_id": retry.tenant_id,
                "run_id": retry.run_id,
            },
        )
    tasks.enrich_run.run(
        str(retry.run_id),
        str(retry.tenant_id),
        str(retry.requested_by_user_id),
        retry.candidate_limit,
        retry.generation,
    )
    assert provider_calls == []
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE enrichment_retry_dispatches SET not_before = :not_before "
                "WHERE tenant_id = :tenant_id AND run_id = :run_id"
            ),
            {
                "not_before": datetime.now(UTC) - timedelta(seconds=1),
                "tenant_id": retry.tenant_id,
                "run_id": retry.run_id,
            },
        )
    tasks.enrich_run.run(
        str(retry.run_id),
        str(retry.tenant_id),
        str(uuid4()),
        retry.candidate_limit,
        retry.generation,
    )
    tasks.enrich_run.run(
        str(retry.run_id),
        str(retry.tenant_id),
        str(retry.requested_by_user_id),
        retry.candidate_limit - 1,
        retry.generation,
    )
    assert provider_calls == []

    def deliver() -> None:
        try:
            tasks.enrich_run.run(
                str(retry.run_id),
                str(retry.tenant_id),
                str(retry.requested_by_user_id),
                retry.candidate_limit,
                retry.generation,
            )
            outcomes.put(None)
        except Exception as error:  # noqa: BLE001 - thread result is asserted
            outcomes.put(error)

    first = threading.Thread(target=deliver)
    first.start()
    assert provider_entered.wait(timeout=5)
    duplicate = threading.Thread(target=deliver)
    duplicate.start()
    duplicate.join(timeout=10)
    assert not duplicate.is_alive()
    release_provider.set()
    first.join(timeout=10)
    assert not first.is_alive()

    observed = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert not any(isinstance(item, Exception) for item in observed), observed
    assert provider_calls == [retry.run_id]
    assert len(published) == 1
    next_schedule = published[0]
    assert next_schedule.generation == retry.generation + 1
    assert next_schedule.task_id == (
        f"enrich-run-retry:{retry.run_id}:{retry.generation + 1}"
    )
    with Session(owner_engine) as session:
        stored = session.get(
            EnrichmentRetryDispatch,
            (retry.tenant_id, retry.run_id),
        )
        assert stored is not None
        assert stored.status == "pending"
        assert stored.generation == retry.generation + 1
        assert stored.claim_token is None

    tasks.enrich_run.run(
        str(retry.run_id),
        str(retry.tenant_id),
        str(retry.requested_by_user_id),
        retry.candidate_limit,
        retry.generation,
    )
    assert provider_calls == [retry.run_id]
    assert len(published) == 1
