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
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from alembic import command
from app.audit.models import AuditEvent
from app.candidates.contacts import ContactCipher
from app.candidates.models import Candidate, ContactPoint, SourceIdentity
from app.candidates.service import CandidateService
from app.clients.models import ClientCompany
from app.core.config import MaintenanceSettings
from app.core.database import Base
from app.crm.models import JobCandidate  # noqa: F401
from app.identity.dependencies import apply_tenant_context
from app.identity.models import Tenant, User
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardVersion
from app.privacy import tasks as privacy_tasks
from app.privacy.models import PrivacyDeletionSnapshotTarget, PrivacyRequest
from app.privacy.schemas import PrivacyRequestType
from app.privacy.service import PrivacyError, PrivacyService, SuppressionService
from app.providers.base import ProviderPerson
from app.sourcing.models import (
    EnrichmentRequest,
    ProviderSnapshot,
    SourcingRun,
    TenantNotification,
)
from app.sourcing.state_machine import RunState

OWNER_DATABASE_URL = os.getenv("TASK11_OWNER_DATABASE_URL")
API_DATABASE_URL = os.getenv("TASK11_API_DATABASE_URL")
MAINTENANCE_DATABASE_URL = os.getenv("TASK11_MAINTENANCE_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not OWNER_DATABASE_URL or not API_DATABASE_URL or not MAINTENANCE_DATABASE_URL,
    reason="Task 11 PostgreSQL URLs are not configured",
)

_PRIVACY_TABLES = (
    "privacy_requests",
    "privacy_request_checkpoints",
    "suppression_identifiers",
    "privacy_deletion_snapshot_targets",
)


def _config() -> Config:
    return Config("alembic.ini")


@pytest.fixture(scope="module")
def owner_engine() -> Generator[Engine, None, None]:
    assert OWNER_DATABASE_URL is not None
    engine = create_engine(OWNER_DATABASE_URL)
    command.upgrade(_config(), "head")
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
        _grant_test_privileges(connection)
        _cleanup(connection)
    yield engine
    with engine.begin() as connection:
        _cleanup(connection)
    engine.dispose()


def test_0010_upgrade_downgrade_upgrade_and_model_parity(
    owner_engine: Engine,
) -> None:
    command.downgrade(_config(), "0009_crm")
    assert not set(_PRIVACY_TABLES) & set(inspect(owner_engine).get_table_names())
    command.upgrade(_config(), "head")

    with owner_engine.begin() as connection:
        _grant_test_privileges(connection)
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0012_enrich_dispatch_recovery"
        )
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )


