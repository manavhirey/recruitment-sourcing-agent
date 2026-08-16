import base64
import json
import os
import queue
import threading
from collections.abc import Generator
from uuid import UUID, uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from alembic import command
from app.audit.models import AuditEvent
from app.candidates.contacts import ContactCipher, ContactService
from app.candidates.models import ContactPoint
from app.core.config import Settings
from app.core.database import Base, get_db
from app.crm.models import ActivityEvent, JobCandidate
from app.crm.service import CrmError, CrmService, materialize_run_matches
from app.identity.dependencies import apply_tenant_context
from app.identity.schemas import IdentityClaims, RequestContext, Role
from app.main import create_app
from app.providers.base import ProviderContact
from app.sourcing.models import SourcingRun

OWNER_DATABASE_URL = os.getenv("TASK10_OWNER_DATABASE_URL")
API_DATABASE_URL = os.getenv("TASK10_API_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not OWNER_DATABASE_URL or not API_DATABASE_URL,
    reason="Task 10 PostgreSQL URLs are not configured",
)

_CRM_TABLES = (
    "job_candidates",
    "crm_acceptance_snapshots",
    "candidate_notes",
    "crm_tags",
    "job_candidate_tags",
    "crm_activity_events",
)


def _config() -> Config:
    return Config("alembic.ini")


@pytest.fixture(scope="module")
def owner_engine() -> Generator[Engine, None, None]:
    assert OWNER_DATABASE_URL is not None
    command.upgrade(_config(), "head")
    engine = create_engine(OWNER_DATABASE_URL)
    with engine.begin() as connection:
        _grant_api(connection)
        _cleanup(connection)
    yield engine
    with engine.begin() as connection:
        _cleanup(connection)
    engine.dispose()


def test_0009_upgrade_downgrade_and_model_parity(owner_engine: Engine) -> None:
    command.downgrade(_config(), "0008_retention_maintenance")
    tables = set(inspect(owner_engine).get_table_names())
    assert not set(_CRM_TABLES) & tables
    candidate_columns = {
        column["name"] for column in inspect(owner_engine).get_columns("candidates")
    }
    assert "normalized_skills" not in candidate_columns
    assert "industry_codes" not in candidate_columns

    command.upgrade(_config(), "head")

    with owner_engine.begin() as connection:
        _grant_api(connection)
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0013_provider_connector_state"
        )
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )


def test_crm_tables_force_rls_with_using_and_check_and_activity_is_append_only(
    owner_engine: Engine,
) -> None:
    seeded = _seed_review_row(owner_engine, "rls")
    with owner_engine.connect() as connection:
        flags = connection.execute(
            text(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname = ANY(:tables) ORDER BY relname"
            ),
            {"tables": list(_CRM_TABLES)},
        ).all()
        policies = connection.execute(
            text(
                "SELECT tablename, qual, with_check FROM pg_policies "
                "WHERE tablename = ANY(:tables) AND policyname = 'tenant_isolation' "
                "ORDER BY tablename"
            ),
            {"tables": list(_CRM_TABLES)},
        ).all()
    assert flags == sorted((table, True, True) for table in _CRM_TABLES)
    assert [row.tablename for row in policies] == sorted(_CRM_TABLES)
    assert all(row.qual and row.with_check for row in policies)

    invalid_states = (
        "UPDATE job_candidates SET stage = 'Near Match' WHERE id = :id",
        (
            "UPDATE job_candidates SET stage = 'Rejected', "
            "rejection_reason_code = NULL WHERE id = :id"
        ),
        "UPDATE job_candidates SET rejection_reason_code = 'other' WHERE id = :id",
    )
    for statement in invalid_states:
        with (
            pytest.raises(SQLAlchemyError, match="check constraint"),
            owner_engine.begin() as connection,
        ):
            _set_tenant(connection, seeded["tenant_id"])
            connection.execute(text(statement), {"id": seeded["job_candidate_id"]})

    activity_id = uuid4()
    with owner_engine.begin() as connection:
        _set_tenant(connection, seeded["tenant_id"])
        connection.execute(
            text(
                "INSERT INTO crm_activity_events "
                "(id, tenant_id, job_candidate_id, actor_user_id, event_key, "
                "action, payload, created_at, updated_at) VALUES "
                "(:id, :tenant, :row, :actor, 'append-only', 'candidate.test', "
                "'{}'::json, now(), now())"
            ),
            {
                "id": activity_id,
                "tenant": seeded["tenant_id"],
                "row": seeded["job_candidate_id"],
                "actor": seeded["user_id"],
            },
        )
    for statement in (
        "UPDATE crm_activity_events SET action = 'changed' WHERE id = :id",
        "DELETE FROM crm_activity_events WHERE id = :id",
    ):
        with (
            pytest.raises(SQLAlchemyError, match="append-only"),
            owner_engine.begin() as connection,
        ):
            _set_tenant(connection, seeded["tenant_id"])
            connection.execute(text(statement), {"id": activity_id})

    ready_run_id = uuid4()
    snapshot_id = uuid4()
    with owner_engine.begin() as connection:
        _set_tenant(connection, seeded["tenant_id"])
        connection.execute(
            text(
                "INSERT INTO sourcing_runs "
                "(id, tenant_id, job_id, scorecard_version_id, started_by_user_id, "
                "state, planned_queries, current_stage, cancellation_requested, "
                "candidate_count, matched_count, completed_at, created_at, updated_at) "
                "VALUES (:id, :tenant, :job, :scorecard, :user, 'ready', "
                "CAST(:queries AS json), 'ready', false, 0, 0, now(), now(), now())"
            ),
            {
                "id": ready_run_id,
                "tenant": seeded["tenant_id"],
                "job": seeded["job_id"],
                "scorecard": seeded["scorecard_id"],
                "user": seeded["user_id"],
                "queries": "[]",
            },
        )
        connection.execute(
            text(
                "INSERT INTO crm_acceptance_snapshots "
                "(id, tenant_id, job_id, run_id, finalized_by_user_id, ready_at, "
                "finalized_at, denominator, accepted_count, reviewed_count, "
                "shortlisted_count, new_count, rejected_count, cohort_candidate_ids, "
                "created_at) VALUES (:id, :tenant, :job, :run, :user, now(), now(), "
                "20, 0, 0, 0, 0, 0, CAST(:cohort AS json), now())"
            ),
            {
                "id": snapshot_id,
                "tenant": seeded["tenant_id"],
                "job": seeded["job_id"],
                "run": ready_run_id,
                "user": seeded["user_id"],
                "cohort": "[]",
            },
        )
    for statement in (
        "UPDATE crm_acceptance_snapshots SET accepted_count = 1 WHERE id = :id",
        "DELETE FROM crm_acceptance_snapshots WHERE id = :id",
    ):
        with (
            pytest.raises(SQLAlchemyError, match="append-only"),
            owner_engine.begin() as connection,
        ):
            _set_tenant(connection, seeded["tenant_id"])
            connection.execute(text(statement), {"id": snapshot_id})


