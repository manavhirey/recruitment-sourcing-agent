import base64
import logging
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.candidates.contacts import ContactCipher
from app.candidates.models import Candidate, ContactPoint, SourceIdentity
from app.clients.models import ClientCompany
from app.core.config import Settings
from app.core.database import Base, get_db
from app.identity.models import Tenant, User
from app.jobs.models import Job, ScorecardVersion
from app.main import create_app
from app.providers.snapshots import SnapshotStore
from app.sourcing.models import (
    EnrichmentRequest,
    RunCandidate,
    SourcingRun,
    WebhookDelivery,
)
from app.sourcing.state_machine import RunState
from app.sourcing.webhooks import CapabilityTokenCodec, WebhookRateLimiter


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, **kwargs: object) -> None:
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = bytes(
            kwargs["Body"]
        )  # type: ignore[arg-type]

    def delete_object(self, **kwargs: object) -> None:
        self.objects.pop((str(kwargs["Bucket"]), str(kwargs["Key"])), None)

    def head_object(self, **kwargs: object) -> dict[str, object]:
        if (str(kwargs["Bucket"]), str(kwargs["Key"])) not in self.objects:
            raise KeyError
        return {}

    def put_bucket_lifecycle_configuration(self, **kwargs: object) -> None:
        return None


@pytest.fixture
def webhook_scenario() -> Generator[dict[str, Any], None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    settings = Settings.for_test()
    codec = CapabilityTokenCodec(settings.webhook_hmac_key.get_secret_value().encode())
    with Session(engine, expire_on_commit=False) as session:
        tenant = Tenant(slug=f"webhook-{uuid4()}")
        user = User(
            oidc_subject=f"oidc|{uuid4()}",
            email="owner@example.test",
            display_name="Owner",
        )
        session.add_all((tenant, user))
        session.flush()
        client = ClientCompany(
            tenant_id=tenant.id, name="Client", normalized_name="client"
        )
        session.add(client)
        session.flush()
        job = Job(
            tenant_id=tenant.id,
            client_id=client.id,
            owner_user_id=user.id,
            title="Product Manager",
            job_description="Job",
        )
        session.add(job)
        session.flush()
        scorecard = ScorecardVersion(
            tenant_id=tenant.id,
            job_id=job.id,
            version=1,
            target_titles=[],
            seniority=[],
            locations=[],
            industry_code="technology",
            suggested_adjacent_industries=[],
            uncertainties=[],
            extraction_status="ready",
            confirmed_by_user_id=user.id,
            confirmed_at=datetime.now(UTC),
        )
        session.add(scorecard)
        session.flush()
        job.current_scorecard_id = scorecard.id
        candidate = Candidate(
            tenant_id=tenant.id,
            full_name="Priya Sharma",
            normalized_name="priya sharma",
        )
        session.add(candidate)
        session.flush()
        session.add(
            SourceIdentity(
                tenant_id=tenant.id,
                candidate_id=candidate.id,
                provider="apollo",
                provider_person_id="person-1",
                source_timestamp=datetime.now(UTC),
                confidence=1,
            )
        )
        run = SourcingRun(
            tenant_id=tenant.id,
            job_id=job.id,
            scorecard_version_id=scorecard.id,
            started_by_user_id=user.id,
            state=RunState.ENRICHING,
            current_stage=RunState.ENRICHING.value,
        )
        session.add(run)
        session.flush()
        run_candidate = RunCandidate(
            tenant_id=tenant.id,
            run_id=run.id,
            candidate_id=candidate.id,
            scorecard_version_id=scorecard.id,
            match_score=95,
            classification="main",
            enrichment_status="pending",
        )
        session.add(run_candidate)
        session.flush()
        request = EnrichmentRequest(
            tenant_id=tenant.id,
            run_id=run.id,
            provider="apollo",
            provider_request_id="123",
            candidate_ids=[str(candidate.id)],
            status="pending",
            reservation_key="enrich:1",
        )
        session.add(request)
        session.flush()
        token = codec.issue(request.id, tenant.id)
        assert codec.tenant_id(token) == tenant.id
        assert codec.request_id(token) == request.id
        secret = token.rsplit(".", 1)[1]
        assert len(base64.urlsafe_b64decode(secret + "=")) == 32
        request.capability_token_hmac = codec.digest(token, tenant.id)
        session.commit()
        request_id = request.id
        candidate_id = candidate.id
        run_id = run.id

    def database_override() -> Generator[Session, None, None]:
        with Session(engine) as session:
            yield session
            session.commit()

    objects = MemoryObjectStore()
    snapshot_store = SnapshotStore(
        objects,
        "snapshots",
        base64.b64encode(b"s" * 32).decode(),
    )
    contact_cipher = ContactCipher(
        settings.contact_encryption_key.get_secret_value(),
        settings.suppression_hmac_key.get_secret_value().encode(),
    )
    app = create_app(
        settings,
        sourcing_dispatcher=lambda *_: None,
        snapshot_store=snapshot_store,
        contact_cipher=contact_cipher,
    )
    app.dependency_overrides[get_db] = database_override
    with TestClient(app) as api:
        yield {
            "api": api,
            "engine": engine,
            "token": token,
            "candidate_id": candidate_id,
            "request_id": request_id,
            "run_id": run_id,
            "objects": objects,
        }
    engine.dispose()


@pytest.fixture
def apollo_phone_payload() -> dict[str, object]:
    return {
        "request_id": 123,
        "people": [
            {
                "id": "person-1",
                "name": "Priya Sharma",
                "phone_numbers": [
                    {
                        "raw_number": "+1 212 555 0112",
                        "type": "mobile",
                        "status": "verified",
                    }
                ],
            }
        ],
    }


def test_duplicate_webhook_is_applied_once(
    webhook_scenario: dict[str, Any], apollo_phone_payload: dict[str, object]
) -> None:
    scenario = webhook_scenario
    path = f"/webhooks/apollo/{scenario['token']}"

    first = scenario["api"].post(path, json=apollo_phone_payload)
    second = scenario["api"].post(path, json=apollo_phone_payload)

    assert first.status_code == 202
    assert second.status_code == 202
    with Session(scenario["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 1
        assert session.scalar(select(func.count()).select_from(WebhookDelivery)) == 1
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None and run.state is RunState.READY
        point = session.scalar(select(ContactPoint))
        assert point is not None and b"2125550112" not in point.value_ciphertext
    assert all(
        b"2125550112" not in body for body in scenario["objects"].objects.values()
    )


def test_webhook_rejects_request_id_mismatch_without_writing_contact(
    webhook_scenario: dict[str, Any], apollo_phone_payload: dict[str, object]
) -> None:
    apollo_phone_payload["request_id"] = 999

    response = webhook_scenario["api"].post(
        f"/webhooks/apollo/{webhook_scenario['token']}",
        json=apollo_phone_payload,
    )

    assert response.status_code == 400
    with Session(webhook_scenario["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 0


def test_contact_and_capability_token_never_reach_logs(
    webhook_scenario: dict[str, Any],
    apollo_phone_payload: dict[str, object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = webhook_scenario["api"].post(
            f"/webhooks/apollo/{webhook_scenario['token']}",
            json=apollo_phone_payload,
        )

    assert response.status_code == 202
    serialized = caplog.text
    assert "+1 212 555 0112" not in serialized
    assert webhook_scenario["token"] not in serialized


def test_webhook_rate_limit_is_scoped_by_source_and_window() -> None:
    limiter = WebhookRateLimiter(limit=1, window_seconds=60)

    assert limiter.allow("192.0.2.10", now=100)
    assert not limiter.allow("192.0.2.10", now=101)
    assert limiter.allow("192.0.2.11", now=101)
    assert limiter.allow("192.0.2.10", now=161)
