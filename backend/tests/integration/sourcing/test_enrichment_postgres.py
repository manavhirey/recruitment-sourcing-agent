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
from app.candidates.contacts import ContactCipher, ContactService
from app.candidates.models import (
    Candidate,
    ContactPoint,
    ContactRetentionTombstone,
    SourceIdentity,
)
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
    ProviderAuthenticationError,
    ProviderContact,
    ProviderRateLimited,
)
from app.providers.health import ProviderConnectorState
from app.providers.snapshots import SnapshotStore
from app.sourcing import tasks
from app.sourcing.dispatch_recovery import recover_pending_enrichment_dispatches
from app.sourcing.enrichment import RegionalContactPolicy
from app.sourcing.models import (
    EnrichmentRequest,
    EnrichmentRetryDispatch,
    ProviderSnapshot,
    RunCandidate,
    SourcingRun,
    UsageBudget,
    UsageLedger,
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
    "candidate_contact_retention_tombstones",
    "enrichment_requests",
    "enrichment_retry_dispatches",
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
                "ALTER TABLE crm_acceptance_cohorts "
                "DISABLE TRIGGER crm_acceptance_cohorts_append_only"
            )
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
        connection.execute(
            text(
                "ALTER TABLE crm_acceptance_cohorts "
                "ENABLE TRIGGER crm_acceptance_cohorts_append_only"
            )
        )
    engine.dispose()