def test_crm_rls_and_client_grants_hide_cross_tenant_and_ungranted_candidates(
    owner_engine: Engine,
) -> None:
    assert API_DATABASE_URL is not None
    granted = _seed_review_row(owner_engine, "granted")
    hidden_same_tenant = _seed_review_row(
        owner_engine,
        "hidden-client",
        tenant_id=granted["tenant_id"],
        user_id=granted["user_id"],
    )
    other_tenant = _seed_review_row(owner_engine, "other-tenant")
    api_engine = create_engine(API_DATABASE_URL)
    context = RequestContext(
        tenant_id=granted["tenant_id"],
        user_id=granted["user_id"],
        role=Role.RECRUITER,
        allowed_client_ids=frozenset((granted["client_id"],)),
    )
    with Session(api_engine) as session:
        apply_tenant_context(session, context.tenant_id)
        assert session.get(JobCandidate, other_tenant["job_candidate_id"]) is None
        service = CrmService(session, b"postgres-client-grants")
        candidates, _ = service.directory(
            context,
            query="Postgres Candidate",
            location=None,
            industry=None,
            cursor=None,
            limit=50,
        )
        assert [candidate.id for candidate in candidates] == [granted["candidate_id"]]
        hidden_search, _ = service.directory(
            context,
            query="hidden-client",
            location=None,
            industry=None,
            cursor=None,
            limit=50,
        )
        assert hidden_search == []
        with pytest.raises(CrmError, match="candidate_not_found"):
            service.candidate_jobs(context, hidden_same_tenant["candidate_id"])
        with pytest.raises(ProgrammingError, match="row-level security"):
            session.execute(
                text("UPDATE job_candidates SET tenant_id = :other WHERE id = :row"),
                {
                    "other": other_tenant["tenant_id"],
                    "row": granted["job_candidate_id"],
                },
            )
    api_engine.dispose()


def test_postgres_full_text_and_trigram_indexes_execute_for_canonical_facts(
    owner_engine: Engine,
) -> None:
    seeded = _seed_review_row(
        owner_engine,
        "search",
        normalized_skills=["payment processing", "sql"],
    )
    source_identity_id = uuid4()
    experience_id = uuid4()
    with owner_engine.begin() as connection:
        _set_tenant(connection, seeded["tenant_id"])
        connection.execute(
            text(
                "INSERT INTO candidate_source_identities "
                "(id, tenant_id, candidate_id, provider, provider_person_id, "
                "source_timestamp, confidence, first_seen_at, last_seen_at) VALUES "
                "(:id, :tenant, :candidate, 'apollo', :provider_id, now(), 1, "
                "now(), now())"
            ),
            {
                "id": source_identity_id,
                "tenant": seeded["tenant_id"],
                "candidate": seeded["candidate_id"],
                "provider_id": f"task10-search-{source_identity_id}",
            },
        )
        connection.execute(
            text(
                "INSERT INTO candidate_experiences "
                "(id, tenant_id, candidate_id, source_identity_id, position, title, "
                "company_name, provider, source_timestamp, observed_value_hash, "
                "confidence) VALUES (:id, :tenant, :candidate, :source, 0, "
                "'Director of Treasury Systems', 'Atlas Payments', 'apollo', now(), "
                ":observed_hash, 1)"
            ),
            {
                "id": experience_id,
                "tenant": seeded["tenant_id"],
                "candidate": seeded["candidate_id"],
                "source": source_identity_id,
                "observed_hash": "e" * 64,
            },
        )
    with owner_engine.connect() as connection:
        _set_tenant(connection, seeded["tenant_id"])
        connection.execute(text("SET enable_seqscan = off"))
        fts_plan = "\n".join(
            row[0]
            for row in connection.execute(
                text(
                    "EXPLAIN (COSTS OFF) SELECT id FROM candidates WHERE "
                    "to_tsvector('simple'::regconfig, "
                    "coalesce(normalized_name, '') || ' ' || "
                    "coalesce(normalized_title, '') || ' ' || "
                    "coalesce(normalized_company, '') || ' ' || "
                    "normalized_skills::text) @@ "
                    "websearch_to_tsquery('simple'::regconfig, 'payment processing')"
                )
            )
        )
        trgm_plan = "\n".join(
            row[0]
            for row in connection.execute(
                text(
                    "EXPLAIN (COSTS OFF) SELECT id FROM candidates "
                    "WHERE normalized_name % 'postgres candidate serch'"
                )
            )
        )
        experience_fts_plan = "\n".join(
            row[0]
            for row in connection.execute(
                text(
                    "EXPLAIN (COSTS OFF) SELECT id FROM candidate_experiences WHERE "
                    "to_tsvector('simple'::regconfig, coalesce(title, '') || ' ' || "
                    "coalesce(company_name, '')) @@ "
                    "websearch_to_tsquery('simple'::regconfig, 'treasury systems')"
                )
            )
        )
        experience_trgm_plan = "\n".join(
            row[0]
            for row in connection.execute(
                text(
                    "EXPLAIN (COSTS OFF) SELECT id FROM candidate_experiences "
                    "WHERE title % 'director of treasury systms'"
                )
            )
        )
        matches = (
            connection.execute(
                text(
                    "SELECT id FROM candidates WHERE "
                    "to_tsvector('simple'::regconfig, normalized_skills::text) @@ "
                    "websearch_to_tsquery('simple'::regconfig, 'payment processing')"
                )
            )
            .scalars()
            .all()
        )
    assert "ix_candidates_search_fts" in fts_plan
    assert "ix_candidates_normalized_name_trgm" in trgm_plan
    assert "ix_candidate_experiences_search_fts" in experience_fts_plan
    assert "ix_candidate_experiences_title_trgm" in experience_trgm_plan
    assert seeded["candidate_id"] in matches

    assert API_DATABASE_URL is not None
    api_engine = create_engine(API_DATABASE_URL)
    context = RequestContext(
        tenant_id=seeded["tenant_id"],
        user_id=seeded["user_id"],
        role=Role.OWNER,
    )
    with Session(api_engine) as session:
        apply_tenant_context(session, context.tenant_id)
        candidates, _ = CrmService(session, b"postgres-search").directory(
            context,
            query="director of treasury systms",
            location=None,
            industry=None,
            cursor=None,
            limit=10,
        )
        assert [candidate.id for candidate in candidates] == [seeded["candidate_id"]]
    api_engine.dispose()