def test_privacy_tables_force_rls_with_check_and_suppression_is_append_only(
    owner_engine: Engine,
) -> None:
    with owner_engine.connect() as connection:
        flags = connection.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname = ANY(:tables) ORDER BY relname"
            ),
            {"tables": list(_PRIVACY_TABLES)},
        ).all()
        policies = connection.execute(
            text(
                "SELECT tablename, qual, with_check FROM pg_policies "
                "WHERE tablename = ANY(:tables) AND policyname = 'tenant_isolation' "
                "ORDER BY tablename"
            ),
            {"tables": list(_PRIVACY_TABLES)},
        ).all()
    assert flags == sorted((table, True, True) for table in _PRIVACY_TABLES)
    assert [row.tablename for row in policies] == sorted(_PRIVACY_TABLES)
    assert all(row.qual and row.with_check for row in policies)

    tenant_id, user_id, candidate_id, request_id, suppression_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, slug, created_at) VALUES "
                "(:tenant, :slug, now())"
            ),
            {"tenant": tenant_id, "slug": f"task11-append-{tenant_id}"},
        )
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, oidc_subject, email, display_name, created_at) VALUES "
                "(:user, :subject, 'privacy-owner@example.test', 'Owner', now())"
            ),
            {"user": user_id, "subject": f"task11|append|{user_id}"},
        )
        connection.execute(
            text(
                "INSERT INTO candidates (id, tenant_id, full_name, normalized_name, "
                "normalized_skills, industry_codes, created_at, updated_at) VALUES "
                "(:candidate, :tenant, 'Privacy Candidate', 'privacy candidate', "
                "'[]'::jsonb, '[]'::jsonb, now(), now())"
            ),
            {"candidate": candidate_id, "tenant": tenant_id},
        )
        connection.execute(
            text(
                "INSERT INTO privacy_requests (id, tenant_id, candidate_id, "
                "request_type, state, submitted_by_user_id, created_at, updated_at) "
                "VALUES (:request, :tenant, :candidate, 'Deletion', 'Approved', "
                ":user, now(), now())"
            ),
            {
                "request": request_id,
                "tenant": tenant_id,
                "candidate": candidate_id,
                "user": user_id,
            },
        )
        connection.execute(
            text(
                "INSERT INTO suppression_identifiers (id, tenant_id, "
                "privacy_request_id, identifier_type, key_version, digest, "
                "created_at) VALUES (:id, :tenant, :request, 'email', 'v1', "
                "decode(:digest, 'hex'), now())"
            ),
            {
                "id": suppression_id,
                "tenant": tenant_id,
                "request": request_id,
                "digest": "11" * 32,
            },
        )
    for statement in (
        "UPDATE suppression_identifiers SET key_version = 'v2' WHERE id = :id",
        "DELETE FROM suppression_identifiers WHERE id = :id",
    ):
        with (
            pytest.raises(SQLAlchemyError, match="append-only"),
            owner_engine.begin() as connection,
        ):
            connection.execute(text(statement), {"id": suppression_id})


def test_api_role_rls_hides_cross_tenant_and_rejects_with_check(
    owner_engine: Engine,
) -> None:
    assert API_DATABASE_URL is not None
    first = _seed_candidate(owner_engine, "rls-first")
    second = _seed_candidate(owner_engine, "rls-second")
    api_engine = create_engine(API_DATABASE_URL)
    with Session(api_engine) as session:
        apply_tenant_context(session, first["tenant_id"])
        assert session.get(Candidate, second["candidate_id"]) is None
        session.add(
            PrivacyRequest(
                tenant_id=second["tenant_id"],
                candidate_id=second["candidate_id"],
                request_type=PrivacyRequestType.ACCESS,
                submitted_by_user_id=first["user_id"],
            )
        )
        with pytest.raises(ProgrammingError, match="row-level security"):
            session.flush()
    api_engine.dispose()


def test_api_finalizer_cannot_bypass_tenant_context(owner_engine: Engine) -> None:
    assert API_DATABASE_URL is not None
    first = _seed_candidate(owner_engine, "finalize-first")
    second = _seed_candidate(owner_engine, "finalize-second")
    request_id = uuid4()
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO privacy_requests (id, tenant_id, candidate_id, "
                "request_type, state, submitted_by_user_id, created_at, updated_at) "
                "VALUES (:request, :tenant, :candidate, 'Deletion', 'Executing', "
                ":user, now(), now())"
            ),
            {
                "request": request_id,
                "tenant": second["tenant_id"],
                "candidate": second["candidate_id"],
                "user": second["user_id"],
            },
        )

    api_engine = create_engine(API_DATABASE_URL)
    with Session(api_engine) as session:
        apply_tenant_context(session, first["tenant_id"])
        assert (
            session.scalar(
                text("SELECT privacy_finalize_deletion(:request_id, :tenant_id)"),
                {"request_id": request_id, "tenant_id": second["tenant_id"]},
            )
            is False
        )
        session.commit()
    api_engine.dispose()

    with Session(owner_engine) as session:
        request = session.get(PrivacyRequest, request_id)
        candidate = session.get(Candidate, second["candidate_id"])
        assert request is not None and request.state.value == "Executing"
        assert candidate is not None and candidate.full_name == "Postgres Privacy"


