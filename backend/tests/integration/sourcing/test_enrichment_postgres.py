import base64
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
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.candidates.contacts import ContactCipher, ContactService, expire_due_contacts
from app.candidates.models import Candidate, ContactPoint, SourceIdentity
from app.clients.models import ClientCompany
from app.core.database import Base
from app.identity.dependencies import apply_tenant_context
from app.identity.models import Tenant, User
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardVersion
from app.providers.base import (
    EnrichmentInput,
    EnrichmentReceipt,
    EnrichmentResult,
    ProviderContact,
)
from app.providers.snapshots import SnapshotStore
from app.sourcing import tasks
from app.sourcing.enrichment import RegionalContactPolicy
from app.sourcing.models import (
    EnrichmentRequest,
    RunCandidate,
    SourcingRun,
    UsageBudget,
    WebhookDelivery,
)
from app.sourcing.service import SourcingService
from app.sourcing.state_machine import RunState
from app.sourcing.webhooks import (
    CapabilityTokenCodec,
    apply_capability_payload,
    apply_enrichment_payload,
)

OWNER_DATABASE_URL = os.getenv("TASK9_OWNER_DATABASE_URL")
API_DATABASE_URL = os.getenv("TASK9_API_DATABASE_URL")
MAINTENANCE_DATABASE_URL = os.getenv("TASK9_MAINTENANCE_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not OWNER_DATABASE_URL or not API_DATABASE_URL or not MAINTENANCE_DATABASE_URL,
    reason="Task 9 PostgreSQL URLs are not configured",
)

_TENANT_TABLES = (
    "candidate_contact_points",
    "enrichment_requests",
    "enrichment_webhook_deliveries",
    "provider_snapshot_references",
)


def _config() -> Config:
    return Config("alembic.ini")


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
            text("GRANT CONNECT ON DATABASE sourcing_test TO sourcing_maintenance")
        )
        connection.execute(text("GRANT USAGE ON SCHEMA public TO sourcing_maintenance"))
    yield engine
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
        )
        connection.execute(
            text(
                "DELETE FROM audit_events WHERE tenant_id IN "
                "(SELECT id FROM tenants WHERE slug LIKE 'task9-%')"
            )
        )
        connection.execute(text("DELETE FROM tenants WHERE slug LIKE 'task9-%'"))
        connection.execute(text("DELETE FROM users WHERE oidc_subject LIKE 'task9|%'"))
        connection.execute(
            text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only")
        )
    engine.dispose()


def test_0007_upgrade_downgrade_and_model_parity(owner_engine: Engine) -> None:
    command.downgrade(_config(), "0006_contacts_enrichment")
    tables = set(inspect(owner_engine).get_table_names())
    assert set(_TENANT_TABLES).issubset(tables)
    request_columns = {
        column["name"]
        for column in inspect(owner_engine).get_columns("enrichment_requests")
    }
    assert "synchronous_credits" not in request_columns

    command.upgrade(_config(), "head")

    with owner_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0007_enrichment_security_fixes"
        )
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )


def test_new_tables_force_rls_with_using_and_check(owner_engine: Engine) -> None:
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
                "WHERE tablename = ANY(:tables) "
                "AND policyname = 'tenant_isolation' ORDER BY tablename"
            ),
            {"tables": list(_TENANT_TABLES)},
        ).all()

    assert flags == sorted((table, True, True) for table in _TENANT_TABLES)
    assert [row.tablename for row in policies] == sorted(_TENANT_TABLES)
    assert all(row.qual and row.with_check for row in policies)