def test_concurrent_match_materialization_creates_one_row_and_one_event(
    owner_engine: Engine,
) -> None:
    assert API_DATABASE_URL is not None
    seeded = _seed_review_row(owner_engine, "concurrency", include_match=True)
    api_engine = create_engine(API_DATABASE_URL)
    context = RequestContext(
        tenant_id=seeded["tenant_id"],
        user_id=seeded["user_id"],
        role=Role.OWNER,
    )
    barrier = threading.Barrier(2)
    outcomes: queue.Queue[object] = queue.Queue()

    def materialize() -> None:
        with Session(api_engine) as session:
            apply_tenant_context(session, context.tenant_id)
            run = session.get(SourcingRun, seeded["run_id"])
            assert run is not None
            barrier.wait(timeout=5)
            try:
                result = materialize_run_matches(session, run, context)
                result_id = result[0].id
                session.commit()
            except SQLAlchemyError as error:
                session.rollback()
                outcomes.put(error)
            else:
                outcomes.put(result_id)

    threads = [threading.Thread(target=materialize) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    results = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert not any(isinstance(result, Exception) for result in results), results
    assert len(set(results)) == 1
    with Session(api_engine) as session:
        apply_tenant_context(session, context.tenant_id)
        assert session.scalar(select(func.count()).select_from(JobCandidate)) == 1
        assert session.scalar(select(func.count()).select_from(ActivityEvent)) == 1
    api_engine.dispose()


def test_postgres_api_reveal_and_streaming_export_preserve_replay_and_redaction(
    owner_engine: Engine,
) -> None:
    assert API_DATABASE_URL is not None
    seeded = _seed_review_row(owner_engine, "api-export")
    with owner_engine.begin() as connection:
        _set_tenant(connection, seeded["tenant_id"])
        identity = connection.execute(
            text("SELECT oidc_subject, email, display_name FROM users WHERE id = :id"),
            {"id": seeded["user_id"]},
        ).one()
        connection.execute(
            text(
                "INSERT INTO memberships "
                "(id, tenant_id, user_id, role, allowed_client_ids, active, created_at) "
                "VALUES (:id, :tenant, :user, 'recruiter', CAST(:clients AS json), "
                "true, now())"
            ),
            {
                "id": uuid4(),
                "tenant": seeded["tenant_id"],
                "user": seeded["user_id"],
                "clients": json.dumps([str(seeded["client_id"])]),
            },
        )
        connection.execute(
            text("UPDATE job_candidates SET stage = 'Shortlisted' WHERE id = :id"),
            {"id": seeded["job_candidate_id"]},
        )

    api_engine = create_engine(API_DATABASE_URL)
    context = RequestContext(
        tenant_id=seeded["tenant_id"],
        user_id=seeded["user_id"],
        role=Role.RECRUITER,
        allowed_client_ids=frozenset((seeded["client_id"],)),
    )
    cipher = ContactCipher(base64.b64encode(b"p" * 32).decode(), b"task10-pg")
    with Session(api_engine, expire_on_commit=False) as session:
        apply_tenant_context(session, context.tenant_id)
        contact = (
            ContactService(session, cipher)
            .store(
                context,
                seeded["candidate_id"],
                ProviderContact(
                    kind="email",
                    value="postgres-export@example.test",
                    classification="work",
                    verification_state="verified",
                ),
            )
            .contact_point
        )
        contact_id = contact.id
        session.commit()

    settings = Settings.for_test()
    app = create_app(settings, contact_cipher=cipher)

    class Verifier:
        def verify(self, token: str) -> IdentityClaims:
            return IdentityClaims(
                subject=identity.oidc_subject,
                email=identity.email,
                name=identity.display_name,
                email_verified=True,
            )

    app.state.token_verifier = Verifier()

    def database_session() -> Generator[Session, None, None]:
        with Session(api_engine, expire_on_commit=False) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    app.dependency_overrides[get_db] = database_session
    headers = {
        "Authorization": "Bearer postgres-api",
        "X-Tenant-ID": str(seeded["tenant_id"]),
    }
    with TestClient(app) as client:
        reveal_headers = {**headers, "Idempotency-Key": "postgres-reveal"}
        first = client.post(
            f"/api/v1/contact-points/{contact_id}/reveal", headers=reveal_headers
        )
        assert first.status_code == 200
        assert first.json()["value"] == "postgres-export@example.test"
        with Session(api_engine) as session:
            apply_tenant_context(session, context.tenant_id)
            first_point = session.get(ContactPoint, contact_id)
            assert first_point is not None
            first_used_at = first_point.last_used_at
            first_expires_at = first_point.expires_at
        replay = client.post(
            f"/api/v1/contact-points/{contact_id}/reveal", headers=reveal_headers
        )
        assert replay.json() == first.json()
        with Session(api_engine) as session:
            apply_tenant_context(session, context.tenant_id)
            replayed_point = session.get(ContactPoint, contact_id)
            assert replayed_point is not None
            assert replayed_point.last_used_at == first_used_at
            assert replayed_point.expires_at == first_expires_at
        exported = client.get(
            f"/api/v1/jobs/{seeded['job_id']}/export.csv",
            headers={**headers, "Idempotency-Key": "postgres-export"},
        )
        with Session(api_engine) as session:
            apply_tenant_context(session, context.tenant_id)
            exported_point = session.get(ContactPoint, contact_id)
            assert exported_point is not None
            exported_used_at = exported_point.last_used_at
            exported_expires_at = exported_point.expires_at
        replayed_export = client.get(
            f"/api/v1/jobs/{seeded['job_id']}/export.csv",
            headers={**headers, "Idempotency-Key": "postgres-export"},
        )
        assert exported.status_code == replayed_export.status_code == 200
        assert exported.text == replayed_export.text
        assert "postgres-export@example.test" in exported.text
        assert "ciphertext" not in exported.text.casefold()
        assert "raw_snapshot" not in exported.text.casefold()
        assert "content-length" not in exported.headers

    with Session(api_engine) as session:
        apply_tenant_context(session, context.tenant_id)
        point = session.get(ContactPoint, contact_id)
        assert point is not None
        assert point.last_used_at == exported_used_at
        assert point.expires_at == exported_expires_at
        assert (
            session.scalar(
                select(func.count())
                .select_from(ActivityEvent)
                .where(ActivityEvent.action == "candidate.contact_revealed")
            )
            == 1
        )
        audit_counts = dict(
            session.execute(
                select(AuditEvent.action, func.count())
                .where(
                    AuditEvent.action.in_(
                        (
                            "candidate.shortlist_export_started",
                            "candidate.contact_exported",
                            "candidate.shortlist_export_completed",
                        )
                    )
                )
                .group_by(AuditEvent.action)
            ).all()
        )
        assert audit_counts == {
            "candidate.shortlist_export_started": 1,
            "candidate.contact_exported": 1,
            "candidate.shortlist_export_completed": 1,
        }
    api_engine.dispose()


def _seed_review_row(
    engine: Engine,
    marker: str,
    *,
    tenant_id: UUID | None = None,
    user_id: UUID | None = None,
    normalized_skills: list[str] | None = None,
    include_match: bool = False,
) -> dict[str, UUID]:
    ids = {
        "tenant_id": tenant_id or uuid4(),
        "user_id": user_id or uuid4(),
        "client_id": uuid4(),
        "job_id": uuid4(),
        "scorecard_id": uuid4(),
        "candidate_id": uuid4(),
        "job_candidate_id": uuid4(),
        "run_id": uuid4(),
        "run_candidate_id": uuid4(),
    }
    with engine.begin() as connection:
        _set_tenant(connection, ids["tenant_id"])
        if tenant_id is None:
            connection.execute(
                text(
                    "INSERT INTO tenants (id, slug, created_at) "
                    "VALUES (:id, :slug, now())"
                ),
                {"id": ids["tenant_id"], "slug": f"task10-{marker}-{uuid4()}"},
            )
        if user_id is None:
            connection.execute(
                text(
                    "INSERT INTO users (id, oidc_subject, email, display_name, created_at) "
                    "VALUES (:id, :subject, :email, 'Task 10', now())"
                ),
                {
                    "id": ids["user_id"],
                    "subject": f"task10|{marker}|{uuid4()}",
                    "email": f"{uuid4()}@example.test",
                },
            )
        connection.execute(
            text(
                "INSERT INTO client_companies "
                "(id, tenant_id, name, normalized_name, created_at) "
                "VALUES (:id, :tenant, :name, :normalized, now())"
            ),
            {
                "id": ids["client_id"],
                "tenant": ids["tenant_id"],
                "name": f"Client {marker}",
                "normalized": f"client-{marker}-{uuid4()}",
            },
        )
        connection.execute(
            text(
                "INSERT INTO jobs "
                "(id, tenant_id, client_id, owner_user_id, title, job_description, "
                "status, draft_revision, draft_extraction_status, created_at, updated_at) "
                "VALUES (:id, :tenant, :client, :user, :title, 'Description', "
                "'awaiting_scorecard', 0, 'ready', now(), now())"
            ),
            {
                "id": ids["job_id"],
                "tenant": ids["tenant_id"],
                "client": ids["client_id"],
                "user": ids["user_id"],
                "title": f"Job {marker}",
            },
        )
        connection.execute(
            text(
                "INSERT INTO scorecard_versions "
                "(id, tenant_id, job_id, version, target_titles, seniority, "
                "locations, industry_code, suggested_adjacent_industries, "
                "uncertainties, extraction_status, confirmed_by_user_id, confirmed_at) "
                "VALUES (:id, :tenant, :job, 1, '[]'::json, '[]'::json, "
                "'[]'::json, 'technology.fintech', '[]'::json, '[]'::json, "
                "'ready', :user, now())"
            ),
            {
                "id": ids["scorecard_id"],
                "tenant": ids["tenant_id"],
                "job": ids["job_id"],
                "user": ids["user_id"],
            },
        )
        connection.execute(
            text("UPDATE jobs SET current_scorecard_id = :scorecard WHERE id = :job"),
            {"scorecard": ids["scorecard_id"], "job": ids["job_id"]},
        )
        connection.execute(
            text(
                "INSERT INTO candidates "
                "(id, tenant_id, full_name, normalized_name, normalized_title, "
                "normalized_company, normalized_skills, industry_codes, "
                "created_at, updated_at) VALUES "
                "(:id, :tenant, :name, :normalized, 'product manager', :company, "
                "CAST(:skills AS jsonb), '[\"technology.fintech\"]'::jsonb, now(), now())"
            ),
            {
                "id": ids["candidate_id"],
                "tenant": ids["tenant_id"],
                "name": f"Postgres Candidate {marker}",
                "normalized": f"postgres candidate {marker}",
                "company": marker,
                "skills": __import__("json").dumps(normalized_skills or []),
            },
        )
        if include_match:
            connection.execute(
                text(
                    "INSERT INTO sourcing_runs "
                    "(id, tenant_id, job_id, scorecard_version_id, started_by_user_id, "
                    "state, planned_queries, current_stage, cancellation_requested, "
                    "candidate_count, matched_count, created_at, updated_at) VALUES "
                    "(:id, :tenant, :job, :scorecard, :user, 'matching', '[]'::json, "
                    "'matching', false, 1, 1, now(), now())"
                ),
                {
                    "id": ids["run_id"],
                    "tenant": ids["tenant_id"],
                    "job": ids["job_id"],
                    "scorecard": ids["scorecard_id"],
                    "user": ids["user_id"],
                },
            )
            connection.execute(
                text(
                    "INSERT INTO run_candidates "
                    "(id, tenant_id, run_id, candidate_id, scorecard_version_id, "
                    "match_score, classification, evidence, scoring_version, created_at, "
                    "matched_at, enrichment_status) VALUES "
                    "(:id, :tenant, :run, :candidate, :scorecard, 88, 'main', "
                    "CAST(:evidence AS json), 'matching-v1', now(), now(), "
                    "'not_requested')"
                ),
                {
                    "id": ids["run_candidate_id"],
                    "tenant": ids["tenant_id"],
                    "run": ids["run_id"],
                    "candidate": ids["candidate_id"],
                    "scorecard": ids["scorecard_id"],
                    "evidence": '{"total": 88}',
                },
            )
        else:
            connection.execute(
                text(
                    "INSERT INTO job_candidates "
                    "(id, tenant_id, job_id, candidate_id, classification, score, "
                    "score_json, scorecard_version_id, scoring_version, stage, "
                    "created_at, updated_at) VALUES "
                    "(:id, :tenant, :job, :candidate, 'main', 80, "
                    "CAST(:score_json AS json), "
                    ":scorecard, 'matching-v1', 'New', now(), now())"
                ),
                {
                    "id": ids["job_candidate_id"],
                    "tenant": ids["tenant_id"],
                    "job": ids["job_id"],
                    "candidate": ids["candidate_id"],
                    "score_json": '{"total": 80}',
                    "scorecard": ids["scorecard_id"],
                },
            )
    return ids


def _set_tenant(connection, tenant_id: UUID) -> None:
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant, true)"),
        {"tenant": str(tenant_id)},
    )


def _grant_api(connection) -> None:
    connection.execute(
        text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
            "IN SCHEMA public TO sourcing_api_test"
        )
    )


def _cleanup(connection) -> None:
    connection.execute(
        text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
    )
    connection.execute(
        text(
            "ALTER TABLE crm_acceptance_snapshots "
            "DISABLE TRIGGER crm_acceptance_snapshots_append_only"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE crm_activity_events "
            "DISABLE TRIGGER crm_activity_events_append_only"
        )
    )
    connection.execute(text("DELETE FROM tenants WHERE slug LIKE 'task10-%'"))
    connection.execute(text("DELETE FROM users WHERE oidc_subject LIKE 'task10|%'"))
    connection.execute(
        text(
            "ALTER TABLE crm_activity_events "
            "ENABLE TRIGGER crm_activity_events_append_only"
        )
    )
    connection.execute(
        text(
            "ALTER TABLE crm_acceptance_snapshots "
            "ENABLE TRIGGER crm_acceptance_snapshots_append_only"
        )
    )
    connection.execute(
        text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only")
    )