def test_deletion_barrier_wins_concurrent_reimport(owner_engine: Engine) -> None:
    assert API_DATABASE_URL is not None
    seeded = _seed_candidate(owner_engine, "concurrency", create_candidate=False)
    api_engine = create_engine(API_DATABASE_URL)
    context = RequestContext(
        tenant_id=seeded["tenant_id"],
        user_id=seeded["user_id"],
        role=Role.OWNER,
    )
    person = _person("task11-concurrent-provider")
    key = b"privacy-concurrency-key"
    with Session(api_engine, expire_on_commit=False) as session:
        apply_tenant_context(session, context.tenant_id)
        candidate = CandidateService(
            session,
            suppression_service=SuppressionService(session, key),
        ).ingest(context, person)
        session.commit()
        assert candidate.candidate_id is not None

    barrier_established = threading.Event()
    release_deletion = threading.Event()
    reimport_started = threading.Event()
    outcomes: queue.Queue[object] = queue.Queue()

    with Session(api_engine, expire_on_commit=False) as session:
        apply_tenant_context(session, context.tenant_id)
        service = PrivacyService(
            session,
            key,
            ContactCipher(base64.b64encode(b"p" * 32).decode(), key),
        )
        request = service.submit(
            context,
            candidate_id=candidate.candidate_id,
            request_type=PrivacyRequestType.DELETION,
            idempotency_key="concurrent-submit",
        )
        service.verify(context, request.id, idempotency_key="concurrent-verify")
        service.approve(context, request.id, idempotency_key="concurrent-approve")
        session.commit()
        request_id = request.id

    class PausingDeletionSuppression(SuppressionService):
        def persist(self, *args: object, **kwargs: object) -> object:
            rows = super().persist(*args, **kwargs)  # type: ignore[arg-type]
            barrier_established.set()
            assert release_deletion.wait(timeout=5)
            return rows

    def delete_candidate() -> None:
        with Session(api_engine) as session:
            apply_tenant_context(session, context.tenant_id)
            service = PrivacyService(
                session,
                key,
                ContactCipher(base64.b64encode(b"p" * 32).decode(), key),
            )
            service._suppression = PausingDeletionSuppression(session, key)
            service.execute_delete(
                context,
                request_id,
                idempotency_key="concurrent-execute",
            )

    def reimport() -> None:
        with Session(api_engine) as session:
            apply_tenant_context(session, context.tenant_id)
            reimport_started.set()
            result = CandidateService(
                session,
                suppression_service=SuppressionService(session, key),
            ).ingest(context, person)
            session.commit()
            outcomes.put(result)

    deletion_thread = threading.Thread(target=delete_candidate, daemon=True)
    deletion_thread.start()
    assert barrier_established.wait(timeout=5)
    reimport_thread = threading.Thread(target=reimport, daemon=True)
    reimport_thread.start()
    assert reimport_started.wait(timeout=5)
    release_deletion.set()
    deletion_thread.join(timeout=10)
    reimport_thread.join(timeout=10)
    assert not deletion_thread.is_alive()
    assert not reimport_thread.is_alive()
    result = outcomes.get_nowait()
    assert result.suppressed is True

    with Session(api_engine) as session:
        apply_tenant_context(session, context.tenant_id)
        assert session.scalar(select(func.count()).select_from(Candidate)) == 1
        erased = session.get(Candidate, candidate.candidate_id)
        assert erased is not None and erased.full_name == "[deleted]"
    api_engine.dispose()