def test_maintenance_role_can_only_erase_due_contacts(
    owner_engine: Engine,
) -> None:
    assert MAINTENANCE_DATABASE_URL is not None
    tenant_id, user_id = uuid4(), uuid4()
    context = RequestContext(tenant_id=tenant_id, user_id=user_id, role=Role.OWNER)
    now = datetime.now(UTC)
    with Session(owner_engine) as session:
        tenant = Tenant(id=tenant_id, slug=f"task9-maint-{tenant_id}")
        expired_candidate = Candidate(
            tenant_id=tenant_id,
            full_name="Expired Contact",
            normalized_name="expired contact",
        )
        current_candidate = Candidate(
            tenant_id=tenant_id,
            full_name="Current Contact",
            normalized_name="current contact",
        )
        session.add(tenant)
        session.flush()
        session.add_all((expired_candidate, current_candidate))
        session.flush()
        service = ContactService(
            session,
            ContactCipher(base64.b64encode(b"c" * 32).decode(), b"lookup"),
        )
        expired = service.store(
            context,
            expired_candidate.id,
            ProviderContact(kind="email", value="expired@example.test"),
        ).contact_point
        current = service.store(
            context,
            current_candidate.id,
            ProviderContact(kind="email", value="current@example.test"),
        ).contact_point
        expired.expires_at = now - timedelta(seconds=1)
        current.expires_at = now + timedelta(days=1)
        expired_id, current_id = expired.id, current.id
        session.commit()

    maintenance_engine = create_engine(MAINTENANCE_DATABASE_URL)
    with Session(maintenance_engine) as session:
        assert expire_due_contacts(session, now=now) == 1
        session.commit()
        visible_ids = set(session.scalars(select(ContactPoint.id)))
        assert visible_ids == {expired_id}
        changed = session.execute(
            text(
                "UPDATE candidate_contact_points SET verification_state = 'expired' "
                "WHERE id = :id"
            ),
            {"id": current_id},
        )
        assert changed.rowcount == 0
        session.rollback()
        with pytest.raises(ProgrammingError):
            session.execute(text("CREATE TABLE task9_maintenance_forbidden (id int)"))
    maintenance_engine.dispose()

    with Session(owner_engine) as session:
        expired_row = session.get(ContactPoint, expired_id)
        current_row = session.get(ContactPoint, current_id)
        assert expired_row is not None and current_row is not None
        assert expired_row.verification_state == "expired"
        assert (
            expired_row.value_ciphertext,
            expired_row.encrypted_data_key,
            expired_row.lookup_hmac,
        ) == (None, None, None)
        assert current_row.verification_state == "unverified"
        assert current_row.value_ciphertext is not None

    with owner_engine.connect() as connection:
        grants = set(
            connection.execute(
                text(
                    "SELECT table_name, privilege_type FROM information_schema."
                    "role_table_grants WHERE grantee = 'sourcing_maintenance'"
                )
            ).all()
        )
    assert grants == {
        ("candidate_contact_points", "SELECT"),
        ("candidate_contact_points", "UPDATE"),
        ("provider_snapshot_references", "SELECT"),
        ("provider_snapshot_references", "DELETE"),
    }


def test_postgres_contact_row_contains_no_plaintext(owner_engine: Engine) -> None:
    assert API_DATABASE_URL is not None
    tenant_id, candidate_id = uuid4(), uuid4()
    with owner_engine.begin() as connection:
        connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"),
            {"tenant": str(tenant_id)},
        )
        connection.execute(
            text(
                "INSERT INTO tenants (id, slug, created_at) VALUES (:id, :slug, now())"
            ),
            {"id": tenant_id, "slug": f"task9-contact-{tenant_id}"},
        )
        connection.execute(
            text(
                "INSERT INTO candidates "
                "(id, tenant_id, full_name, normalized_name, created_at, updated_at) "
                "VALUES (:id, :tenant, 'Priya Sharma', 'priya sharma', now(), now())"
            ),
            {"id": candidate_id, "tenant": tenant_id},
        )
        connection.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                "IN SCHEMA public TO sourcing_api_test"
            )
        )
    engine = create_engine(API_DATABASE_URL)
    context = RequestContext(tenant_id=tenant_id, user_id=uuid4(), role=Role.OWNER)
    with Session(engine) as session:
        apply_tenant_context(session, tenant_id)
        ContactService(
            session,
            ContactCipher(base64.b64encode(b"c" * 32).decode(), b"lookup"),
        ).store(
            context,
            candidate_id,
            ProviderContact(
                kind="email",
                value="priya@example.com",
                verification_state="verified",
                observed_at=datetime.now(UTC),
            ),
        )
        session.commit()
    with Session(engine) as session:
        apply_tenant_context(session, tenant_id)
        point = session.scalar(select(ContactPoint))
        assert point is not None
        serialized = session.scalar(
            text(
                "SELECT concat_ws('|', lookup_hmac, encode(value_ciphertext, 'hex'), "
                "encode(encrypted_data_key, 'hex')) FROM candidate_contact_points"
            )
        )
        assert "priya@example.com" not in serialized
    engine.dispose()


class ThreadSafeObjectStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.lock = threading.Lock()

    def put_object(self, **kwargs: object) -> None:
        with self.lock:
            self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = bytes(
                kwargs["Body"]  # type: ignore[arg-type]
            )

    def delete_object(self, **kwargs: object) -> None:
        with self.lock:
            self.objects.pop((str(kwargs["Bucket"]), str(kwargs["Key"])), None)

    def head_object(self, **kwargs: object) -> dict[str, object]:
        return {}

    def put_bucket_lifecycle_configuration(self, **kwargs: object) -> None:
        return None


def test_concurrent_webhook_and_poll_apply_one_delivery_and_contact(
    owner_engine: Engine,
) -> None:
    assert API_DATABASE_URL is not None
    tenant_id, user_id = uuid4(), uuid4()
    with Session(owner_engine, expire_on_commit=False) as session:
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant, true)"),
            {"tenant": str(tenant_id)},
        )
        tenant = Tenant(id=tenant_id, slug=f"task9-race-{tenant_id}")
        user = User(
            id=user_id,
            oidc_subject=f"task9|{user_id}",
            email=f"{user_id}@example.test",
            display_name="Task 9",
        )
        session.add_all((tenant, user))
        session.flush()
        client = ClientCompany(
            tenant_id=tenant_id,
            name="Task 9 Client",
            normalized_name="task 9 client",
        )
        session.add(client)
        session.flush()
        job = Job(
            tenant_id=tenant_id,
            client_id=client.id,
            owner_user_id=user_id,
            title="Product Manager",
            job_description="Job",
        )
        session.add(job)
        session.flush()
        scorecard = ScorecardVersion(
            tenant_id=tenant_id,
            job_id=job.id,
            version=1,
            target_titles=[],
            seniority=[],
            locations=[],
            industry_code="technology",
            suggested_adjacent_industries=[],
            uncertainties=[],
            extraction_status="ready",
            confirmed_by_user_id=user_id,
            confirmed_at=datetime.now(UTC),
        )
        session.add(scorecard)
        session.flush()
        job.current_scorecard_id = scorecard.id
        candidate = Candidate(
            tenant_id=tenant_id,
            full_name="Priya Sharma",
            normalized_name="priya sharma",
        )
        session.add(candidate)
        session.flush()
        session.add(
            SourceIdentity(
                tenant_id=tenant_id,
                candidate_id=candidate.id,
                provider="apollo",
                provider_person_id="person-race",
                source_timestamp=datetime.now(UTC),
                confidence=1,
            )
        )
        run = SourcingRun(
            tenant_id=tenant_id,
            job_id=job.id,
            scorecard_version_id=scorecard.id,
            started_by_user_id=user_id,
            state=RunState.ENRICHING,
            current_stage=RunState.ENRICHING.value,
        )
        session.add(run)
        session.flush()
        session.add(
            RunCandidate(
                tenant_id=tenant_id,
                run_id=run.id,
                candidate_id=candidate.id,
                scorecard_version_id=scorecard.id,
                match_score=95,
                classification="main",
                enrichment_status="pending",
            )
        )
        request = EnrichmentRequest(
            tenant_id=tenant_id,
            run_id=run.id,
            provider="apollo",
            provider_request_id="123",
            candidate_ids=[str(candidate.id)],
            reservation_key="race",
            status="pending",
            reveal_phone_number=True,
        )
        session.add(request)
        session.flush()
        codec = CapabilityTokenCodec(b"task9-webhook-key")
        token = codec.issue(request.id, tenant_id)
        request.capability_token_hmac = codec.digest(token, tenant_id)
        request_id = request.id
        session.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                "IN SCHEMA public TO sourcing_api_test"
            )
        )
        session.commit()

    payload: dict[str, object] = {
        "credits_consumed": 8,
        "people": [
            {
                "id": "person-race",
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
    cipher = ContactCipher(base64.b64encode(b"c" * 32).decode(), b"lookup")
    snapshots = SnapshotStore(
        ThreadSafeObjectStore(),
        "snapshots",
        base64.b64encode(b"s" * 32).decode(),
    )
    barrier = threading.Barrier(2)
    outcomes: queue.Queue[object] = queue.Queue()

    def webhook() -> None:
        engine = create_engine(API_DATABASE_URL)
        with Session(engine) as session:
            barrier.wait(timeout=5)
            try:
                apply_capability_payload(
                    session,
                    codec,
                    token,
                    payload,
                    snapshot_store=snapshots,
                    contact_cipher=cipher,
                    source="webhook",
                )
                session.commit()
            except Exception as error:  # noqa: BLE001 - outcome is asserted
                session.rollback()
                outcomes.put(error)
            else:
                outcomes.put("webhook")
        engine.dispose()

    def poll() -> None:
        engine = create_engine(API_DATABASE_URL)
        with Session(engine) as session:
            apply_tenant_context(session, tenant_id)
            barrier.wait(timeout=5)
            try:
                request = session.scalar(
                    select(EnrichmentRequest)
                    .where(EnrichmentRequest.id == request_id)
                    .with_for_update()
                )
                assert request is not None
                apply_enrichment_payload(
                    session,
                    request,
                    payload,
                    codec=codec,
                    snapshot_store=snapshots,
                    contact_cipher=cipher,
                    source="poll",
                )
                session.commit()
            except Exception as error:  # noqa: BLE001 - outcome is asserted
                session.rollback()
                outcomes.put(error)
            else:
                outcomes.put("poll")
        engine.dispose()

    threads = [threading.Thread(target=webhook), threading.Thread(target=poll)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    results = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert not any(isinstance(result, Exception) for result in results), results

    engine = create_engine(API_DATABASE_URL)
    with Session(engine) as session:
        apply_tenant_context(session, tenant_id)
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 1
        assert session.scalar(select(func.count()).select_from(WebhookDelivery)) == 1
    engine.dispose()


class CeleryEntryGateway:
    def __init__(self, *, async_phone: bool = False) -> None:
        self.async_phone = async_phone

    def enrich_batch(
        self,
        people: tuple[EnrichmentInput, ...],
        webhook_url: str,
        *,
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
    ) -> EnrichmentReceipt:
        del webhook_url, reveal_personal_emails
        request_id = "9001"
        return EnrichmentReceipt(
            provider="apollo",
            request_id=request_id,
            submitted_count=len(people),
            result=EnrichmentResult(
                provider="apollo",
                request_id=request_id,
                people=(),
                snapshot_payload={"request_id": 9001, "people": []},
                charged_credits=1,
            ),
            charged_units=(
                ("enrichments", len(people)),
                ("estimated_credits", 1),
            ),
        )

    def poll_enrichment(self, request_id: str) -> EnrichmentResult:
        assert request_id == "9001"
        return EnrichmentResult(
            provider="apollo",
            request_id=request_id,
            people=(),
            snapshot_payload={"credits_consumed": 0, "people": []},
            charged_credits=0,
        )

    def close(self) -> None:
        return None


def _seed_celery_entry_run(
    owner_engine: Engine, *, suffix: str
) -> tuple[UUID, UUID, UUID, UUID]:
    tenant_id, user_id = uuid4(), uuid4()
    with Session(owner_engine, expire_on_commit=False) as session:
        tenant = Tenant(id=tenant_id, slug=f"task9-celery-{suffix}-{tenant_id}")
        user = User(
            id=user_id,
            oidc_subject=f"task9|celery|{suffix}|{user_id}",
            email=f"{user_id}@example.test",
            display_name="Celery Entry",
        )
        session.add_all((tenant, user))
        session.flush()
        client = ClientCompany(
            tenant_id=tenant_id,
            name="Celery Client",
            normalized_name=f"celery client {suffix}",
        )
        session.add(client)
        session.flush()
        job = Job(
            tenant_id=tenant_id,
            client_id=client.id,
            owner_user_id=user_id,
            title="Engineer",
            job_description="Job",
        )
        session.add(job)
        session.flush()
        scorecard = ScorecardVersion(
            tenant_id=tenant_id,
            job_id=job.id,
            version=1,
            target_titles=[],
            seniority=[],
            locations=[],
            industry_code="technology",
            suggested_adjacent_industries=[],
            uncertainties=[],
            extraction_status="ready",
            confirmed_by_user_id=user_id,
            confirmed_at=datetime.now(UTC),
        )
        session.add(scorecard)
        session.flush()
        job.current_scorecard_id = scorecard.id
        candidate = Candidate(
            tenant_id=tenant_id,
            full_name="Celery Candidate",
            normalized_name=f"celery candidate {suffix}",
            location="New York, United States",
        )
        session.add(candidate)
        session.flush()
        session.add(
            SourceIdentity(
                tenant_id=tenant_id,
                candidate_id=candidate.id,
                provider="apollo",
                provider_person_id=f"celery-person-{suffix}",
                source_timestamp=datetime.now(UTC),
                confidence=1,
            )
        )
        run = SourcingRun(
            tenant_id=tenant_id,
            job_id=job.id,
            scorecard_version_id=scorecard.id,
            started_by_user_id=user_id,
            state=RunState.ENRICHING,
            current_stage=RunState.ENRICHING.value,
        )
        session.add(run)
        session.flush()
        run_candidate = RunCandidate(
            tenant_id=tenant_id,
            run_id=run.id,
            candidate_id=candidate.id,
            scorecard_version_id=scorecard.id,
            match_score=90,
            classification="main",
        )
        session.add(run_candidate)
        session.add(
            UsageBudget(
                tenant_id=tenant_id,
                max_enrichments=10,
                max_estimated_credits=100,
            )
        )
        session.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                "IN SCHEMA public TO sourcing_api_test"
            )
        )
        session.commit()
        return tenant_id, user_id, run.id, run_candidate.id


def _patch_celery_entry_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    api_engine: Engine,
    gateway: CeleryEntryGateway,
    *,
    reveal_phone: bool,
) -> None:
    objects = ThreadSafeObjectStore()
    snapshots = SnapshotStore(
        objects, "snapshots", base64.b64encode(b"s" * 32).decode()
    )
    cipher = ContactCipher(base64.b64encode(b"c" * 32).decode(), b"lookup")
    codec = CapabilityTokenCodec(b"task9-webhook-key")
    monkeypatch.setattr(
        tasks,
        "database_session_factory",
        sessionmaker(bind=api_engine, expire_on_commit=False),
    )
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: type("S", (), {"webhook_base_url": "https://api.example.test"})(),
    )
    monkeypatch.setattr(tasks, "ApolloGateway", lambda settings: gateway)
    monkeypatch.setattr(
        tasks,
        "_enrichment_dependencies",
        lambda settings: (
            cipher,
            snapshots,
            RegionalContactPolicy(False, reveal_phone),
            codec,
        ),
    )
    monkeypatch.setattr(
        tasks.poll_enrichment_result, "apply_async", lambda **kwargs: None
    )


