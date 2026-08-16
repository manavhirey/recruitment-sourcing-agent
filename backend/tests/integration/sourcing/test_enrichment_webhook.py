import asyncio
import base64
import hashlib
import json
import logging
import threading
import time
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi.testclient import TestClient
from redis import Redis
from redis.exceptions import RedisError
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
from app.sourcing import webhooks as webhooks_module
from app.sourcing.models import (
    EnrichmentRequest,
    RunCandidate,
    SourcingRun,
    WebhookDelivery,
)
from app.sourcing.state_machine import RunState
from app.sourcing.webhooks import (
    CapabilityTokenCodec,
    RedisWebhookRateLimiter,
    apply_enrichment_payload,
    resolve_webhook_source,
)


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


class AllowAllLimiter:
    def allow(self, source: str, now: float) -> bool:
        del source, now
        return True


def _decrypt_snapshot(reference: str, body: bytes) -> dict[str, object]:
    assert body[:4] == b"SNP1"
    nonce = body[4:16]
    key_nonce = body[16:28]
    wrapped_length = int.from_bytes(body[28:30], "big")
    wrapped_key = body[30 : 30 + wrapped_length]
    ciphertext = body[30 + wrapped_length :]
    aad = f"provider-snapshot-v1\0{reference}".encode()
    data_key = AESGCM(b"s" * 32).decrypt(
        key_nonce, wrapped_key, b"snapshot-data-key\0" + aad
    )
    decoded = AESGCM(data_key).decrypt(nonce, ciphertext, aad)
    payload = json.loads(decoded)
    assert isinstance(payload, dict)
    return payload


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
            reveal_phone_number=True,
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
        webhook_rate_limiter=AllowAllLimiter(),
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
            "snapshot_store": snapshot_store,
            "contact_cipher": contact_cipher,
        }
    engine.dispose()


