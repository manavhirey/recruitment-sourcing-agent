from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.audit.models import AuditEvent
from app.candidates.models import Candidate
from app.clients.models import ClientCompany
from app.core.config import Settings
from app.core.database import Base, get_db
from app.identity.models import Membership, Tenant, User
from app.identity.schemas import IdentityClaims, Role
from app.jobs.models import Job, ScorecardCriterionRecord, ScorecardVersion
from app.main import create_app
from app.sourcing.models import (
    EnrichmentRequest,
    RunCandidate,
    SourcingRun,
    TenantNotification,
    UsageBudget,
    UsageLedger,
)
from app.sourcing.state_machine import RunState


class StaticVerifier:
    def __init__(self, claims: IdentityClaims) -> None:
        self.claims = claims

    def verify(self, token: str) -> IdentityClaims:
        return self.claims


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID, UUID, str]] = []

    def __call__(
        self, run_id: UUID, tenant_id: UUID, user_id: UUID, dispatch_key: str
    ) -> None:
        self.calls.append((run_id, tenant_id, user_id, dispatch_key))


class RecordingEnrichmentDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID, UUID, str]] = []
        self.failures_remaining = 0

    def __call__(
        self, request_id: UUID, tenant_id: UUID, user_id: UUID, dispatch_key: str
    ) -> None:
        self.calls.append((request_id, tenant_id, user_id, dispatch_key))
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise ConnectionError("broker unavailable")


