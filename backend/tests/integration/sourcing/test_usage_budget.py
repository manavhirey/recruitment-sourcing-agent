from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.clients.models import ClientCompany
from app.core.database import Base
from app.identity.models import Tenant, User
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardVersion
from app.sourcing.models import (
    SourcingRun,
    TenantNotification,
    UsageBudget,
    UsageLedger,
)
from app.sourcing.service import SourcingError, SourcingService
from app.sourcing.state_machine import RunState


@pytest.fixture
def budget_scenario() -> Generator[dict[str, Any], None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        tenant = Tenant(id=uuid4(), slug=f"budget-{uuid4()}")
        other_tenant = Tenant(id=uuid4(), slug=f"budget-other-{uuid4()}")
        user = User(
            id=uuid4(),
            oidc_subject=f"oidc|{uuid4()}",
            email="owner@example.test",
            display_name="Owner",
        )
        other_user = User(
            id=uuid4(),
            oidc_subject=f"oidc|{uuid4()}",
            email="other@example.test",
            display_name="Other Owner",
        )
        session.add_all((tenant, other_tenant, user, other_user))
        session.flush()
        runs: list[SourcingRun] = []
        for index, (owner_tenant, owner_user) in enumerate(
            ((tenant, user), (tenant, user), (other_tenant, other_user))
        ):
            client = ClientCompany(
                tenant_id=owner_tenant.id,
                name=f"Client {index}",
                normalized_name=f"client {index}",
            )
            session.add(client)
            session.flush()
            job = Job(
                tenant_id=owner_tenant.id,
                client_id=client.id,
                owner_user_id=owner_user.id,
                title=f"Job {index}",
                job_description="Job",
            )
            session.add(job)
            session.flush()
            scorecard = ScorecardVersion(
                tenant_id=owner_tenant.id,
                job_id=job.id,
                version=1,
                target_titles=["Product Manager"],
                seniority=[],
                minimum_years=None,
                maximum_years=None,
                locations=[],
                industry_code="technology.fintech",
                suggested_adjacent_industries=[],
                uncertainties=[],
                extraction_status="ready",
                confirmed_by_user_id=owner_user.id,
                confirmed_at=datetime(2026, 8, 16, tzinfo=UTC),
            )
            session.add(scorecard)
            session.flush()
            job.current_scorecard_id = scorecard.id
            run = SourcingRun(
                tenant_id=owner_tenant.id,
                job_id=job.id,
                scorecard_version_id=scorecard.id,
                started_by_user_id=owner_user.id,
                state=RunState.SOURCING,
                current_stage=RunState.SOURCING.value,
            )
            session.add(run)
            runs.append(run)
        session.flush()
        session.add_all(
            (
                UsageBudget(
                    tenant_id=tenant.id,
                    max_search_pages=1,
                    max_enrichments=10,
                    max_estimated_credits=1,
                ),
                UsageBudget(
                    tenant_id=other_tenant.id,
                    max_search_pages=1,
                    max_enrichments=10,
                    max_estimated_credits=1,
                ),
            )
        )
        session.commit()
        yield {
            "session": session,
            "tenant": tenant,
            "other_tenant": other_tenant,
            "user": user,
            "other_user": other_user,
            "runs": runs,
        }
    engine.dispose()


def _context(tenant_id, user_id) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, user_id=user_id, role=Role.OWNER)


def test_reservation_reconciliation_and_tenant_budget_isolation(
    budget_scenario: dict[str, Any],
) -> None:
    scenario = budget_scenario
    session: Session = scenario["session"]
    service = SourcingService(session, b"test-suppression-key")
    first_run, second_run, other_run = scenario["runs"]
    context = _context(scenario["tenant"].id, scenario["user"].id)

    reservations = service.reserve_usage(
        context,
        first_run.id,
        provider="apollo",
        endpoint="people_search",
        reservation_key="query-1-page-1",
        requested_units={"search_pages": 1, "estimated_credits": 1},
    )
    replay = service.reserve_usage(
        context,
        first_run.id,
        provider="apollo",
        endpoint="people_search",
        reservation_key="query-1-page-1",
        requested_units={"search_pages": 1, "estimated_credits": 1},
    )
    with pytest.raises(SourcingError, match="usage_reservation_conflict"):
        service.reserve_usage(
            context,
            first_run.id,
            provider="apollo",
            endpoint="different_endpoint",
            reservation_key="query-1-page-1",
            requested_units={"search_pages": 1, "estimated_credits": 1},
        )
    service.reconcile_usage(
        context,
        first_run.id,
        reservation_key="query-1-page-1",
        charged_units={"search_pages": 1, "estimated_credits": 1},
        provider_request_id="provider-request-1",
    )
    with pytest.raises(SourcingError, match="usage_reconciliation_conflict"):
        service.reconcile_usage(
            context,
            first_run.id,
            reservation_key="query-1-page-1",
            charged_units={"search_pages": 1, "estimated_credits": 1},
            provider_request_id="different-provider-request",
        )

    assert {row.id for row in reservations} == {row.id for row in replay}
    assert all(row.charged_units == 1 for row in reservations)
    assert all(row.provider_request_id == "provider-request-1" for row in reservations)

    with pytest.raises(SourcingError, match="usage_budget_exhausted"):
        service.reserve_usage(
            context,
            second_run.id,
            provider="apollo",
            endpoint="people_search",
            reservation_key="query-2-page-1",
            requested_units={"search_pages": 1, "estimated_credits": 1},
        )
    assert second_run.state is RunState.PARTIALLY_READY
    assert second_run.error_code == "usage_budget_exhausted"
    assert (
        session.scalar(
            select(func.count())
            .select_from(TenantNotification)
            .where(TenantNotification.run_id == second_run.id)
        )
        == 2
    )

    other_context = _context(scenario["other_tenant"].id, scenario["other_user"].id)
    other = service.reserve_usage(
        other_context,
        other_run.id,
        provider="apollo",
        endpoint="people_search",
        reservation_key="query-other-page-1",
        requested_units={"search_pages": 1, "estimated_credits": 1},
    )
    assert len(other) == 2
    assert session.scalar(select(func.count()).select_from(UsageLedger)) == 4