@pytest.fixture
def apollo_phone_payload() -> dict[str, object]:
    return {
        "status": "success",
        "credits_consumed": 8,
        "people": [
            {
                "id": "person-1",
                "status": "success",
                "phone_numbers": [
                    {
                        "raw_number": "+1 212 555 0112",
                        "type_cd": "mobile",
                        "status_cd": "valid_number",
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


def test_webhook_rejects_provider_person_outside_bound_request(
    webhook_scenario: dict[str, Any], apollo_phone_payload: dict[str, object]
) -> None:
    people = apollo_phone_payload["people"]
    assert isinstance(people, list) and isinstance(people[0], dict)
    people[0]["id"] = "person-outside-request"

    response = webhook_scenario["api"].post(
        f"/webhooks/apollo/{webhook_scenario['token']}",
        json=apollo_phone_payload,
    )

    assert response.status_code == 400
    with Session(webhook_scenario["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 0


@pytest.mark.parametrize("source", ["synchronous", "webhook", "poll"])
def test_persisted_reveal_permissions_filter_denied_contacts_for_every_delivery_path(
    webhook_scenario: dict[str, Any], source: str
) -> None:
    scenario = webhook_scenario
    payload: dict[str, object] = {
        "request_id": 123,
        "credits_consumed": 1,
        "people": [
            {
                "id": "person-1",
                "email": "work@example.test",
                "email_status": "verified",
                "personal_emails": ["private@example.test"],
                "phone_numbers": [
                    {
                        "raw_number": "+1 212 555 0112",
                        "type_cd": "mobile",
                        "status_cd": "valid_number",
                    }
                ],
            }
        ],
    }
    with Session(scenario["engine"]) as session:
        request = session.get(EnrichmentRequest, scenario["request_id"])
        assert request is not None
        request.reveal_personal_emails = False
        request.reveal_phone_number = False
        apply_enrichment_payload(
            session,
            request,
            payload,
            codec=CapabilityTokenCodec(b"test-webhook-key"),
            snapshot_store=scenario["snapshot_store"],
            contact_cipher=scenario["contact_cipher"],
            source=source,
        )
        session.commit()

    with Session(scenario["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 1
    assert len(scenario["objects"].objects) == 1
    (_bucket, reference), body = next(iter(scenario["objects"].objects.items()))
    stored_payload = _decrypt_snapshot(reference, body)
    serialized = json.dumps(stored_payload, sort_keys=True)
    assert "private@example.test" not in serialized
    assert "+1 212 555 0112" not in serialized
    assert "work@example.test" in serialized


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


def test_webhook_rate_limit_is_shared_across_instances_without_local_lock_state() -> (
    None
):
    class SharedRedis:
        def __init__(self) -> None:
            self.counts: dict[str, int] = {}

        def eval(self, script: str, key_count: int, key: str, ttl: int) -> int:
            del script, key_count, ttl
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]

    shared = SharedRedis()
    first = RedisWebhookRateLimiter(shared, limit=1, window_seconds=60)
    second = RedisWebhookRateLimiter(shared, limit=1, window_seconds=60)

    assert first.allow("192.0.2.10", now=100)
    assert not second.allow("192.0.2.10", now=101)
    assert second.allow("192.0.2.11", now=101)
    assert first.allow("192.0.2.10", now=161)
    assert not hasattr(webhooks_module, "_REQUEST_LOCKS")


def test_webhook_rate_limit_is_shared_by_real_redis_instances() -> None:
    first_client = Redis.from_url(Settings.for_test().redis_url)
    second_client = Redis.from_url(Settings.for_test().redis_url)
    try:
        first_client.ping()
    except RedisError:
        pytest.skip("Redis integration service is unavailable")
    now = datetime.now(UTC).timestamp()
    source = f"198.51.100.{uuid4().int % 255}"
    bucket = int(now // 60)
    digest = hashlib.sha256(source.encode()).hexdigest()
    key = f"webhook-rate:apollo:{bucket}:{digest}"
    first_client.delete(key)
    try:
        first = RedisWebhookRateLimiter(first_client, limit=1)
        second = RedisWebhookRateLimiter(second_client, limit=1)

        assert first.allow(source, now)
        assert not second.allow(source, now)
    finally:
        first_client.delete(key)
        first_client.close()
        second_client.close()


def test_webhook_source_uses_forwarded_address_only_from_trusted_proxy() -> None:
    assert (
        resolve_webhook_source(
            peer="10.0.0.5",
            forwarded_for="192.0.2.10, 10.0.0.5",
            trusted_proxies=frozenset({"10.0.0.5"}),
        )
        == "192.0.2.10"
    )
    assert (
        resolve_webhook_source(
            peer="198.51.100.9",
            forwarded_for="192.0.2.10",
            trusted_proxies=frozenset({"10.0.0.5"}),
        )
        == "198.51.100.9"
    )
    assert (
        resolve_webhook_source(
            peer="10.0.0.5",
            forwarded_for="203.0.113.66, 192.0.2.10",
            trusted_proxies=frozenset({"10.0.0.5"}),
        )
        == "192.0.2.10"
    )


def test_webhook_rejects_oversized_body_before_payload_processing(
    webhook_scenario: dict[str, Any],
) -> None:
    response = webhook_scenario["api"].post(
        f"/webhooks/apollo/{webhook_scenario['token']}",
        content=b"x" * (262_144 + 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    with Session(webhook_scenario["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(WebhookDelivery)) == 0


def test_blocked_sync_webhook_dependency_does_not_stall_event_loop(
    webhook_scenario: dict[str, Any], apollo_phone_payload: dict[str, object]
) -> None:
    scenario = webhook_scenario
    release = threading.Event()

    class BlockingLimiter:
        def allow(self, source: str, now: float) -> bool:
            del source, now
            release.wait(timeout=0.75)
            return True

    app = scenario["api"].app
    app.state.webhook_rate_limiter = BlockingLimiter()
    probe_path = f"/event-loop-probe-{uuid4()}"

    @app.get(probe_path)
    async def event_loop_probe() -> dict[str, str]:
        return {"status": "responsive"}

    async def exercise() -> tuple[float, int, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            started_at = time.monotonic()
            webhook = asyncio.create_task(
                client.post(
                    f"/webhooks/apollo/{scenario['token']}",
                    json=apollo_phone_payload,
                )
            )
            await asyncio.sleep(0.05)
            probe = await client.get(probe_path)
            elapsed = time.monotonic() - started_at
            release.set()
            webhook_response = await webhook
            return elapsed, probe.status_code, webhook_response.status_code

    elapsed, probe_status, webhook_status = asyncio.run(exercise())

    assert probe_status == 200
    assert webhook_status == 202
    assert elapsed < 0.3


def test_duplicate_nonterminal_payload_can_terminalize_same_request(
    webhook_scenario: dict[str, Any], apollo_phone_payload: dict[str, object]
) -> None:
    scenario = webhook_scenario
    codec = CapabilityTokenCodec(b"test-webhook-key")
    with Session(scenario["engine"]) as session:
        request = session.get(EnrichmentRequest, scenario["request_id"])
        assert request is not None
        apply_enrichment_payload(
            session,
            request,
            apollo_phone_payload,
            codec=codec,
            snapshot_store=scenario["snapshot_store"],
            contact_cipher=scenario["contact_cipher"],
            source="synchronous",
            terminal=False,
        )
        session.commit()
        assert request.status == "pending"

        apply_enrichment_payload(
            session,
            request,
            apollo_phone_payload,
            codec=codec,
            snapshot_store=scenario["snapshot_store"],
            contact_cipher=scenario["contact_cipher"],
            source="poll",
            terminal=True,
        )
        session.commit()

    with Session(scenario["engine"]) as session:
        request = session.get(EnrichmentRequest, scenario["request_id"])
        run = session.get(SourcingRun, scenario["run_id"])
        assert request is not None and request.status == "completed"
        assert run is not None and run.state is RunState.READY
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 1
        assert session.scalar(select(func.count()).select_from(WebhookDelivery)) == 1


@pytest.mark.parametrize("request_status", ["failed", "cancelled"])
def test_failed_or_cancelled_request_is_fenced_before_any_payload_write(
    webhook_scenario: dict[str, Any],
    apollo_phone_payload: dict[str, object],
    request_status: str,
) -> None:
    scenario = webhook_scenario
    with Session(scenario["engine"]) as session:
        request = session.get(EnrichmentRequest, scenario["request_id"])
        run = session.get(SourcingRun, scenario["run_id"])
        assert request is not None and run is not None
        request.status = request_status
        if request_status == "cancelled":
            run.state = RunState.CANCELLED
            run.cancellation_requested = True
        session.commit()

    response = scenario["api"].post(
        f"/webhooks/apollo/{scenario['token']}",
        json=apollo_phone_payload,
    )

    assert response.status_code == 202
    with Session(scenario["engine"]) as session:
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 0
        assert session.scalar(select(func.count()).select_from(WebhookDelivery)) == 0
    assert scenario["objects"].objects == {}