def test_0007_to_head_upgrade_downgrade_and_model_parity(owner_engine: Engine) -> None:
    command.downgrade(_config(), "0006_contacts_enrichment")
    tables = set(inspect(owner_engine).get_table_names())
    assert (
        set(_TENANT_TABLES)
        - {
            "candidate_contact_retention_tombstones",
            "enrichment_retry_dispatches",
        }
        <= tables
    )
    assert "candidate_contact_retention_tombstones" not in tables
    request_columns = {
        column["name"]
        for column in inspect(owner_engine).get_columns("enrichment_requests")
    }
    assert "synchronous_credits" not in request_columns

    command.upgrade(_config(), "head")

    with owner_engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0017_enrich_dispatch_deadlines"
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
        assert session.scalar(text("SELECT maintenance_erase_due_contacts()")) == 1
        session.commit()
        with pytest.raises(ProgrammingError):
            session.scalars(select(ContactPoint.id)).all()
        session.rollback()
        with pytest.raises(ProgrammingError):
            session.execute(
                text(
                    "UPDATE candidate_contact_points "
                    "SET verification_state = 'expired' WHERE id = :id"
                ),
                {"id": current_id},
            )
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
        tombstone = session.scalar(
            select(ContactRetentionTombstone).where(
                ContactRetentionTombstone.contact_point_id == expired_id
            )
        )
        assert tombstone is not None and tombstone.suppression_hmac
        assert "expired@example.test" not in tombstone.suppression_hmac

    with owner_engine.connect() as connection:
        table_grants = set(
            connection.execute(
                text(
                    "SELECT table_name, privilege_type FROM information_schema."
                    "role_table_grants WHERE grantee = 'sourcing_maintenance'"
                )
            ).all()
        )
        routine_grants = set(
            connection.execute(
                text(
                    "SELECT routine_name, privilege_type FROM information_schema."
                    "role_routine_grants WHERE grantee = 'sourcing_maintenance' "
                    "AND routine_name LIKE 'maintenance_%'"
                )
            ).all()
        )
    assert table_grants == set()
    assert routine_grants == {
        ("maintenance_claim_expired_snapshots", "EXECUTE"),
        ("maintenance_delete_claimed_snapshot", "EXECUTE"),
        ("maintenance_erase_due_contacts", "EXECUTE"),
        ("maintenance_record_snapshot_delete_failure", "EXECUTE"),
        ("maintenance_stuck_run_count", "EXECUTE"),
        ("maintenance_claim_pending_sourcing_dispatches", "EXECUTE"),
        ("maintenance_complete_sourcing_dispatch", "EXECUTE"),
        ("maintenance_release_sourcing_dispatch", "EXECUTE"),
        ("maintenance_claim_pending_enrichment_dispatches", "EXECUTE"),
        ("maintenance_complete_enrichment_dispatch", "EXECUTE"),
        ("maintenance_release_enrichment_dispatch", "EXECUTE"),
        ("maintenance_claim_pending_enrichment_retries", "EXECUTE"),
        ("maintenance_complete_enrichment_retry_publish", "EXECUTE"),
        ("maintenance_release_enrichment_retry_publish", "EXECUTE"),
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
                "(id, tenant_id, full_name, normalized_name, normalized_skills, "
                "industry_codes, created_at, updated_at) VALUES "
                "(:id, :tenant, 'Priya Sharma', 'priya sharma', '[]'::jsonb, "
                "'[]'::jsonb, now(), now())"
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
        suppression = session.scalar(select(ContactRetentionTombstone.suppression_hmac))
        assert suppression is not None
        assert "priya@example.com" not in suppression
    engine.dispose()


def test_postgres_erasure_tombstone_blocks_stale_replay_under_forced_rls(
    owner_engine: Engine,
) -> None:
    assert API_DATABASE_URL is not None
    assert MAINTENANCE_DATABASE_URL is not None
    tenant_id, candidate_id = uuid4(), uuid4()
    observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    deadline = observed_at + timedelta(days=180)
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, slug, created_at) VALUES (:id, :slug, now())"
            ),
            {"id": tenant_id, "slug": f"task9-tombstone-{tenant_id}"},
        )
        connection.execute(
            text(
                "INSERT INTO candidates "
                "(id, tenant_id, full_name, normalized_name, normalized_skills, "
                "industry_codes, created_at, updated_at) VALUES "
                "(:id, :tenant, 'Retention Candidate', 'retention candidate', "
                "'[]'::jsonb, '[]'::jsonb, now(), now())"
            ),
            {"id": candidate_id, "tenant": tenant_id},
        )
        connection.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                "IN SCHEMA public TO sourcing_api_test"
            )
        )

    api_engine = create_engine(API_DATABASE_URL)
    context = RequestContext(tenant_id=tenant_id, user_id=uuid4(), role=Role.OWNER)
    cipher = ContactCipher(base64.b64encode(b"c" * 32).decode(), b"lookup")
    with Session(api_engine) as session:
        apply_tenant_context(session, tenant_id)
        point = (
            ContactService(session, cipher)
            .store(
                context,
                candidate_id,
                ProviderContact(
                    kind="email",
                    value="retained@example.test",
                    verification_state="verified",
                    observed_at=observed_at,
                ),
                processed_at=observed_at,
            )
            .contact_point
        )
        point_id = point.id
        session.commit()

    maintenance_engine = create_engine(MAINTENANCE_DATABASE_URL)
    with Session(maintenance_engine) as session:
        session.scalar(text("SELECT maintenance_erase_due_contacts()"))
        session.commit()
    maintenance_engine.dispose()

    with Session(api_engine) as session:
        apply_tenant_context(session, tenant_id)
        replay = ContactService(session, cipher).store(
            context,
            candidate_id,
            ProviderContact(
                kind="email",
                value="retained@example.test",
                verification_state="unverified",
                observed_at=deadline + timedelta(days=1),
            ),
            processed_at=deadline + timedelta(days=1),
        )
        assert replay.accepted is False
        assert replay.contact_point.id == point_id
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 1
        tombstone = session.scalar(select(ContactRetentionTombstone))
        assert tombstone is not None
        assert "retained@example.test" not in tombstone.suppression_hmac
        session.rollback()

    with Session(api_engine) as session:
        apply_tenant_context(session, uuid4())
        assert (
            session.scalar(select(func.count()).select_from(ContactRetentionTombstone))
            == 0
        )

    with Session(api_engine) as session:
        apply_tenant_context(session, tenant_id)
        reverified = ContactService(session, cipher).store(
            context,
            candidate_id,
            ProviderContact(
                kind="email",
                value="retained@example.test",
                verification_state="verified",
                observed_at=deadline + timedelta(days=2),
            ),
            processed_at=deadline + timedelta(days=2),
        )
        assert reverified.accepted is True
        assert reverified.contact_point.id == point_id
        assert reverified.contact_point.expires_at == deadline + timedelta(days=182)
        session.commit()
    api_engine.dispose()


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

    def list_object_versions(self, **kwargs: object) -> dict[str, object]:
        return {"Versions": [], "DeleteMarkers": [], "IsTruncated": False}

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