def test_ingest_shared_gate_then_candidate_lock_does_not_deadlock_deletion(
    owner_engine: Engine,
) -> None:
    assert API_DATABASE_URL is not None
    seeded = _seed_candidate(owner_engine, "lock-order", create_candidate=False)
    api_engine = create_engine(API_DATABASE_URL)
    context = RequestContext(
        tenant_id=seeded["tenant_id"],
        user_id=seeded["user_id"],
        role=Role.OWNER,
    )
    person = _person("task11-lock-order-provider")
    key = b"privacy-lock-order-key"
    cipher = ContactCipher(base64.b64encode(b"l" * 32).decode(), key)
    with Session(api_engine, expire_on_commit=False) as session:
        apply_tenant_context(session, context.tenant_id)
        candidate = CandidateService(
            session,
            suppression_service=SuppressionService(session, key),
        ).ingest(context, person)
        session.commit()
        assert candidate.candidate_id is not None
        apply_tenant_context(session, context.tenant_id)
        service = PrivacyService(session, key, cipher)
        request = service.submit(
            context,
            candidate_id=candidate.candidate_id,
            request_type=PrivacyRequestType.DELETION,
            idempotency_key="lock-submit",
        )
        service.verify(context, request.id, idempotency_key="lock-verify")
        service.approve(context, request.id, idempotency_key="lock-approve")
        session.commit()
        request_id = request.id
        candidate_id = candidate.candidate_id

    shared_gate = threading.Event()
    try_candidate_lock = threading.Event()
    errors: queue.Queue[BaseException] = queue.Queue()

    def in_flight_ingest() -> None:
        try:
            with Session(api_engine) as session:
                apply_tenant_context(session, context.tenant_id)
                suppression = SuppressionService(session, key)
                assert suppression.match_person(context.tenant_id, person) is None
                shared_gate.set()
                assert try_candidate_lock.wait(timeout=5)
                session.scalar(
                    select(Candidate)
                    .where(
                        Candidate.tenant_id == context.tenant_id,
                        Candidate.id == candidate_id,
                    )
                    .with_for_update()
                )
                session.commit()
        except (AssertionError, PrivacyError, RuntimeError, SQLAlchemyError) as error:
            errors.put(error)

    def delete_candidate() -> None:
        try:
            with Session(api_engine) as session:
                apply_tenant_context(session, context.tenant_id)
                PrivacyService(session, key, cipher).execute_delete(
                    context,
                    request_id,
                    idempotency_key="lock-execute",
                )
        except (AssertionError, PrivacyError, RuntimeError, SQLAlchemyError) as error:
            errors.put(error)

    ingest_thread = threading.Thread(target=in_flight_ingest, daemon=True)
    ingest_thread.start()
    assert shared_gate.wait(timeout=5)
    deletion_thread = threading.Thread(target=delete_candidate, daemon=True)
    deletion_thread.start()
    assert deletion_thread.is_alive()
    try_candidate_lock.set()
    ingest_thread.join(timeout=10)
    deletion_thread.join(timeout=10)

    assert not ingest_thread.is_alive()
    assert not deletion_thread.is_alive()
    assert errors.empty(), list(errors.queue)
    with Session(api_engine) as session:
        apply_tenant_context(session, context.tenant_id)
        erased = session.get(Candidate, candidate_id)
        assert erased is not None and erased.full_name == "[deleted]"
    api_engine.dispose()


