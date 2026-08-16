from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.clients.models import ClientCompany
from app.core.config import Settings
from app.core.database import Base, get_db
from app.identity.models import Membership, Tenant, User
from app.identity.schemas import IdentityClaims, Role
from app.jobs.models import Job, ScorecardCriterionRecord, ScorecardVersion
from app.main import create_app
from app.sourcing.models import SourcingRun, TenantNotification


class StaticVerifier:
    def __init__(self, claims: IdentityClaims) -> None:
        self.claims = claims

    def verify(self, token: str) -> IdentityClaims:
        return self.claims


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID, UUID]] = []

    def __call__(self, run_id: UUID, tenant_id: UUID, user_id: UUID) -> None:
        self.calls.append((run_id, tenant_id, user_id))


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
    app = create_app(Settings.for_test(), sourcing_dispatcher=dispatcher)
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
    with Session(sourcing_api["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(SourcingRun)) == 1


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
