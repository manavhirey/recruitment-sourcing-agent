from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from app.audit.models import AuditEvent
from app.clients.models import ClientCompany
from app.core.database import Base
from app.identity.models import IdentityIdempotencyKey, Tenant, User
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardCriterionRecord, ScorecardVersion
from app.sourcing.models import SourcingRun
from app.sourcing.service import SourcingError, SourcingService
from app.sourcing.state_machine import RunState
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@pytest.fixture
def service_scenario() -> Generator[dict[str, Any], None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        tenant = Tenant(id=uuid4(), slug=f"service-{uuid4()}")
        user = User(
            id=uuid4(),
            oidc_subject=f"oidc|{uuid4()}",
            email="owner@example.test",
            display_name="Owner",
        )
        client = ClientCompany(
            tenant_id=tenant.id,
            name="Client",
            normalized_name="client",
        )
        session.add_all((tenant, user, client))
        session.flush()
        confirmed_job = Job(
            tenant_id=tenant.id,
            client_id=client.id,
            owner_user_id=user.id,
            title="Confirmed",
            job_description="Confirmed job",
        )
        unconfirmed_job = Job(
            tenant_id=tenant.id,
            client_id=client.id,
            owner_user_id=user.id,
            title="Unconfirmed",
            job_description="Unconfirmed job",
        )
        session.add_all((confirmed_job, unconfirmed_job))
        session.flush()
        scorecard = ScorecardVersion(
            tenant_id=tenant.id,
            job_id=confirmed_job.id,
            version=1,
            target_titles=["Product Manager"],
            seniority=["mid_level"],
            minimum_years=None,
            maximum_years=None,
            locations=[],
            industry_code="technology.fintech",
            suggested_adjacent_industries=[],
            uncertainties=[],
            extraction_status="ready",
            confirmed_by_user_id=user.id,
            confirmed_at=datetime(2026, 8, 16, tzinfo=UTC),
        )
        session.add(scorecard)
        session.flush()
        session.add(
            ScorecardCriterionRecord(
                tenant_id=tenant.id,
                scorecard_version_id=scorecard.id,
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
        confirmed_job.current_scorecard_id = scorecard.id
        session.commit()
        context = RequestContext(
            tenant_id=tenant.id,
            user_id=user.id,
            role=Role.OWNER,
        )
        yield {
            "session": session,
            "context": context,
            "confirmed_job": confirmed_job,
            "unconfirmed_job": unconfirmed_job,
        }
    engine.dispose()


def test_start_requires_a_confirmed_immutable_scorecard(
    service_scenario: dict[str, Any],
) -> None:
    scenario = service_scenario
    service = SourcingService(scenario["session"], b"test-suppression-key")

    with pytest.raises(SourcingError, match="scorecard_required"):
        service.start(
            scenario["context"],
            scenario["unconfirmed_job"].id,
            idempotency_key="start-unconfirmed",
        )


def test_unknown_historical_seniority_requires_revision_before_run(
    service_scenario: dict[str, Any],
) -> None:
    scenario = service_scenario
    scorecard = scenario["session"].scalar(
        select(ScorecardVersion).where(
            ScorecardVersion.id == scenario["confirmed_job"].current_scorecard_id
        )
    )
    assert scorecard is not None
    scorecard.seniority = ["manager"]
    scenario["session"].flush()
    service = SourcingService(scenario["session"], b"test-suppression-key")

    with pytest.raises(SourcingError, match="scorecard_seniority_revision_required"):
        service.start_with_outcome(
            scenario["context"],
            scenario["confirmed_job"].id,
            idempotency_key="legacy-run",
        )

    assert (
        scenario["session"].scalar(select(func.count()).select_from(SourcingRun)) == 0
    )
    assert (
        scenario["session"].scalar(
            select(func.count()).select_from(IdentityIdempotencyKey)
        )
        == 0
    )


def test_start_is_idempotent_and_binds_a_new_key_to_a_pending_run(
    service_scenario: dict[str, Any],
) -> None:
    scenario = service_scenario
    service = SourcingService(scenario["session"], b"test-suppression-key")

    first = service.start(
        scenario["context"],
        scenario["confirmed_job"].id,
        idempotency_key="start-confirmed",
    )
    replay = service.start(
        scenario["context"],
        scenario["confirmed_job"].id,
        idempotency_key="start-confirmed",
    )

    assert first.id == replay.id
    assert first.state is RunState.QUEUED
    assert (
        scenario["session"].scalar(select(func.count()).select_from(SourcingRun)) == 1
    )
    assert scenario["session"].scalar(select(func.count()).select_from(AuditEvent)) == 1

    recovered = service.start(
        scenario["context"],
        scenario["confirmed_job"].id,
        idempotency_key="different-request",
    )
    assert recovered.id == first.id


def test_cancel_is_replay_safe_and_terminal(
    service_scenario: dict[str, Any],
) -> None:
    scenario = service_scenario
    service = SourcingService(scenario["session"], b"test-suppression-key")
    run = service.start(
        scenario["context"],
        scenario["confirmed_job"].id,
        idempotency_key="start-before-cancel",
    )

    first = service.cancel(scenario["context"], run.id, idempotency_key="cancel-run")
    replay = service.cancel(scenario["context"], run.id, idempotency_key="cancel-run")

    assert first.id == replay.id
    assert replay.state is RunState.CANCELLED
    assert replay.cancellation_requested is True
    assert replay.completed_at is not None
    assert (
        scenario["session"].scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "sourcing_run.cancelled")
        )
        == 1
    )