def test_maintenance_role_is_function_only_and_snapshot_failure_alert_is_durable(
    owner_engine: Engine,
) -> None:
    assert MAINTENANCE_DATABASE_URL is not None
    snapshot_id = _seed_expired_snapshot(owner_engine)
    with owner_engine.connect() as connection:
        privacy_table_grants = connection.execute(
            text(
                "SELECT table_name, privilege_type FROM information_schema."
                "role_table_grants WHERE grantee = 'sourcing_maintenance' "
                "AND table_name = ANY(:tables)"
            ),
            {"tables": list(_PRIVACY_TABLES)},
        ).all()
        routine_grants = {
            row.routine_name
            for row in connection.execute(
                text(
                    "SELECT routine_name FROM information_schema."
                    "role_routine_grants WHERE grantee = 'sourcing_maintenance'"
                )
            )
        }
    assert privacy_table_grants == []
    assert {
        "maintenance_record_snapshot_delete_failure",
        "privacy_due_deletions",
        "privacy_claim_deletion_snapshots",
        "privacy_mark_deletion_snapshot_deleted",
        "privacy_mark_deletion_snapshot_failed",
        "privacy_finalize_deletion",
    } <= routine_grants

    maintenance_engine = create_engine(MAINTENANCE_DATABASE_URL)
    with Session(maintenance_engine) as session:
        with pytest.raises(ProgrammingError, match="permission denied"):
            session.scalars(select(PrivacyRequest.id)).all()
        session.rollback()
        assert session.scalar(
            text(
                "SELECT maintenance_record_snapshot_delete_failure("
                ":snapshot_id, 'object_delete_failed')"
            ),
            {"snapshot_id": snapshot_id},
        )
        assert (
            session.execute(
                text(
                    "SELECT snapshot_id, tenant_id, object_reference FROM "
                    "maintenance_claim_expired_snapshots(10)"
                )
            ).all()
            == []
        )
        session.commit()

    with Session(owner_engine) as session:
        snapshot = session.get(ProviderSnapshot, snapshot_id)
        assert snapshot is not None
        assert snapshot.delete_attempts == 1
        assert snapshot.delete_failure_started_at is not None
        assert session.scalar(select(func.count()).select_from(TenantNotification)) == 0
        snapshot.delete_failure_started_at = datetime.now(UTC) - timedelta(hours=25)
        session.commit()

    with Session(maintenance_engine) as session:
        assert session.scalar(
            text(
                "SELECT maintenance_record_snapshot_delete_failure("
                ":snapshot_id, 'object_delete_failed')"
            ),
            {"snapshot_id": snapshot_id},
        )
        session.commit()
    maintenance_engine.dispose()

    with Session(owner_engine) as session:
        snapshot = session.get(ProviderSnapshot, snapshot_id)
        assert snapshot is not None
        assert snapshot.delete_attempts == 2
        alerts = session.scalars(
            select(TenantNotification).where(
                TenantNotification.code == "snapshot_expiry_failed"
            )
        ).all()
        assert {alert.audience_role for alert in alerts} == {"owner", "admin"}


def test_contact_maintenance_anchors_shorter_policy_to_last_verified_or_used(
    owner_engine: Engine,
) -> None:
    assert MAINTENANCE_DATABASE_URL is not None
    seeded = _seed_candidate(owner_engine, "retention-anchor")
    contact_id = uuid4()
    now = datetime.now(UTC)
    verified_at = now - timedelta(days=40)
    with owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO candidate_contact_points "
                "(id, tenant_id, candidate_id, kind, classification, "
                "verification_state, confidence, provider, lookup_hmac, "
                "value_ciphertext, value_nonce, encrypted_data_key, key_nonce, "
                "schema_version, observed_at, last_verified_at, expires_at, "
                "retention_days, created_at, updated_at) VALUES "
                "(:id, :tenant, :candidate, 'email', 'work', 'unverified', "
                "1, 'apollo', :lookup_hmac, decode('01', 'hex'), "
                "decode('02', 'hex'), decode('03', 'hex'), decode('04', 'hex'), "
                "1, :observed_at, :verified_at, :platform_expiry, 30, now(), now())"
            ),
            {
                "id": contact_id,
                "tenant": seeded["tenant_id"],
                "candidate": seeded["candidate_id"],
                "lookup_hmac": uuid4().hex * 2,
                "observed_at": now,
                "verified_at": verified_at,
                "platform_expiry": verified_at + timedelta(days=180),
            },
        )

    maintenance_engine = create_engine(MAINTENANCE_DATABASE_URL)
    with Session(maintenance_engine) as session:
        session.scalar(text("SELECT maintenance_erase_due_contacts()"))
        session.commit()
    maintenance_engine.dispose()

    with Session(owner_engine) as session:
        point = session.get(ContactPoint, contact_id)
        assert point is not None
        assert point.expired_at is not None
        assert point.value_ciphertext is None
        assert point.encrypted_data_key is None