@pytest.fixture
def sourcing_api(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    tenant_id = uuid4()
    user_id = uuid4()
    other_tenant_id = uuid4()
    with Session(engine) as session:
        tenant = Tenant(id=tenant_id, slug=f"router-{tenant_id}")
        other_tenant = Tenant(id=other_tenant_id, slug=f"router-{other_tenant_id}")
        user = User(
            id=user_id,
            oidc_subject="oidc|sourcing-router-owner",
            email="owner@example.test",
            display_name="Owner",
        )
        client = ClientCompany(
            tenant_id=tenant_id,
            name="Client",
            normalized_name="client",
        )
        session.add_all((tenant, other_tenant, user, client))
        session.flush()
        session.add(Membership(tenant_id=tenant_id, user_id=user_id, role=Role.OWNER))
        job = Job(
            tenant_id=tenant_id,
            client_id=client.id,
            owner_user_id=user_id,
            title="Product Manager",
            job_description="Find a product manager.",
        )
        session.add(job)
        session.flush()
        scorecard = ScorecardVersion(
            tenant_id=tenant_id,
            job_id=job.id,
            version=1,
            target_titles=["Product Manager"],
            seniority=["manager"],
            minimum_years=None,
            maximum_years=None,
            locations=[],
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
        job.current_scorecard_id = scorecard.id
        session.commit()
        job_id = job.id

    dispatcher = RecordingDispatcher()
    enrichment_dispatcher = RecordingEnrichmentDispatcher()
    app = create_app(
        Settings.for_test(),
        sourcing_dispatcher=dispatcher,
        enrichment_dispatcher=enrichment_dispatcher,
    )
    app.state.token_verifier = StaticVerifier(
        IdentityClaims(
            subject="oidc|sourcing-router-owner",
            email="owner@example.test",
            name="Owner",
            email_verified=True,
        )
    )

    def database_session() -> Generator[Session, None, None]:
        with Session(engine, expire_on_commit=False) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    app.dependency_overrides[get_db] = database_session
    monkeypatch.setattr(
        "app.identity.dependencies.apply_tenant_context",
        lambda session, tenant_id: None,
    )
    with TestClient(app) as api:
        yield {
            "api": api,
            "engine": engine,
            "tenant_id": tenant_id,
            "other_tenant_id": other_tenant_id,
            "user_id": user_id,
            "job_id": job_id,
            "dispatcher": dispatcher,
            "enrichment_dispatcher": enrichment_dispatcher,
            "metrics": app.state.metrics,
        }
    engine.dispose()


def _headers(sourcing_api: dict[str, Any]) -> dict[str, str]:
    return {
        "Authorization": "Bearer signed-token",
        "X-Tenant-ID": str(sourcing_api["tenant_id"]),
    }


def test_start_status_activity_and_cancel_routes_are_idempotent(
    sourcing_api: dict[str, Any],
) -> None:
    api: TestClient = sourcing_api["api"]
    headers = _headers(sourcing_api)
    url = f"/api/v1/jobs/{sourcing_api['job_id']}/runs"

    missing_key = api.post(url, headers=headers, json={})
    assert missing_key.status_code == 400
    assert missing_key.json() == {"detail": {"code": "idempotency_key_required"}}

    created = api.post(
        url,
        headers={**headers, "Idempotency-Key": "start-api-run"},
        json={},
    )
    replay = api.post(
        url,
        headers={**headers, "Idempotency-Key": "start-api-run"},
        json={},
    )
    assert created.status_code == replay.status_code == 201
    assert created.json()["id"] == replay.json()["id"]
    assert len(sourcing_api["dispatcher"].calls) == 1
    assert created.json()["state"] == "queued"
    assert created.json()["budget_use"] == {
        "search_pages": 0,
        "enrichments": 0,
        "estimated_credits": 0,
    }
    run_id = created.json()["id"]

    status_response = api.get(f"/api/v1/runs/{run_id}", headers=headers)
    assert status_response.status_code == 200
    assert status_response.json()["current_stage"] == "queued"
    activity = api.get(f"/api/v1/runs/{run_id}/activity", headers=headers)
    assert activity.status_code == 200
    assert [event["action"] for event in activity.json()] == ["sourcing_run.started"]
    with Session(sourcing_api["engine"]) as session:
        session.add_all(
            (
                AuditEvent(
                    tenant_id=sourcing_api["tenant_id"],
                    run_id=UUID(run_id),
                    actor_user_id=sourcing_api["user_id"],
                    event_key="safe-run-action-private-payload",
                    action="sourcing_run.usage_budget_exhausted",
                    entity_type="sourcing_run",
                    entity_id=UUID(run_id),
                    payload={"provider_error": "private-error", "token": "secret"},
                ),
                AuditEvent(
                    tenant_id=sourcing_api["tenant_id"],
                    run_id=UUID(run_id),
                    actor_user_id=sourcing_api["user_id"],
                    event_key="private-run-action",
                    action="provider.raw_response_received",
                    entity_type="provider_snapshot",
                    entity_id=uuid4(),
                    payload={"body": "private-provider-body"},
                ),
            )
        )
        session.commit()
    sanitized_activity = api.get(f"/api/v1/runs/{run_id}/activity", headers=headers)
    assert [event["action"] for event in sanitized_activity.json()] == [
        "sourcing_run.started",
        "sourcing_run.usage_budget_exhausted",
    ]
    assert all(
        set(event) == {"id", "action", "summary", "created_at"}
        for event in sanitized_activity.json()
    )
    assert "private-error" not in sanitized_activity.text
    assert "private-provider-body" not in sanitized_activity.text

    cancel_missing_key = api.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)
    assert cancel_missing_key.status_code == 400
    cancelled = api.post(
        f"/api/v1/runs/{run_id}/cancel",
        headers={**headers, "Idempotency-Key": "cancel-api-run"},
    )
    cancel_replay = api.post(
        f"/api/v1/runs/{run_id}/cancel",
        headers={**headers, "Idempotency-Key": "cancel-api-run"},
    )
    assert cancelled.status_code == cancel_replay.status_code == 200
    assert cancelled.json()["state"] == "cancelled"

    duplicate_after_terminal = api.post(
        url,
        headers={**headers, "Idempotency-Key": "new-intent-same-scorecard"},
        json={},
    )
    assert duplicate_after_terminal.status_code == 409
    assert duplicate_after_terminal.json() == {
        "detail": {"code": "scorecard_run_exists"}
    }
    with Session(sourcing_api["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(SourcingRun)) == 1


def test_start_replay_recovers_a_committed_run_after_dispatch_failure(
    sourcing_api: dict[str, Any],
) -> None:
    api: TestClient = sourcing_api["api"]
    attempts: list[str] = []

    def fail_once(_run_id: UUID, _tenant_id: UUID, _user_id: UUID, key: str) -> None:
        attempts.append(key)
        if len(attempts) == 1:
            raise RuntimeError("queue unavailable")

    api.app.state.sourcing_dispatcher = fail_once
    url = f"/api/v1/jobs/{sourcing_api['job_id']}/runs"
    headers = {**_headers(sourcing_api), "Idempotency-Key": "recover-dispatch"}

    with pytest.raises(RuntimeError, match="queue unavailable"):
        api.post(url, headers=headers, json={})
    replay = api.post(url, headers=headers, json={})

    assert replay.status_code == 201
    assert attempts == [
        f"sourcing-plan-{replay.json()['id']}",
        f"sourcing-plan-{replay.json()['id']}",
    ]
    with Session(sourcing_api["engine"]) as session:
        run = session.scalar(select(SourcingRun))
        assert run is not None
        assert run.dispatch_pending is False


def test_start_with_a_new_key_recovers_pending_dispatch_after_reload(
    sourcing_api: dict[str, Any],
) -> None:
    api: TestClient = sourcing_api["api"]
    attempts: list[str] = []

    def fail_once(_run_id: UUID, _tenant_id: UUID, _user_id: UUID, key: str) -> None:
        attempts.append(key)
        if len(attempts) == 1:
            raise RuntimeError("queue unavailable")

    api.app.state.sourcing_dispatcher = fail_once
    url = f"/api/v1/jobs/{sourcing_api['job_id']}/runs"

    with pytest.raises(RuntimeError, match="queue unavailable"):
        api.post(
            url,
            headers={**_headers(sourcing_api), "Idempotency-Key": "lost-browser"},
            json={},
        )
    recovered = api.post(
        url,
        headers={**_headers(sourcing_api), "Idempotency-Key": "reloaded-browser"},
        json={},
    )

    assert recovered.status_code == 201
    assert attempts == [
        f"sourcing-plan-{recovered.json()['id']}",
        f"sourcing-plan-{recovered.json()['id']}",
    ]
    with Session(sourcing_api["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(SourcingRun)) == 1
        run = session.scalar(select(SourcingRun))
        assert run is not None and run.dispatch_pending is False


def test_run_lookup_does_not_disclose_another_tenant(
    sourcing_api: dict[str, Any],
) -> None:
    api: TestClient = sourcing_api["api"]
    created = api.post(
        f"/api/v1/jobs/{sourcing_api['job_id']}/runs",
        headers={**_headers(sourcing_api), "Idempotency-Key": "tenant-hidden-run"},
        json={},
    )
    hidden = api.get(
        f"/api/v1/runs/{created.json()['id']}",
        headers={
            "Authorization": "Bearer signed-token",
            "X-Tenant-ID": str(sourcing_api["other_tenant_id"]),
        },
    )
    assert hidden.status_code == 404
    assert hidden.json() == {"detail": {"code": "tenant_not_found"}}


def test_latest_job_run_is_deterministic_and_reports_review_counts(
    sourcing_api: dict[str, Any],
) -> None:
    with Session(sourcing_api["engine"], expire_on_commit=False) as session:
        job = session.get(Job, sourcing_api["job_id"])
        assert job is not None and job.current_scorecard_id is not None
        older = SourcingRun(
            tenant_id=sourcing_api["tenant_id"],
            job_id=job.id,
            scorecard_version_id=job.current_scorecard_id,
            started_by_user_id=sourcing_api["user_id"],
            state=RunState.CANCELLED,
            current_stage=RunState.CANCELLED.value,
            candidate_count=4,
            matched_count=3,
            created_at=datetime(2026, 8, 15, tzinfo=UTC),
        )
        latest = SourcingRun(
            tenant_id=sourcing_api["tenant_id"],
            job_id=job.id,
            scorecard_version_id=job.current_scorecard_id,
            started_by_user_id=sourcing_api["user_id"],
            state=RunState.PARTIALLY_READY,
            current_stage=RunState.ENRICHING.value,
            candidate_count=3,
            matched_count=3,
            error_code="usage_budget_exhausted",
            error_message="raw text must not define the public message",
            created_at=datetime(2026, 8, 16, tzinfo=UTC),
        )
        session.add_all((older, latest))
        session.flush()
        for position, enrichment_status in enumerate(
            ("available", "failed", "unavailable")
        ):
            candidate = Candidate(
                tenant_id=sourcing_api["tenant_id"],
                full_name=f"Candidate {position}",
                normalized_name=f"candidate {position}",
            )
            session.add(candidate)
            session.flush()
            session.add(
                RunCandidate(
                    tenant_id=sourcing_api["tenant_id"],
                    run_id=latest.id,
                    candidate_id=candidate.id,
                    scorecard_version_id=job.current_scorecard_id,
                    enrichment_status=enrichment_status,
                )
            )
        session.commit()
        latest_id = latest.id

    response = sourcing_api["api"].get(
        f"/api/v1/jobs/{sourcing_api['job_id']}/runs/latest",
        headers=_headers(sourcing_api),
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(latest_id)
    assert response.json()["enriched_count"] == 1
    assert response.json()["failed_count"] == 1
    assert response.json()["error_message"] == (
        "The configured sourcing usage budget was exhausted."
    )


def test_latest_job_run_collapses_missing_job_and_missing_run(
    sourcing_api: dict[str, Any],
) -> None:
    headers = _headers(sourcing_api)
    no_run = sourcing_api["api"].get(
        f"/api/v1/jobs/{sourcing_api['job_id']}/runs/latest", headers=headers
    )
    missing = sourcing_api["api"].get(
        f"/api/v1/jobs/{uuid4()}/runs/latest", headers=headers
    )

    assert no_run.status_code == missing.status_code == 404
    assert no_run.json() == missing.json() == {"detail": {"code": "run_not_found"}}


def test_notifications_are_role_scoped_and_acknowledgement_is_idempotent(
    sourcing_api: dict[str, Any],
) -> None:
    with Session(sourcing_api["engine"]) as session:
        session.add_all(
            (
                TenantNotification(
                    tenant_id=sourcing_api["tenant_id"],
                    audience_role=Role.OWNER.value,
                    code="usage_budget_exhausted",
                    title="Owner alert",
                    message="Owner message",
                ),
                TenantNotification(
                    tenant_id=sourcing_api["tenant_id"],
                    audience_role=Role.ADMIN.value,
                    code="usage_budget_exhausted",
                    title="Admin alert",
                    message="Admin message",
                ),
            )
        )
        session.commit()

    api: TestClient = sourcing_api["api"]
    headers = _headers(sourcing_api)
    listed = api.get("/api/v1/notifications", headers=headers)
    assert listed.status_code == 200
    assert [item["title"] for item in listed.json()] == ["Owner alert"]
    notification_id = listed.json()[0]["id"]

    missing_key = api.patch(f"/api/v1/notifications/{notification_id}", headers=headers)
    assert missing_key.status_code == 400
    acknowledged = api.patch(
        f"/api/v1/notifications/{notification_id}",
        headers={**headers, "Idempotency-Key": "ack-owner-alert"},
    )
    replay = api.patch(
        f"/api/v1/notifications/{notification_id}",
        headers={**headers, "Idempotency-Key": "ack-owner-alert"},
    )
    assert acknowledged.status_code == replay.status_code == 200
    assert acknowledged.json()["acknowledged_at"] is not None
    assert replay.json()["acknowledged_at"] == acknowledged.json()["acknowledged_at"]


def test_on_demand_enrichment_is_authorized_budgeted_and_idempotent(
    sourcing_api: dict[str, Any],
) -> None:
    api: TestClient = sourcing_api["api"]
    headers = _headers(sourcing_api)
    created = api.post(
        f"/api/v1/jobs/{sourcing_api['job_id']}/runs",
        headers={**headers, "Idempotency-Key": "on-demand-run"},
        json={},
    )
    run_id = UUID(created.json()["id"])
    with Session(sourcing_api["engine"]) as session:
        run = session.get(SourcingRun, run_id)
        assert run is not None
        run.state = RunState.READY
        run.current_stage = RunState.READY.value
        for position in range(50):
            ranked = Candidate(
                tenant_id=sourcing_api["tenant_id"],
                full_name=f"Ranked Candidate {position}",
                normalized_name=f"ranked candidate {position}",
            )
            session.add(ranked)
            session.flush()
            session.add(
                RunCandidate(
                    tenant_id=sourcing_api["tenant_id"],
                    run_id=run.id,
                    candidate_id=ranked.id,
                    scorecard_version_id=run.scorecard_version_id,
                    match_score=100 - position,
                    classification="main",
                )
            )
        candidate = Candidate(
            tenant_id=sourcing_api["tenant_id"],
            full_name="Priya Sharma",
            normalized_name="priya sharma",
        )
        session.add(candidate)
        session.flush()
        run_candidate = RunCandidate(
            tenant_id=sourcing_api["tenant_id"],
            run_id=run.id,
            candidate_id=candidate.id,
            scorecard_version_id=run.scorecard_version_id,
            match_score=1,
            classification="main",
        )
        session.add(run_candidate)
        session.commit()
        run_candidate_id = run_candidate.id

    url = f"/api/v1/job-candidates/{run_candidate_id}/enrich"
    first = api.post(
        url,
        headers={**headers, "Idempotency-Key": "enrich-priya"},
    )
    replay = api.post(
        url,
        headers={**headers, "Idempotency-Key": "enrich-priya"},
    )

    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert len(sourcing_api["enrichment_dispatcher"].calls) == 1
    with Session(sourcing_api["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(EnrichmentRequest)) == 1
        assert session.scalar(select(func.count()).select_from(UsageLedger)) == 2


def test_on_demand_budget_exhaustion_is_committed_and_counted(
    sourcing_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    api: TestClient = sourcing_api["api"]
    headers = _headers(sourcing_api)
    created = api.post(
        f"/api/v1/jobs/{sourcing_api['job_id']}/runs",
        headers={**headers, "Idempotency-Key": "budget-exhausted-run"},
        json={},
    )
    run_id = UUID(created.json()["id"])
    with Session(sourcing_api["engine"]) as session:
        run = session.get(SourcingRun, run_id)
        assert run is not None
        run.state = RunState.READY
        run.current_stage = RunState.READY.value
        candidate = Candidate(
            tenant_id=sourcing_api["tenant_id"],
            full_name="Budget Exhausted Candidate",
            normalized_name="budget exhausted candidate",
        )
        session.add(candidate)
        session.flush()
        row = RunCandidate(
            tenant_id=sourcing_api["tenant_id"],
            run_id=run.id,
            candidate_id=candidate.id,
            scorecard_version_id=run.scorecard_version_id,
            match_score=1,
            classification="main",
            enrichment_status="failed",
        )
        session.add_all(
            (
                row,
                UsageBudget(
                    tenant_id=sourcing_api["tenant_id"],
                    max_search_pages=None,
                    max_enrichments=0,
                    max_estimated_credits=None,
                ),
            )
        )
        session.commit()
        run_candidate_id = row.id

    monkeypatch.setattr(
        "app.sourcing.service.SourcingService._has_usage_capacity",
        lambda *args, **kwargs: True,
    )
    response = api.post(
        f"/api/v1/job-candidates/{run_candidate_id}/enrich",
        headers={**headers, "Idempotency-Key": "budget-exhausted-enrichment"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "usage_budget_exhausted"}}
    assert sourcing_api["metrics"].budget_exhaustion._value.get() == 1
    assert sourcing_api["enrichment_dispatcher"].calls == []
    with Session(sourcing_api["engine"]) as session:
        run = session.get(SourcingRun, run_id)
        assert run is not None and run.error_code == "usage_budget_exhausted"
        assert (
            session.scalar(
                select(func.count())
                .select_from(TenantNotification)
                .where(TenantNotification.run_id == run_id)
            )
            == 2
        )


def test_on_demand_enrichment_retries_pending_dispatch_with_the_same_key(
    sourcing_api: dict[str, Any],
) -> None:
    api: TestClient = sourcing_api["api"]
    headers = _headers(sourcing_api)
    created = api.post(
        f"/api/v1/jobs/{sourcing_api['job_id']}/runs",
        headers={**headers, "Idempotency-Key": "dispatch-retry-run"},
        json={},
    )
    run_id = UUID(created.json()["id"])
    with Session(sourcing_api["engine"]) as session:
        run = session.get(SourcingRun, run_id)
        assert run is not None
        run.state = RunState.READY
        run.current_stage = RunState.READY.value
        candidate = Candidate(
            tenant_id=sourcing_api["tenant_id"],
            full_name="Dispatch Retry Candidate",
            normalized_name="dispatch retry candidate",
        )
        session.add(candidate)
        session.flush()
        row = RunCandidate(
            tenant_id=sourcing_api["tenant_id"],
            run_id=run.id,
            candidate_id=candidate.id,
            scorecard_version_id=run.scorecard_version_id,
            match_score=1,
            classification="main",
            enrichment_status="failed",
        )
        session.add(row)
        session.commit()
        run_candidate_id = row.id

    dispatcher: RecordingEnrichmentDispatcher = sourcing_api["enrichment_dispatcher"]
    dispatcher.failures_remaining = 1
    url = f"/api/v1/job-candidates/{run_candidate_id}/enrich"
    with pytest.raises(ConnectionError, match="broker unavailable"):
        api.post(
            url,
            headers={**headers, "Idempotency-Key": "enrich-dispatch-retry"},
        )

    retried = api.post(
        url,
        headers={**headers, "Idempotency-Key": "enrich-dispatch-retry"},
    )

    assert retried.status_code == 202
    assert len(dispatcher.calls) == 2
    assert dispatcher.calls[0] == dispatcher.calls[1]
    with Session(sourcing_api["engine"]) as session:
        request = session.scalar(select(EnrichmentRequest))
        assert request is not None
        assert request.dispatch_pending is False


def test_on_demand_enrichment_new_key_binds_the_pending_request(
    sourcing_api: dict[str, Any],
) -> None:
    api: TestClient = sourcing_api["api"]
    headers = _headers(sourcing_api)
    created = api.post(
        f"/api/v1/jobs/{sourcing_api['job_id']}/runs",
        headers={**headers, "Idempotency-Key": "new-key-retry-run"},
        json={},
    )
    run_id = UUID(created.json()["id"])
    with Session(sourcing_api["engine"]) as session:
        run = session.get(SourcingRun, run_id)
        assert run is not None
        run.state = RunState.READY
        run.current_stage = RunState.READY.value
        candidate = Candidate(
            tenant_id=sourcing_api["tenant_id"],
            full_name="New Key Retry Candidate",
            normalized_name="new key retry candidate",
        )
        session.add(candidate)
        session.flush()
        row = RunCandidate(
            tenant_id=sourcing_api["tenant_id"],
            run_id=run.id,
            candidate_id=candidate.id,
            scorecard_version_id=run.scorecard_version_id,
            match_score=1,
            classification="main",
            enrichment_status="failed",
        )
        session.add(row)
        session.commit()
        run_candidate_id = row.id

    dispatcher: RecordingEnrichmentDispatcher = sourcing_api["enrichment_dispatcher"]
    dispatcher.failures_remaining = 1
    url = f"/api/v1/job-candidates/{run_candidate_id}/enrich"
    with pytest.raises(ConnectionError):
        api.post(url, headers={**headers, "Idempotency-Key": "first-intent"})

    retried = api.post(
        url,
        headers={**headers, "Idempotency-Key": "replacement-intent"},
    )

    assert retried.status_code == 202
    assert len(dispatcher.calls) == 2
    assert dispatcher.calls[0] == dispatcher.calls[1]
    with Session(sourcing_api["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(EnrichmentRequest)) == 1
        assert session.scalar(select(func.count()).select_from(UsageLedger)) == 2


def test_on_demand_enrichment_rechecks_server_rank_run_state_and_status(
    sourcing_api: dict[str, Any],
) -> None:
    api: TestClient = sourcing_api["api"]
    headers = _headers(sourcing_api)
    created = api.post(
        f"/api/v1/jobs/{sourcing_api['job_id']}/runs",
        headers={**headers, "Idempotency-Key": "ineligible-run"},
        json={},
    )
    run_id = UUID(created.json()["id"])
    with Session(sourcing_api["engine"]) as session:
        run = session.get(SourcingRun, run_id)
        assert run is not None
        run.state = RunState.READY
        run.current_stage = RunState.READY.value
        candidate = Candidate(
            tenant_id=sourcing_api["tenant_id"],
            full_name="Automatic Top Candidate",
            normalized_name="automatic top candidate",
        )
        session.add(candidate)
        session.flush()
        row = RunCandidate(
            tenant_id=sourcing_api["tenant_id"],
            run_id=run.id,
            candidate_id=candidate.id,
            scorecard_version_id=run.scorecard_version_id,
            match_score=99,
            classification="main",
            enrichment_status="not_requested",
        )
        session.add(row)
        session.commit()
        run_candidate_id = row.id

    response = api.post(
        f"/api/v1/job-candidates/{run_candidate_id}/enrich",
        headers={**headers, "Idempotency-Key": "ineligible-top-50"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "enrichment_not_eligible"}}
    assert sourcing_api["enrichment_dispatcher"].calls == []
