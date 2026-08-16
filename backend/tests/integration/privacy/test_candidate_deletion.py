import base64
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.audit.models import AuditEvent
from app.candidates.contacts import ContactCipher, ContactService
from app.candidates.models import Candidate, ContactPoint, SourceIdentity
from app.candidates.service import CandidateService
from app.clients.models import ClientCompany
from app.core.config import Settings
from app.core.database import Base, get_db
from app.crm.models import JobCandidate
from app.identity.dependencies import get_request_context
from app.identity.models import Tenant, User
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardVersion
from app.main import create_app
from app.privacy.models import PrivacyRequest, SuppressionIdentifier
from app.privacy.schemas import PrivacyRequestState, PrivacyRequestType
from app.privacy.service import PrivacyService, SuppressionService
from app.providers.base import ProviderContact, ProviderPerson


def _person() -> ProviderPerson:
    return ProviderPerson(
        provider="apollo",
        provider_person_id="apollo-private-1",
        full_name="Priya Sharma",
        current_title="Senior Product Manager",
        current_company="Private Payments",
        location="New York",
        linkedin_url="https://www.linkedin.com/in/priya-private?trk=search",
        experiences=(),
        contacts=(
            ProviderContact(
                kind="email",
                value="Priya.Private@Example.test",
                verification_state="verified",
            ),
        ),
    )


def test_deletion_erases_personal_data_blocks_reimport_and_replays_once() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    key = b"privacy-suppression-key"
    cipher = ContactCipher(base64.b64encode(b"d" * 32).decode(), key)
    with Session(engine, expire_on_commit=False) as session:
        tenant = Tenant(slug=f"privacy-delete-{uuid4()}")
        owner = User(
            oidc_subject=f"privacy-owner|{uuid4()}",
            email="owner@example.test",
            display_name="Privacy Owner",
        )
        session.add_all((tenant, owner))
        session.flush()
        context = RequestContext(
            tenant_id=tenant.id,
            user_id=owner.id,
            role=Role.OWNER,
        )
        suppression = SuppressionService(session, key)
        candidate_service = CandidateService(session, suppression_service=suppression)
        ingested = candidate_service.ingest(
            context,
            _person(),
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert ingested.candidate_id is not None
        ContactService(session, cipher).store(
            context,
            ingested.candidate_id,
            _person().contacts[0],
            processed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.commit()

        service = PrivacyService(session, key, cipher)
        request = service.submit(
            context,
            candidate_id=ingested.candidate_id,
            request_type=PrivacyRequestType.DELETION,
            idempotency_key="delete-submit",
        )
        service.verify(context, request.id, idempotency_key="delete-verify")
        service.approve(context, request.id, idempotency_key="delete-approve")
        completed = service.execute_delete(
            context,
            request.id,
            idempotency_key="delete-execute",
        )
        replay = service.execute_delete(
            context,
            request.id,
            idempotency_key="delete-execute",
        )

        assert completed.state is PrivacyRequestState.COMPLETED
        assert replay.id == completed.id
        candidate = session.get(Candidate, ingested.candidate_id)
        assert candidate is not None
        assert candidate.full_name == "[deleted]"
        assert candidate.current_title is None
        assert candidate.current_company is None
        assert candidate.location is None
        assert candidate.profile_url is None
        assert candidate.normalized_skills == []
        assert session.scalar(select(func.count()).select_from(SourceIdentity)) == 0
        assert session.scalar(select(func.count()).select_from(ContactPoint)) == 0

        rows = session.scalars(select(SuppressionIdentifier)).all()
        assert {row.identifier_type for row in rows} == {
            "email",
            "profile_url",
            "provider_id:apollo",
        }
        serialized = "|".join(
            f"{row.identifier_type}:{row.digest.hex()}:{row.key_version}"
            for row in rows
        ).casefold()
        assert "priya" not in serialized
        assert "apollo-private-1" not in serialized

        suppressed = candidate_service.ingest(context, _person())
        assert suppressed.suppressed is True
        assert suppressed.candidate_id is None
        assert session.scalar(select(func.count()).select_from(Candidate)) == 1
        audit_rows = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "candidate.import_suppressed")
        ).all()
        assert len(audit_rows) == 1
        assert "priya" not in str(audit_rows[0].payload).casefold()
        assert "apollo-private-1" not in str(audit_rows[0].payload)
        assert session.scalar(select(func.count()).select_from(PrivacyRequest)) == 1

    engine.dispose()