def test_privacy_deletion_retries_after_object_is_already_missing_without_duplicate_audit(
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, _user_id, candidate_id, request_id = _seed_privacy_snapshot_deletion(
        owner_engine
    )

    class AlreadyMissingObjectStore:
        def __init__(self) -> None:
            self.delete_calls: list[str] = []

        def delete_object(self, **kwargs: object) -> None:
            self.delete_calls.append(str(kwargs["Key"]))

    objects = AlreadyMissingObjectStore()
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: objects)
    settings = MaintenanceSettings.for_test()

    privacy_tasks._run_privacy_deletion(settings, request_id, tenant_id)
    privacy_tasks._run_privacy_deletion(settings, request_id, tenant_id)

    assert len(objects.delete_calls) == 1
    with Session(owner_engine) as session:
        request = session.get(PrivacyRequest, request_id)
        candidate = session.get(Candidate, candidate_id)
        target = session.scalar(
            select(PrivacyDeletionSnapshotTarget).where(
                PrivacyDeletionSnapshotTarget.privacy_request_id == request_id
            )
        )
        assert request is not None and request.state.value == "Completed"
        assert candidate is not None and candidate.full_name == "[deleted]"
        assert target is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(ProviderSnapshot)
                .where(ProviderSnapshot.tenant_id == tenant_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.tenant_id == tenant_id,
                    AuditEvent.action == "privacy.deletion_completed",
                )
            )
            == 1
        )


def _seed_privacy_snapshot_deletion(
    engine: Engine,
) -> tuple[UUID, UUID, UUID, UUID]:
    key = b"privacy-crash-window-key"
    with Session(engine, expire_on_commit=False) as session:
        tenant = Tenant(slug=f"task11-crash-{uuid4()}")
        user = User(
            oidc_subject=f"task11|crash|{uuid4()}",
            email=f"crash-{uuid4()}@example.test",
            display_name="Crash Window Owner",
        )
        session.add_all((tenant, user))
        session.flush()
        candidate = Candidate(
            tenant_id=tenant.id,
            full_name="Crash Window Candidate",
            normalized_name="crash window candidate",
            profile_url="https://www.linkedin.com/in/task11-crash",
            normalized_profile_url="https://linkedin.com/in/task11-crash",
        )
        session.add(candidate)
        session.flush()
        session.add(
            SourceIdentity(
                tenant_id=tenant.id,
                candidate_id=candidate.id,
                provider="apollo",
                provider_person_id="task11-crash-provider",
                source_timestamp=datetime.now(UTC),
                confidence=1,
            )
        )
        client = ClientCompany(
            tenant_id=tenant.id,
            name="Crash Client",
            normalized_name=f"crash-{uuid4()}",
        )
        session.add(client)
        session.flush()
        job = Job(
            tenant_id=tenant.id,
            client_id=client.id,
            owner_user_id=user.id,
            title="Crash Job",
            job_description="Crash recovery probe",
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
        )
        session.add(scorecard)
        session.flush()
        run = SourcingRun(
            tenant_id=tenant.id,
            job_id=job.id,
            scorecard_version_id=scorecard.id,
            started_by_user_id=user.id,
            state=RunState.READY,
            current_stage=RunState.READY.value,
        )
        session.add(run)
        session.flush()
        enrichment = EnrichmentRequest(
            tenant_id=tenant.id,
            run_id=run.id,
            provider="apollo",
            provider_request_id="crash-snapshot",
            candidate_ids=[str(candidate.id)],
            reservation_key=f"crash-snapshot-{uuid4()}",
            status="completed",
        )
        session.add(enrichment)
        session.flush()
        session.add(
            ProviderSnapshot(
                tenant_id=tenant.id,
                run_id=run.id,
                enrichment_request_id=enrichment.id,
                provider="apollo",
                object_reference=f"{tenant.id}/{run.id}/apollo/crash-snapshot",
                checksum_sha256="c" * 64,
                created_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=30),
            )
        )
        session.flush()
        context = RequestContext(
            tenant_id=tenant.id,
            user_id=user.id,
            role=Role.OWNER,
        )
        service = PrivacyService(
            session,
            key,
            ContactCipher(base64.b64encode(b"c" * 32).decode(), key),
        )
        request = service.submit(
            context,
            candidate_id=candidate.id,
            request_type=PrivacyRequestType.DELETION,
            idempotency_key="crash-submit",
        )
        service.verify(context, request.id, idempotency_key="crash-verify")
        service.approve(context, request.id, idempotency_key="crash-approve")
        executing = service.execute_delete(
            context,
            request.id,
            idempotency_key="crash-execute",
        )
        assert executing.state.value == "Executing"
        session.refresh(candidate)
        assert candidate.full_name == "[deleted]"
        assert candidate.profile_url is None
        return tenant.id, user.id, candidate.id, request.id


