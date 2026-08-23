import base64
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.candidates.contacts import ContactCipher, ContactService
from app.candidates.models import Candidate
from app.clients.models import ClientCompany
from app.core.config import Settings
from app.core.database import Base, get_db
from app.crm.models import (
    CandidateStage,
    JobCandidate,
    JobCandidateTag,
    Tag,
)
from app.identity.models import Membership, Tenant, User
from app.identity.schemas import IdentityClaims, RequestContext, Role
from app.jobs.models import Job, ScorecardVersion
from app.main import create_app
from app.providers.base import ProviderContact


class MutableVerifier:
    def __init__(self, claims: IdentityClaims) -> None:
        self.claims = claims

    def verify(self, token: str) -> IdentityClaims:
        return self.claims


@pytest.fixture
def crm_api(monkeypatch: pytest.MonkeyPatch) -> Generator[dict[str, Any], None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    cipher = ContactCipher(base64.b64encode(b"c" * 32).decode(), b"crm-lookup")
    with Session(engine, expire_on_commit=False) as session:
        tenant = Tenant(slug=f"crm-api-{uuid4()}")
        recruiter = User(
            oidc_subject=f"crm-api-recruiter|{uuid4()}",
            email="recruiter@example.test",
            display_name="Recruiter",
        )
        assignee = User(
            oidc_subject=f"crm-api-assignee|{uuid4()}",
            email="assignee@example.test",
            display_name="Assignee",
        )
        session.add_all((tenant, recruiter, assignee))
        session.flush()
        granted_client = ClientCompany(
            tenant_id=tenant.id,
            name="Granted Client",
            normalized_name=f"granted-{uuid4()}",
        )
        hidden_client = ClientCompany(
            tenant_id=tenant.id,
            name="Hidden Client",
            normalized_name=f"hidden-{uuid4()}",
        )
        session.add_all((granted_client, hidden_client))
        session.flush()
        session.add_all(
            (
                Membership(
                    tenant_id=tenant.id,
                    user_id=recruiter.id,
                    role=Role.RECRUITER,
                    allowed_client_ids=[str(granted_client.id)],
                ),
                Membership(
                    tenant_id=tenant.id,
                    user_id=assignee.id,
                    role=Role.RECRUITER,
                    allowed_client_ids=[str(granted_client.id)],
                ),
            )
        )
        jobs = [
            Job(
                tenant_id=tenant.id,
                client_id=client_id,
                owner_user_id=recruiter.id,
                title=title,
                job_description=f"Find {title}.",
            )
            for client_id, title in (
                (granted_client.id, "Product Manager"),
                (granted_client.id, "Product Lead"),
                (hidden_client.id, "Hidden Product Manager"),
            )
        ]
        session.add_all(jobs)
        session.flush()
        scorecards = [
            ScorecardVersion(
                tenant_id=tenant.id,
                job_id=job.id,
                version=1,
                target_titles=[job.title],
                seniority=[],
                minimum_years=None,
                maximum_years=None,
                locations=[],
                industry_code="technology.fintech",
                suggested_adjacent_industries=[],
                uncertainties=[],
                extraction_status="ready",
                confirmed_by_user_id=recruiter.id,
            )
            for job in jobs
        ]
        session.add_all(scorecards)
        session.flush()
        for job, scorecard in zip(jobs, scorecards, strict=True):
            job.current_scorecard_id = scorecard.id

        priya = Candidate(
            tenant_id=tenant.id,
            full_name="Priya Sharma",
            normalized_name="priya sharma",
            current_title="Senior Product Manager",
            normalized_title="senior product manager",
            current_company="PayFlow",
            normalized_company="payflow",
            location="New York, United States",
            normalized_location="new york united states",
            normalized_skills=["payment processing", "sql"],
            industry_codes=["technology.fintech"],
        )
        jamal = Candidate(
            tenant_id=tenant.id,
            full_name="Jamal Reed",
            normalized_name="jamal reed",
            current_title="Product Manager",
            normalized_title="product manager",
            current_company="Ledger Labs",
            normalized_company="ledger labs",
            location="Boston, United States",
            normalized_location="boston united states",
            normalized_skills=["roadmaps"],
            industry_codes=["financial_services.banking"],
        )
        formula = Candidate(
            tenant_id=tenant.id,
            full_name="=2+3",
            normalized_name="=2+3",
            current_title="Product Lead",
            normalized_title="product lead",
            current_company="Sheets Inc",
            normalized_company="sheets inc",
            location="Remote",
            normalized_location="remote",
            normalized_skills=["payments"],
            industry_codes=["technology.fintech"],
        )
        hidden_candidate = Candidate(
            tenant_id=tenant.id,
            full_name="Priya Hidden",
            normalized_name="priya hidden",
            current_title="Secret Role",
            normalized_title="secret role",
            current_company="Secret Co",
            normalized_company="secret co",
            normalized_skills=["payment processing"],
            industry_codes=["technology.fintech"],
        )
        session.add_all((priya, jamal, formula, hidden_candidate))
        session.flush()
        rows = [
            JobCandidate(
                tenant_id=tenant.id,
                job_id=job_id,
                candidate_id=candidate.id,
                scorecard_version_id=scorecard_id,
                classification=classification,
                score=score,
                score_json={"total": score, "criteria": []},
                scoring_version="matching-v1",
                stage=stage,
                owner_user_id=owner_user_id,
            )
            for job_id, candidate, scorecard_id, classification, score, stage, owner_user_id in (
                (
                    jobs[0].id,
                    priya,
                    scorecards[0].id,
                    "main",
                    90,
                    CandidateStage.NEW,
                    assignee.id,
                ),
                (
                    jobs[0].id,
                    jamal,
                    scorecards[0].id,
                    "main",
                    80,
                    CandidateStage.REVIEWED,
                    None,
                ),
                (
                    jobs[0].id,
                    formula,
                    scorecards[0].id,
                    "near_match",
                    70,
                    CandidateStage.SHORTLISTED,
                    None,
                ),
                (
                    jobs[1].id,
                    priya,
                    scorecards[1].id,
                    "main",
                    88,
                    CandidateStage.REVIEWED,
                    assignee.id,
                ),
                (
                    jobs[2].id,
                    priya,
                    scorecards[2].id,
                    "main",
                    99,
                    CandidateStage.SHORTLISTED,
                    None,
                ),
                (
                    jobs[2].id,
                    hidden_candidate,
                    scorecards[2].id,
                    "main",
                    98,
                    CandidateStage.SHORTLISTED,
                    None,
                ),
            )
        ]
        session.add_all(rows)
        session.flush()
        urgent = Tag(
            tenant_id=tenant.id,
            name="Urgent",
            normalized_name="urgent",
        )
        session.add(urgent)
        session.flush()
        session.add(
            JobCandidateTag(
                tenant_id=tenant.id,
                job_candidate_id=rows[0].id,
                tag_id=urgent.id,
            )
        )
        contact_context = RequestContext(
            tenant_id=tenant.id,
            user_id=recruiter.id,
            role=Role.RECRUITER,
            allowed_client_ids=frozenset((granted_client.id,)),
        )
        work_email = (
            ContactService(session, cipher)
            .store(
                contact_context,
                priya.id,
                ProviderContact(
                    kind="email",
                    value="priya@example.test",
                    classification="work",
                    verification_state="verified",
                    observed_at=datetime.now(UTC),
                ),
            )
            .contact_point
        )
        phone = (
            ContactService(session, cipher)
            .store(
                contact_context,
                formula.id,
                ProviderContact(
                    kind="phone",
                    value="+1 212 555 0112",
                    classification="work",
                    verification_state="verified",
                    observed_at=datetime.now(UTC),
                ),
            )
            .contact_point
        )
        session.commit()
        fixture = {
            "tenant_id": tenant.id,
            "recruiter_id": recruiter.id,
            "assignee_id": assignee.id,
            "granted_client_id": granted_client.id,
            "hidden_client_id": hidden_client.id,
            "job_id": jobs[0].id,
            "second_job_id": jobs[1].id,
            "hidden_job_id": jobs[2].id,
            "priya_id": priya.id,
            "jamal_id": jamal.id,
            "formula_id": formula.id,
            "hidden_candidate_id": hidden_candidate.id,
            "priya_row_id": rows[0].id,
            "jamal_row_id": rows[1].id,
            "formula_row_id": rows[2].id,
            "hidden_row_id": rows[4].id,
            "work_email_id": work_email.id,
            "phone_id": phone.id,
        }

    settings = Settings.for_test()
    app = create_app(settings, contact_cipher=cipher)
    verifier = MutableVerifier(
        IdentityClaims(
            subject=recruiter.oidc_subject,
            email=recruiter.email,
            name=recruiter.display_name,
            email_verified=True,
        )
    )
    app.state.token_verifier = verifier

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
            **fixture,
            "api": api,
            "engine": engine,
            "cipher": cipher,
            "verifier": verifier,
            "headers": {
                "Authorization": "Bearer signed-token",
                "X-Tenant-ID": str(fixture["tenant_id"]),
            },
        }
    engine.dispose()