def test_deletion_of_missing_or_already_erased_candidate_is_replay_safe() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    key = b"privacy-suppression-key"
    cipher = ContactCipher(base64.b64encode(b"m" * 32).decode(), key)
    with Session(engine) as session:
        tenant = Tenant(slug=f"privacy-missing-{uuid4()}")
        owner = User(
            oidc_subject=f"privacy-missing-owner|{uuid4()}",
            email="owner@example.test",
            display_name="Privacy Owner",
        )
        candidate = Candidate(
            tenant_id=tenant.id,
            full_name="[deleted]",
            normalized_name=f"deleted-{uuid4()}",
        )
        session.add_all((tenant, owner))
        session.flush()
        candidate.tenant_id = tenant.id
        session.add(candidate)
        session.flush()
        context = RequestContext(
            tenant_id=tenant.id,
            user_id=owner.id,
            role=Role.OWNER,
        )
        service = PrivacyService(session, key, cipher)
        request = service.submit(
            context,
            candidate_id=candidate.id,
            request_type=PrivacyRequestType.DELETION,
            idempotency_key="missing-submit",
        )
        service.verify(context, request.id, idempotency_key="missing-verify")
        service.approve(context, request.id, idempotency_key="missing-approve")

        assert (
            service.execute_delete(
                context,
                request.id,
                idempotency_key="missing-execute",
            ).state
            is PrivacyRequestState.COMPLETED
        )

    engine.dispose()


@pytest.fixture
def privacy_api() -> Generator[dict[str, Any], None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        tenant = Tenant(slug=f"privacy-api-{uuid4()}")
        other_tenant = Tenant(slug=f"privacy-api-other-{uuid4()}")
        recruiter = User(
            oidc_subject=f"privacy-recruiter|{uuid4()}",
            email="recruiter@example.test",
            display_name="Recruiter",
        )
        owner = User(
            oidc_subject=f"privacy-owner-api|{uuid4()}",
            email="owner@example.test",
            display_name="Owner",
        )
        session.add_all((tenant, other_tenant, recruiter, owner))
        session.flush()
        granted = ClientCompany(
            tenant_id=tenant.id,
            name="Granted",
            normalized_name=f"granted-{uuid4()}",
        )
        hidden = ClientCompany(
            tenant_id=tenant.id,
            name="Hidden",
            normalized_name=f"hidden-{uuid4()}",
        )
        session.add_all((granted, hidden))
        session.flush()
        jobs = [
            Job(
                tenant_id=tenant.id,
                client_id=client.id,
                owner_user_id=owner.id,
                title=client.name,
                job_description="Privacy route fixture",
            )
            for client in (granted, hidden)
        ]
        session.add_all(jobs)
        session.flush()
        scorecards = [
            ScorecardVersion(
                tenant_id=tenant.id,
                job_id=job.id,
                version=1,
                target_titles=["Product Manager"],
                seniority=[],
                minimum_years=None,
                maximum_years=None,
                locations=[],
                industry_code="technology.fintech",
                suggested_adjacent_industries=[],
                uncertainties=[],
                extraction_status="ready",
                confirmed_by_user_id=owner.id,
            )
            for job in jobs
        ]
        session.add_all(scorecards)
        session.flush()
        candidates = [
            Candidate(
                tenant_id=tenant.id,
                full_name=name,
                normalized_name=name.casefold(),
            )
            for name in ("Visible Candidate", "Hidden Candidate")
        ]
        session.add_all(candidates)
        session.flush()
        session.add_all(
            JobCandidate(
                tenant_id=tenant.id,
                job_id=job.id,
                candidate_id=candidate.id,
                classification="main",
                score=80,
                score_json={},
                scorecard_version_id=scorecard.id,
                scoring_version="matching-v1",
            )
            for job, candidate, scorecard in zip(
                jobs, candidates, scorecards, strict=True
            )
        )
        session.commit()
        ids = {
            "tenant_id": tenant.id,
            "other_tenant_id": other_tenant.id,
            "recruiter_id": recruiter.id,
            "owner_id": owner.id,
            "granted_client_id": granted.id,
            "visible_candidate_id": candidates[0].id,
            "hidden_candidate_id": candidates[1].id,
        }

    current = {
        "context": RequestContext(
            tenant_id=ids["tenant_id"],
            user_id=ids["recruiter_id"],
            role=Role.RECRUITER,
            allowed_client_ids=frozenset((ids["granted_client_id"],)),
        )
    }

    def database() -> Generator[Session, None, None]:
        with Session(engine, expire_on_commit=False) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    app = create_app(Settings.for_test(), privacy_dispatcher=lambda *_: None)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_request_context] = lambda: current["context"]
    with TestClient(app) as client:
        yield {"api": client, "current": current, **ids}
    engine.dispose()