def test_privacy_snapshot_failure_has_durable_backoff_before_reclaim(
    owner_engine: Engine,
) -> None:
    assert MAINTENANCE_DATABASE_URL is not None
    tenant_id, _user_id, _candidate_id, request_id = _seed_privacy_snapshot_deletion(
        owner_engine
    )
    maintenance_engine = create_engine(MAINTENANCE_DATABASE_URL)
    with Session(maintenance_engine) as session:
        claimed = session.execute(
            text(
                "SELECT target_id, tenant_id, object_reference "
                "FROM privacy_claim_deletion_snapshots("
                ":request_id, :tenant_id, 10)"
            ),
            {"request_id": request_id, "tenant_id": tenant_id},
        ).one()
        assert session.scalar(
            text(
                "SELECT privacy_mark_deletion_snapshot_failed("
                ":target_id, 'object_delete_failed')"
            ),
            {"target_id": claimed.target_id},
        )
        immediate = session.execute(
            text(
                "SELECT target_id, tenant_id, object_reference "
                "FROM privacy_claim_deletion_snapshots("
                ":request_id, :tenant_id, 10)"
            ),
            {"request_id": request_id, "tenant_id": tenant_id},
        ).all()
        session.commit()
    maintenance_engine.dispose()

    assert immediate == []
    with Session(owner_engine) as session:
        target = session.scalar(
            select(PrivacyDeletionSnapshotTarget).where(
                PrivacyDeletionSnapshotTarget.privacy_request_id == request_id
            )
        )
        assert target is not None
        assert target.next_attempt_at is not None
        assert target.last_failure_at is not None
        assert target.next_attempt_at > target.last_failure_at


def test_privacy_snapshot_target_cannot_bridge_to_cross_tenant_object(
    owner_engine: Engine,
) -> None:
    first_tenant, _first_user, _first_candidate, first_request = (
        _seed_privacy_snapshot_deletion(owner_engine)
    )
    second_tenant, _second_user, _second_candidate, second_request = (
        _seed_privacy_snapshot_deletion(owner_engine)
    )
    with Session(owner_engine) as session:
        second_snapshot_id = session.scalar(
            select(PrivacyDeletionSnapshotTarget.snapshot_id).where(
                PrivacyDeletionSnapshotTarget.tenant_id == second_tenant,
                PrivacyDeletionSnapshotTarget.privacy_request_id == second_request,
            )
        )
    assert second_snapshot_id is not None

    with pytest.raises(IntegrityError), owner_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO privacy_deletion_snapshot_targets "
                "(id, tenant_id, privacy_request_id, snapshot_id, status, "
                "delete_attempts, created_at, updated_at) VALUES "
                "(:id, :tenant, :request, :snapshot, 'pending', 0, now(), now())"
            ),
            {
                "id": uuid4(),
                "tenant": first_tenant,
                "request": first_request,
                "snapshot": second_snapshot_id,
            },
        )