class AuthenticationFailureGateway(CeleryEntryGateway):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def enrich_batch(
        self,
        people: tuple[EnrichmentInput, ...],
        webhook_url: str,
        *,
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
    ) -> EnrichmentReceipt:
        del people, webhook_url, reveal_personal_emails, reveal_phone_number
        self.calls += 1
        raise ProviderAuthenticationError("provider rejected credentials")


class BlockingAcceptedGateway(CeleryEntryGateway):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def enrich_batch(
        self,
        people: tuple[EnrichmentInput, ...],
        webhook_url: str,
        *,
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
    ) -> EnrichmentReceipt:
        del webhook_url, reveal_personal_emails, reveal_phone_number
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release provider")
        return EnrichmentReceipt(
            provider="apollo",
            request_id="accepted-on-demand",
            submitted_count=len(people),
            result=None,
            charged_units=(("enrichments", len(people)), ("estimated_credits", 1)),
        )


class RateLimitThenAcceptedGateway(CeleryEntryGateway):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def enrich_batch(
        self,
        people: tuple[EnrichmentInput, ...],
        webhook_url: str,
        *,
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
    ) -> EnrichmentReceipt:
        del webhook_url, reveal_personal_emails, reveal_phone_number
        self.calls += 1
        if self.calls == 1:
            raise ProviderRateLimited(120)
        return EnrichmentReceipt(
            provider="apollo",
            request_id="accepted-after-rate-limit",
            submitted_count=len(people),
            result=None,
            charged_units=(("enrichments", len(people)), ("estimated_credits", 1)),
        )


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


def test_maintenance_snapshot_functions_only_claim_and_delete_due_references(
    owner_engine: Engine,
) -> None:
    assert MAINTENANCE_DATABASE_URL is not None
    tenant_id, _user_id, run_id, _candidate_id = _seed_celery_entry_run(
        owner_engine, suffix="snap"
    )
    now = datetime.now(UTC)
    with Session(owner_engine, expire_on_commit=False) as session:
        expired_request = EnrichmentRequest(
            tenant_id=tenant_id,
            run_id=run_id,
            provider="apollo",
            provider_request_id="expired-snapshot",
            candidate_ids=[],
            reservation_key="snapshot-expired",
            status="completed",
        )
        current_request = EnrichmentRequest(
            tenant_id=tenant_id,
            run_id=run_id,
            provider="apollo",
            provider_request_id="current-snapshot",
            candidate_ids=[],
            reservation_key="snapshot-current",
            status="completed",
        )
        session.add_all((expired_request, current_request))
        session.flush()
        expired = ProviderSnapshot(
            tenant_id=tenant_id,
            run_id=run_id,
            enrichment_request_id=expired_request.id,
            provider="apollo",
            object_reference=f"{tenant_id}/{run_id}/apollo/expired-snapshot",
            checksum_sha256="a" * 64,
            created_at=now - timedelta(days=31),
            expires_at=now - timedelta(days=1),
        )
        current = ProviderSnapshot(
            tenant_id=tenant_id,
            run_id=run_id,
            enrichment_request_id=current_request.id,
            provider="apollo",
            object_reference=f"{tenant_id}/{run_id}/apollo/current-snapshot",
            checksum_sha256="b" * 64,
            created_at=now,
            expires_at=now + timedelta(days=30),
        )
        session.add_all((expired, current))
        session.flush()
        expired_id, current_id = expired.id, current.id
        session.commit()

    engine = create_engine(MAINTENANCE_DATABASE_URL)
    with Session(engine) as session:
        claimed = session.execute(
            text(
                "SELECT snapshot_id, tenant_id, object_reference "
                "FROM maintenance_claim_expired_snapshots(100)"
            )
        ).all()
        session.commit()
        assert claimed == [
            (
                expired_id,
                tenant_id,
                f"{tenant_id}/{run_id}/apollo/expired-snapshot",
            )
        ]
        assert session.scalar(
            text("SELECT maintenance_delete_claimed_snapshot(:id)"),
            {"id": expired_id},
        )
        session.commit()
        with pytest.raises(ProgrammingError):
            session.scalars(select(ProviderSnapshot.id)).all()
        session.rollback()
    engine.dispose()

    with Session(owner_engine) as session:
        assert session.get(ProviderSnapshot, expired_id) is None
        assert session.get(ProviderSnapshot, current_id) is not None


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
        "get_worker_settings",
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


