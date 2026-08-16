import os
import queue
import threading
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
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
from app.sourcing.dispatch_recovery import (
    recover_pending_dispatches,
    recover_pending_enrichment_dispatches,
)
from app.sourcing.models import EnrichmentRequest, RunCandidate, SourcingRun
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
            text(
                "ALTER TABLE audit_events DISABLE TRIGGER "
                "audit_events_append_only"
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
            text(
                "ALTER TABLE audit_events ENABLE TRIGGER "
                "audit_events_append_only"
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

    with owner_engine.begin() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0012_enrich_dispatch_recovery"
        )
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []


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