def _seed_expired_snapshot(engine: Engine) -> UUID:
    with Session(engine, expire_on_commit=False) as session:
        tenant = Tenant(slug=f"task11-snapshot-{uuid4()}")
        user = User(
            oidc_subject=f"task11|snapshot|{uuid4()}",
            email=f"snapshot-{uuid4()}@example.test",
            display_name="Snapshot Owner",
        )
        session.add_all((tenant, user))
        session.flush()
        client = ClientCompany(
            tenant_id=tenant.id,
            name="Snapshot Client",
            normalized_name=f"snapshot-{uuid4()}",
        )
        session.add(client)
        session.flush()
        job = Job(
            tenant_id=tenant.id,
            client_id=client.id,
            owner_user_id=user.id,
            title="Snapshot Job",
            job_description="Snapshot retention probe",
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
        )
        session.add(scorecard)
        session.flush()
        run = SourcingRun(
            tenant_id=tenant.id,
            job_id=job.id,
            scorecard_version_id=scorecard.id,
            started_by_user_id=user.id,
            state=RunState.READY,
            current_stage=RunState.READY.value,
        )
        session.add(run)
        session.flush()
        request = EnrichmentRequest(
            tenant_id=tenant.id,
            run_id=run.id,
            provider="apollo",
            provider_request_id="snapshot-failure",
            candidate_ids=[],
            reservation_key=f"snapshot-failure-{uuid4()}",
            status="completed",
        )
        session.add(request)
        session.flush()
        snapshot = ProviderSnapshot(
            tenant_id=tenant.id,
            run_id=run.id,
            enrichment_request_id=request.id,
            provider="apollo",
            object_reference=f"{tenant.id}/{run.id}/apollo/snapshot-failure",
            checksum_sha256="f" * 64,
            created_at=datetime.now(UTC) - timedelta(days=31),
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        session.add(snapshot)
        session.commit()
        return snapshot.id


def _person(provider_id: str) -> ProviderPerson:
    return ProviderPerson(
        provider="apollo",
        provider_person_id=provider_id,
        full_name="Concurrency Candidate",
        current_title="Product Manager",
        current_company="Privacy Inc",
        location="New York",
        linkedin_url=f"https://www.linkedin.com/in/{provider_id}",
        experiences=(),
    )


def _seed_candidate(
    engine: Engine,
    suffix: str,
    *,
    create_candidate: bool = True,
) -> dict[str, object]:
    tenant_id, user_id, candidate_id = uuid4(), uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, slug, created_at) VALUES "
                "(:tenant, :slug, now())"
            ),
            {"tenant": tenant_id, "slug": f"task11-{suffix}-{tenant_id}"},
        )
        connection.execute(
            text(
                "INSERT INTO users "
                "(id, oidc_subject, email, display_name, created_at) VALUES "
                "(:user, :subject, :email, 'Owner', now())"
            ),
            {
                "user": user_id,
                "subject": f"task11|{suffix}|{user_id}",
                "email": f"{suffix}-{user_id}@example.test",
            },
        )
        if create_candidate:
            connection.execute(
                text(
                    "INSERT INTO candidates (id, tenant_id, full_name, "
                    "normalized_name, normalized_skills, industry_codes, created_at, "
                    "updated_at) VALUES (:candidate, :tenant, 'Postgres Privacy', "
                    "'postgres privacy', '[]'::jsonb, '[]'::jsonb, now(), now())"
                ),
                {"candidate": candidate_id, "tenant": tenant_id},
            )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "candidate_id": candidate_id,
    }


def _cleanup(connection: Connection) -> None:
    for table in (
        "audit_events",
        "crm_activity_events",
        "crm_acceptance_snapshots",
        "suppression_identifiers",
    ):
        connection.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER ALL"))
    connection.execute(text("DELETE FROM tenants WHERE slug LIKE 'task11-%'"))
    connection.execute(text("DELETE FROM users WHERE oidc_subject LIKE 'task11|%'"))
    for table in (
        "suppression_identifiers",
        "crm_acceptance_snapshots",
        "crm_activity_events",
        "audit_events",
    ):
        connection.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER ALL"))


def _grant_test_privileges(connection: Connection) -> None:
    connection.execute(
        text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
            "IN SCHEMA public TO sourcing_api_test"
        )
    )
    signature = connection.scalar(
        text(
            "SELECT CASE "
            "WHEN to_regprocedure('privacy_finalize_deletion(uuid, uuid)') "
            "IS NOT NULL THEN 'privacy_finalize_deletion(uuid, uuid)' "
            "WHEN to_regprocedure('privacy_finalize_deletion(uuid)') IS NOT NULL "
            "THEN 'privacy_finalize_deletion(uuid)' END"
        )
    )
    if signature is not None:
        connection.execute(
            text(f"GRANT EXECUTE ON FUNCTION {signature} TO sourcing_api_test")
        )