def _queue_celery_on_demand_request(
    owner_engine: Engine,
    tenant_id: UUID,
    user_id: UUID,
    run_candidate_id: UUID,
    *,
    idempotency_key: str,
) -> tuple[UUID, UUID]:
    context = RequestContext(tenant_id=tenant_id, user_id=user_id, role=Role.OWNER)
    with Session(owner_engine, expire_on_commit=False) as session:
        run_candidate = session.get(RunCandidate, run_candidate_id)
        assert run_candidate is not None
        run = session.get(SourcingRun, run_candidate.run_id)
        assert run is not None
        run.state = RunState.READY
        run.current_stage = RunState.READY.value
        run_candidate.enrichment_status = "unavailable"
        session.commit()
        service = SourcingService(session, b"test-key")
        outcome = service.queue_on_demand_enrichment(
            context,
            run_candidate_id,
            idempotency_key=idempotency_key,
        )
        assert outcome.claim_token is not None
        request_id = outcome.request.id
        run_id = outcome.request.run_id
        session.commit()
        assert service.finish_enrichment_dispatch(
            context,
            request_id,
            outcome.claim_token,
            published=True,
        )
        session.commit()
        return request_id, run_id


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


def test_cross_run_auth_circuit_sweeps_disabled_retry_generation(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert API_DATABASE_URL is not None
    tenant_a, user_a, run_a, run_candidate_a = _seed_celery_entry_run(
        owner_engine, suffix="circuit-a"
    )
    tenant_b, user_b, run_b, _ = _seed_celery_entry_run(
        owner_engine, suffix="circuit-b"
    )
    now = datetime.now(UTC)
    reservation_states = {
        "circuit-queued": ("queued", None, None),
        "circuit-accepted": ("pending", "accepted-request", None),
        "circuit-ambiguous": ("submitting", None, now),
    }
    with Session(owner_engine, expire_on_commit=False) as session:
        connector = session.get(ProviderConnectorState, "apollo")
        if connector is not None:
            session.delete(connector)
        run = session.get(SourcingRun, run_a)
        run_candidate = session.get(RunCandidate, run_candidate_a)
        assert run is not None and run_candidate is not None
        for reservation_key, (
            status,
            provider_request_id,
            reconciled_at,
        ) in reservation_states.items():
            request = EnrichmentRequest(
                tenant_id=tenant_a,
                run_id=run_a,
                provider="apollo",
                provider_request_id=provider_request_id,
                candidate_ids=[str(run_candidate.candidate_id)],
                reservation_key=reservation_key,
                status=status,
                usage_reconciled_at=reconciled_at,
            )
            session.add(request)
            for unit_type, requested_units in (
                ("enrichments", 1),
                ("estimated_credits", 9),
            ):
                session.add(
                    UsageLedger(
                        tenant_id=tenant_a,
                        run_id=run_a,
                        job_id=run.job_id,
                        provider="apollo",
                        endpoint="people_bulk_match",
                        unit_type=unit_type,
                        reservation_key=reservation_key,
                        requested_units=requested_units,
                        charged_units=(
                            requested_units if reconciled_at is not None else None
                        ),
                        reconciled_at=reconciled_at,
                    )
                )
        session.add(
            EnrichmentRetryDispatch(
                tenant_id=tenant_a,
                run_id=run_a,
                generation=1,
                status="published",
                state_fingerprint="a" * 64,
                task_id=f"enrich-run-retry:{run_a}:1",
                requested_by_user_id=user_a,
                candidate_limit=50,
                not_before=now - timedelta(seconds=1),
            )
        )
        session.commit()

    gateway = AuthenticationFailureGateway()
    api_engine = create_engine(API_DATABASE_URL)
    _patch_celery_entry_dependencies(
        monkeypatch, api_engine, gateway, reveal_phone=False
    )
    try:
        tasks.enrich_run.run(str(run_b), str(tenant_b), str(user_b), 50)
        assert gateway.calls == 1

        tasks.enrich_run.run(str(run_a), str(tenant_a), str(user_a), 50, 1)
        assert gateway.calls == 1

        with Session(owner_engine) as session:
            requests = list(
                session.scalars(
                    select(EnrichmentRequest)
                    .where(
                        EnrichmentRequest.tenant_id == tenant_a,
                        EnrichmentRequest.run_id == run_a,
                    )
                    .order_by(EnrichmentRequest.reservation_key)
                )
            )
            assert requests
            assert all(request.status == "failed" for request in requests)
            assert all(
                request.error_code == "provider_connector_disabled"
                for request in requests
            )
            assert all(request.usage_reconciled_at is not None for request in requests)
            assert not session.scalar(
                select(func.count())
                .select_from(EnrichmentRequest)
                .where(
                    EnrichmentRequest.tenant_id == tenant_a,
                    EnrichmentRequest.run_id == run_a,
                    EnrichmentRequest.status.in_(("queued", "submitting", "pending")),
                )
            )
            run = session.get(SourcingRun, run_a)
            run_candidate = session.get(RunCandidate, run_candidate_a)
            retry = session.get(EnrichmentRetryDispatch, (tenant_a, run_a))
            assert run is not None and run.state is RunState.PARTIALLY_READY
            assert run.error_code == "provider_connector_disabled"
            assert run_candidate is not None
            assert run_candidate.enrichment_status == "failed"
            assert retry is not None and retry.status == "completed"
            assert retry.claim_token is None and retry.claimed_at is None

            ledger = list(
                session.scalars(
                    select(UsageLedger).where(
                        UsageLedger.tenant_id == tenant_a,
                        UsageLedger.run_id == run_a,
                    )
                )
            )
            charges = {
                (row.reservation_key, row.unit_type): row.charged_units
                for row in ledger
            }
            assert charges[("circuit-queued", "enrichments")] == 0
            assert charges[("circuit-queued", "estimated_credits")] == 0
            for reservation_key in ("circuit-accepted", "circuit-ambiguous"):
                assert charges[(reservation_key, "enrichments")] == 1
                assert charges[(reservation_key, "estimated_credits")] == 9
    finally:
        api_engine.dispose()
        with Session(owner_engine) as session:
            connector = session.get(ProviderConnectorState, "apollo")
            if connector is not None:
                session.delete(connector)
            session.commit()


def test_on_demand_submission_serializes_disabled_run_sweep(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert API_DATABASE_URL is not None
    tenant_id, user_id, run_id, run_candidate_id = _seed_celery_entry_run(
        owner_engine, suffix="demand-race"
    )
    request_id, queued_run_id = _queue_celery_on_demand_request(
        owner_engine,
        tenant_id,
        user_id,
        run_candidate_id,
        idempotency_key="serialized-provider-call",
    )
    assert queued_run_id == run_id
    gateway = BlockingAcceptedGateway()
    outcomes: queue.Queue[BaseException | None] = queue.Queue()
    api_engine = create_engine(API_DATABASE_URL)
    _patch_celery_entry_dependencies(
        monkeypatch, api_engine, gateway, reveal_phone=False
    )

    def submit_on_demand() -> None:
        try:
            tasks.enrich_request.run(str(request_id), str(tenant_id), str(user_id))
            outcomes.put(None)
        except BaseException as error:  # noqa: BLE001 - asserted in parent thread
            outcomes.put(error)

    worker = threading.Thread(target=submit_on_demand)
    try:
        worker.start()
        assert gateway.entered.wait(timeout=5)
        tasks.disable_provider(
            tasks.database_session_factory, "apollo", "authentication_error"
        )

        tasks.enrich_run.run(str(run_id), str(tenant_id), str(user_id), 50)
        with Session(owner_engine) as session:
            request = session.get(EnrichmentRequest, request_id)
            assert request is not None and request.status == "submitting"
            assert all(
                row.charged_units is None
                for row in session.scalars(
                    select(UsageLedger).where(
                        UsageLedger.tenant_id == tenant_id,
                        UsageLedger.run_id == run_id,
                        UsageLedger.reservation_key == request.reservation_key,
                    )
                )
            )

        gateway.release.set()
        worker.join(timeout=8)
        assert not worker.is_alive()
        assert outcomes.get_nowait() is None

        tasks.enrich_run.run(str(run_id), str(tenant_id), str(user_id), 50)
        with Session(owner_engine) as session:
            request = session.get(EnrichmentRequest, request_id)
            assert request is not None and request.status == "completed"
            assert request.provider_request_id == "accepted-on-demand"
            assert request.usage_reconciled_at is not None
            assert request.dispatch_pending is False
            assert request.dispatch_claim_token is None
            charges = {
                row.unit_type: row.charged_units
                for row in session.scalars(
                    select(UsageLedger).where(
                        UsageLedger.tenant_id == tenant_id,
                        UsageLedger.run_id == run_id,
                        UsageLedger.reservation_key == request.reservation_key,
                    )
                )
            }
            assert charges == {"enrichments": 1, "estimated_credits": 1}
        assert gateway.calls == 1
    finally:
        gateway.release.set()
        worker.join(timeout=8)
        api_engine.dispose()
        with Session(owner_engine) as session:
            connector = session.get(ProviderConnectorState, "apollo")
            if connector is not None:
                session.delete(connector)
            session.commit()


def test_on_demand_lock_contention_recovers_without_client_retry(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert API_DATABASE_URL is not None and MAINTENANCE_DATABASE_URL is not None
    tenant_id, user_id, run_id, run_candidate_id = _seed_celery_entry_run(
        owner_engine, suffix="recover"
    )
    request_id, queued_run_id = _queue_celery_on_demand_request(
        owner_engine,
        tenant_id,
        user_id,
        run_candidate_id,
        idempotency_key="recover-contended-delivery",
    )
    assert queued_run_id == run_id
    gateway = BlockingAcceptedGateway()
    gateway.release.set()
    api_engine = create_engine(API_DATABASE_URL)
    _patch_celery_entry_dependencies(
        monkeypatch, api_engine, gateway, reveal_phone=False
    )
    try:
        with tasks._enrichment_execution_lock(
            tasks.database_session_factory, tenant_id, run_id
        ) as acquired:
            assert acquired
            tasks.enrich_request.run(str(request_id), str(tenant_id), str(user_id))
        assert gateway.calls == 0
        with Session(owner_engine) as session:
            request = session.get(EnrichmentRequest, request_id)
            assert request is not None and request.status == "queued"
            assert request.dispatch_pending is True
            assert request.dispatch_claim_token is None

        published = []
        recover_pending_enrichment_dispatches(
            MAINTENANCE_DATABASE_URL,
            published.append,
        )
        claim = next(item for item in published if item.request_id == request_id)
        assert claim.dispatch_key == f"enrichment-request-{request_id}"
        tasks.enrich_request.run(
            str(claim.request_id), str(claim.tenant_id), str(claim.user_id)
        )

        with Session(owner_engine) as session:
            request = session.get(EnrichmentRequest, request_id)
            assert request is not None and request.status == "completed"
            assert request.provider_request_id == "accepted-on-demand"
            assert request.dispatch_pending is False
            assert request.dispatch_claimed_at is None
            assert request.dispatch_claim_token is None
            assert request.usage_reconciled_at is not None
        assert gateway.calls == 1
    finally:
        api_engine.dispose()
        with Session(owner_engine) as session:
            connector = session.get(ProviderConnectorState, "apollo")
            if connector is not None:
                session.delete(connector)
            session.commit()


def test_stale_submitting_on_demand_dispatch_is_reclaimed_without_provider_call(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert API_DATABASE_URL is not None and MAINTENANCE_DATABASE_URL is not None
    tenant_id, user_id, run_id, run_candidate_id = _seed_celery_entry_run(
        owner_engine, suffix="stale"
    )
    request_id, _ = _queue_celery_on_demand_request(
        owner_engine,
        tenant_id,
        user_id,
        run_candidate_id,
        idempotency_key="stale-submitting-recovery",
    )
    with Session(owner_engine) as session:
        request = session.get(EnrichmentRequest, request_id)
        assert request is not None
        request.status = "submitting"
        request.stage_deadline = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()

    gateway = BlockingAcceptedGateway()
    gateway.release.set()
    api_engine = create_engine(API_DATABASE_URL)
    _patch_celery_entry_dependencies(
        monkeypatch, api_engine, gateway, reveal_phone=False
    )
    context = RequestContext(tenant_id=tenant_id, user_id=user_id, role=Role.OWNER)
    try:
        assert tasks._requeue_enrichment_dispatch(request_id, context)
        published = []
        recover_pending_enrichment_dispatches(
            MAINTENANCE_DATABASE_URL,
            published.append,
        )
        claim = next(item for item in published if item.request_id == request_id)
        tasks.enrich_request.run(
            str(claim.request_id), str(claim.tenant_id), str(claim.user_id)
        )

        with Session(owner_engine) as session:
            request = session.get(EnrichmentRequest, request_id)
            assert request is not None and request.status == "failed"
            assert request.error_code == "ambiguous_provider_submission"
            assert request.dispatch_pending is False
            assert request.dispatch_claimed_at is None
            assert request.dispatch_claim_token is None
            charges = {
                row.unit_type: row.charged_units
                for row in session.scalars(
                    select(UsageLedger).where(
                        UsageLedger.tenant_id == tenant_id,
                        UsageLedger.run_id == run_id,
                        UsageLedger.reservation_key == request.reservation_key,
                    )
                )
            }
            assert charges == {"enrichments": 1, "estimated_credits": 9}
        assert gateway.calls == 0
    finally:
        api_engine.dispose()


def test_rate_limited_on_demand_dispatch_waits_for_durable_deadline(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert API_DATABASE_URL is not None and MAINTENANCE_DATABASE_URL is not None
    tenant_id, user_id, _run_id, run_candidate_id = _seed_celery_entry_run(
        owner_engine, suffix="deadline"
    )
    request_id, _ = _queue_celery_on_demand_request(
        owner_engine,
        tenant_id,
        user_id,
        run_candidate_id,
        idempotency_key="rate-limit-deadline",
    )
    gateway = RateLimitThenAcceptedGateway()
    api_engine = create_engine(API_DATABASE_URL)
    _patch_celery_entry_dependencies(
        monkeypatch, api_engine, gateway, reveal_phone=False
    )
    try:
        tasks.enrich_request.run(str(request_id), str(tenant_id), str(user_id))
        assert gateway.calls == 1
        with Session(owner_engine) as session:
            request = session.get(EnrichmentRequest, request_id)
            assert request is not None and request.status == "queued"
            assert request.dispatch_pending is True
            assert request.poll_after is not None

        published = []
        recover_pending_enrichment_dispatches(
            MAINTENANCE_DATABASE_URL,
            published.append,
        )
        assert not any(item.request_id == request_id for item in published)
        tasks.enrich_request.run(str(request_id), str(tenant_id), str(user_id))
        assert gateway.calls == 1

        with Session(owner_engine) as session:
            request = session.get(EnrichmentRequest, request_id)
            assert request is not None
            request.poll_after = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
        published = []
        recover_pending_enrichment_dispatches(
            MAINTENANCE_DATABASE_URL,
            published.append,
        )
        claim = next(item for item in published if item.request_id == request_id)
        tasks.enrich_request.run(
            str(claim.request_id), str(claim.tenant_id), str(claim.user_id)
        )

        with Session(owner_engine) as session:
            request = session.get(EnrichmentRequest, request_id)
            assert request is not None and request.status == "completed"
            assert request.provider_request_id == "accepted-after-rate-limit"
            assert request.dispatch_pending is False
        assert gateway.calls == 2
    finally:
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
        run_candidate = session.get(RunCandidate, run_candidate_id)
        assert run_candidate is not None
        run = session.get(SourcingRun, run_candidate.run_id)
        assert run is not None
        run.state = RunState.READY
        run.current_stage = RunState.READY.value
        run_candidate.enrichment_status = "unavailable"
        session.commit()
        outcome = SourcingService(session, b"test-key").queue_on_demand_enrichment(
            context,
            run_candidate_id,
            idempotency_key="celery-entry",
        )
        assert outcome.claim_token is not None
        request_id = outcome.request.id
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


def test_on_demand_lock_order_avoids_run_candidate_deadlock_and_replays(
    owner_engine: Engine,
) -> None:
    tenant_id, user_id, run_id, run_candidate_id = _seed_celery_entry_run(
        owner_engine, suffix="lock-order"
    )
    context = RequestContext(tenant_id=tenant_id, user_id=user_id, role=Role.OWNER)
    with Session(owner_engine) as session:
        run = session.get(SourcingRun, run_id)
        run_candidate = session.get(RunCandidate, run_candidate_id)
        assert run is not None and run_candidate is not None
        run.state = RunState.READY
        run.current_stage = RunState.READY.value
        run_candidate.enrichment_status = "unavailable"
        session.commit()

    run_locked = threading.Event()
    candidate_read = threading.Event()
    automatic_complete = threading.Event()
    outcomes: queue.Queue[object] = queue.Queue()

    class SignallingSession(Session):
        observed_candidate = False

        def scalar(self, statement, *args, **kwargs):  # type: ignore[no-untyped-def]
            result = super().scalar(statement, *args, **kwargs)
            if not self.observed_candidate:
                self.observed_candidate = True
                candidate_read.set()
                if not automatic_complete.wait(timeout=5):
                    raise TimeoutError("automatic lock holder did not complete")
            return result

    def automatic_worker() -> None:
        try:
            with Session(owner_engine) as session:
                session.execute(text("SET LOCAL lock_timeout = '2s'"))
                locked_run = session.scalar(
                    select(SourcingRun)
                    .where(SourcingRun.id == run_id)
                    .with_for_update()
                )
                assert locked_run is not None
                run_locked.set()
                assert candidate_read.wait(timeout=5)
                locked_candidate = session.scalar(
                    select(RunCandidate)
                    .where(RunCandidate.id == run_candidate_id)
                    .with_for_update()
                )
                assert locked_candidate is not None
                locked_candidate.enrichment_status = "unavailable"
                session.commit()
                outcomes.put("automatic-complete")
        except Exception as error:  # noqa: BLE001 - worker errors are asserted centrally
            outcomes.put(error)
        finally:
            automatic_complete.set()

    def on_demand_worker() -> None:
        try:
            assert run_locked.wait(timeout=5)
            with SignallingSession(
                bind=owner_engine, expire_on_commit=False
            ) as session:
                outcome = SourcingService(
                    session, b"test-key"
                ).queue_on_demand_enrichment(
                    context,
                    run_candidate_id,
                    idempotency_key="lock-order-replay",
                )
                session.commit()
                outcomes.put((outcome.request.id, outcome.claim_token))
        except Exception as error:  # noqa: BLE001 - worker errors are asserted centrally
            outcomes.put(error)

    threads = [
        threading.Thread(target=automatic_worker),
        threading.Thread(target=on_demand_worker),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=8)
    assert all(not thread.is_alive() for thread in threads)
    observed = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert not [item for item in observed if isinstance(item, BaseException)]
    request_id, claim_token = next(item for item in observed if isinstance(item, tuple))
    assert claim_token is not None

    with Session(owner_engine, expire_on_commit=False) as session:
        replay = SourcingService(session, b"test-key").queue_on_demand_enrichment(
            context,
            run_candidate_id,
            idempotency_key="lock-order-replay",
        )
        session.commit()
    assert replay.request.id == request_id
    assert replay.claim_token is None