def test_auto_enrichment_celery_entry_binds_tenant_before_forced_rls(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert API_DATABASE_URL is not None
    tenant_id, user_id, run_id, _ = _seed_celery_entry_run(owner_engine, suffix="auto")
    api_engine = create_engine(API_DATABASE_URL)
    with api_engine.connect() as connection:
        assert not connection.scalar(
            text("SELECT current_setting('app.tenant_id', true)")
        )
    _patch_celery_entry_dependencies(
        monkeypatch, api_engine, CeleryEntryGateway(), reveal_phone=False
    )

    tasks.enrich_run.run(str(run_id), str(tenant_id), str(user_id), 50)

    with Session(owner_engine) as session:
        run = session.get(SourcingRun, run_id)
        assert run is not None and run.state is RunState.READY
    api_engine.dispose()


def test_on_demand_and_poll_celery_entries_bind_tenant_before_forced_rls(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert API_DATABASE_URL is not None
    tenant_id, user_id, _run_id, run_candidate_id = _seed_celery_entry_run(
        owner_engine, suffix="demand"
    )
    context = RequestContext(tenant_id=tenant_id, user_id=user_id, role=Role.OWNER)
    with Session(owner_engine, expire_on_commit=False) as session:
        request, created = SourcingService(
            session, b"test-key"
        ).queue_on_demand_enrichment(
            context,
            run_candidate_id,
            idempotency_key="celery-entry",
        )
        assert created
        request_id = request.id
        session.commit()
    api_engine = create_engine(API_DATABASE_URL)
    _patch_celery_entry_dependencies(
        monkeypatch, api_engine, CeleryEntryGateway(async_phone=True), reveal_phone=True
    )

    tasks.enrich_request.run(str(request_id), str(tenant_id), str(user_id))
    tasks.poll_enrichment_result.run(str(request_id), str(tenant_id), str(user_id))

    with Session(owner_engine) as session:
        request = session.get(EnrichmentRequest, request_id)
        assert request is not None and request.status == "completed"
        assert request.usage_reconciled_at is not None
    api_engine.dispose()
