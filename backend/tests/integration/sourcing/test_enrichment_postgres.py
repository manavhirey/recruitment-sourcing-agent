import base64
import os
import queue
import threading
from collections.abc import Generator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.candidates.contacts import ContactCipher, ContactService
from app.candidates.models import Candidate, ContactPoint, SourceIdentity
from app.clients.models import ClientCompany
from app.core.database import Base
from app.identity.dependencies import apply_tenant_context
from app.identity.models import Tenant, User
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardVersion
from app.providers.base import ProviderContact
from app.providers.snapshots import SnapshotStore
from app.sourcing.models import (
    EnrichmentRequest,
    RunCandidate,
    SourcingRun,
    WebhookDelivery,
)
from app.sourcing.state_machine import RunState
from app.sourcing.webhooks import (
    CapabilityTokenCodec,
    apply_capability_payload,
    apply_enrichment_payload,
)

OWNER_DATABASE_URL = os.getenv("TASK9_OWNER_DATABASE_URL")
API_DATABASE_URL = os.getenv("TASK9_API_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not OWNER_DATABASE_URL or not API_DATABASE_URL,
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


def test_0006_upgrade_downgrade_and_model_parity(owner_engine: Engine) -> None:
    command.downgrade(_config(), "0005_sourcing_audit")
    tables = set(inspect(owner_engine).get_table_names())
    assert not tables.intersection(_TENANT_TABLES)
    assert "enrichment_status" not in {
        column["name"] for column in inspect(owner_engine).get_columns("run_candidates")
    }

    command.upgrade(_config(), "head")

    with owner_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0006_contacts_enrichment"
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
                "WHERE tablename = ANY(:tables) ORDER BY tablename"
            ),
            {"tables": list(_TENANT_TABLES)},
        ).all()

    assert flags == sorted((table, True, True) for table in _TENANT_TABLES)
    assert [row.tablename for row in policies] == sorted(_TENANT_TABLES)
    assert all(row.qual and row.with_check for row in policies)


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
        "request_id": 123,
        "people": [
            {
                "id": "person-race",
                "name": "Priya Sharma",
                "email": "race@example.com",
                "email_status": "verified",
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