def test_privacy_routes_allow_recruiter_submission_but_manager_only_operation(
    privacy_api: dict[str, Any],
) -> None:
    api = privacy_api["api"]
    created = api.post(
        "/api/v1/privacy-requests",
        headers={"Idempotency-Key": "privacy-api-submit"},
        json={
            "candidate_id": str(privacy_api["visible_candidate_id"]),
            "request_type": "Deletion",
        },
    )
    assert created.status_code == 201
    assert created.json()["state"] == "Identity Verification Required"
    request_id = created.json()["id"]

    hidden = api.post(
        "/api/v1/privacy-requests",
        headers={"Idempotency-Key": "privacy-api-hidden"},
        json={
            "candidate_id": str(privacy_api["hidden_candidate_id"]),
            "request_type": "Deletion",
        },
    )
    recruiter_verify = api.post(
        f"/api/v1/privacy-requests/{request_id}/verify",
        headers={"Idempotency-Key": "privacy-api-recruiter-verify"},
    )
    assert hidden.status_code == 404
    assert recruiter_verify.status_code == 403

    privacy_api["current"]["context"] = RequestContext(
        tenant_id=privacy_api["tenant_id"],
        user_id=privacy_api["owner_id"],
        role=Role.OWNER,
    )
    verified = api.post(
        f"/api/v1/privacy-requests/{request_id}/verify",
        headers={"Idempotency-Key": "privacy-api-owner-verify"},
    )
    approved = api.post(
        f"/api/v1/privacy-requests/{request_id}/approve",
        headers={"Idempotency-Key": "privacy-api-owner-approve"},
    )
    executed = api.post(
        f"/api/v1/privacy-requests/{request_id}/execute",
        headers={"Idempotency-Key": "privacy-api-owner-execute"},
    )
    assert verified.json()["state"] == "Received"
    assert approved.json()["state"] == "Approved"
    assert executed.json()["state"] == "Completed"
    assert api.get(f"/api/v1/privacy-requests/{request_id}").status_code == 200
    assert len(api.get("/api/v1/privacy-requests").json()) == 1


def test_privacy_request_status_is_not_disclosed_cross_tenant(
    privacy_api: dict[str, Any],
) -> None:
    privacy_api["current"]["context"] = RequestContext(
        tenant_id=privacy_api["tenant_id"],
        user_id=privacy_api["owner_id"],
        role=Role.OWNER,
    )
    created = privacy_api["api"].post(
        "/api/v1/privacy-requests",
        headers={"Idempotency-Key": "privacy-cross-submit"},
        json={
            "candidate_id": str(privacy_api["visible_candidate_id"]),
            "request_type": "Access",
        },
    )
    request_id = created.json()["id"]
    privacy_api["current"]["context"] = RequestContext(
        tenant_id=privacy_api["other_tenant_id"],
        user_id=privacy_api["owner_id"],
        role=Role.OWNER,
    )

    response = privacy_api["api"].get(f"/api/v1/privacy-requests/{request_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "privacy_request_not_found"}}


def test_recruiter_request_is_not_disclosed_after_client_grant_is_revoked(
    privacy_api: dict[str, Any],
) -> None:
    created = privacy_api["api"].post(
        "/api/v1/privacy-requests",
        headers={"Idempotency-Key": "privacy-revoked-submit"},
        json={
            "candidate_id": str(privacy_api["visible_candidate_id"]),
            "request_type": "Access",
        },
    )
    assert created.status_code == 201
    request_id = created.json()["id"]
    privacy_api["current"]["context"] = RequestContext(
        tenant_id=privacy_api["tenant_id"],
        user_id=privacy_api["recruiter_id"],
        role=Role.RECRUITER,
        allowed_client_ids=frozenset(),
    )

    status_response = privacy_api["api"].get(f"/api/v1/privacy-requests/{request_id}")
    list_response = privacy_api["api"].get("/api/v1/privacy-requests")

    assert status_response.status_code == 404
    assert list_response.status_code == 200
    assert list_response.json() == []
