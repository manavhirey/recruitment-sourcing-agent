from collections.abc import Generator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.clients.models import ClientCompany, ClientIndustry
from app.core.config import Settings
from app.core.database import Base, get_db
from app.identity.models import Membership, Tenant, User
from app.identity.schemas import IdentityClaims, RequestContext, Role
from app.jobs.llm import ScorecardExtractionError
from app.jobs.schemas import CriterionKind, ScorecardCriterion, ScorecardDraft
from app.jobs.service import JobError, JobService
from app.main import create_app


class StaticVerifier:
    def __init__(self, claims: IdentityClaims) -> None:
        self.claims = claims

    def verify(self, token: str) -> IdentityClaims:
        return self.claims


class AlwaysInvalidGateway:
    def extract(self, job_description, client_context):
        raise ScorecardExtractionError("invalid scorecard")


@pytest.fixture
def job_session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


@pytest.fixture
def owner_context(job_session: Session) -> RequestContext:
    tenant = Tenant(id=uuid4(), slug="job-agency")
    owner = User(
        id=uuid4(),
        oidc_subject="oidc|job-owner",
        email="job-owner@agency.test",
        display_name="Job Owner",
    )
    job_session.add_all((tenant, owner))
    job_session.flush()
    return RequestContext(
        tenant_id=tenant.id,
        user_id=owner.id,
        role=Role.OWNER,
    )


@pytest.fixture
def job_service(job_session: Session) -> JobService:
    return JobService(job_session, b"test-suppression-key")


@pytest.fixture
def draft_job(
    job_session: Session,
    job_service: JobService,
    owner_context: RequestContext,
):
    client = ClientCompany(
        tenant_id=owner_context.tenant_id,
        name="Fintech Client",
        normalized_name="fintech client",
    )
    job_session.add(client)
    job_session.flush()
    job_session.add(
        ClientIndustry(
            tenant_id=owner_context.tenant_id,
            client_id=client.id,
            industry_code="technology.fintech",
            taxonomy_version="v1",
        )
    )
    job_session.flush()
    job = job_service.create(
        owner_context,
        client_id=client.id,
        title="Product Manager",
        job_description="Hire a fintech product manager with payments experience.",
        idempotency_key="create-draft-job",
    )
    job_service.update_draft(
        owner_context,
        job.id,
        ScorecardDraft(
            target_titles=["Product Manager"],
            criteria=[
                ScorecardCriterion(
                    key="payments",
                    label="Payments experience",
                    kind=CriterionKind.MUST_HAVE,
                    source_text="payments experience",
                )
            ],
            seniority=["manager"],
            minimum_years=5,
            maximum_years=12,
            locations=["India"],
            industry_code="technology.fintech",
            suggested_adjacent_industries=[],
            uncertainties=[],
        ),
        expected_revision=0,
        idempotency_key="save-draft-scorecard",
    )
    return job


def test_confirmed_scorecard_is_immutable(
    job_service, draft_job, owner_context
) -> None:
    first = job_service.confirm_scorecard(
        owner_context, draft_job.id, expected_revision=1
    )
    second = job_service.revise_scorecard(owner_context, draft_job.id, first.to_draft())
    assert first.version == 1
    assert second.version == 2
    assert first.id != second.id


def test_confirmation_rejects_unapproved_adjacent_industry(
    job_service, draft_job, owner_context
) -> None:
    draft = ScorecardDraft.model_validate(draft_job.draft_payload)
    draft.suggested_adjacent_industries = ["financial_services.banking"]
    job_service.update_draft(
        owner_context,
        draft_job.id,
        draft,
        expected_revision=1,
        idempotency_key="save-unapproved-adjacency",
    )

    with pytest.raises(JobError, match="scorecard_adjacency_not_approved"):
        job_service.confirm_scorecard(owner_context, draft_job.id, expected_revision=2)


@pytest.fixture
def job_api(monkeypatch: pytest.MonkeyPatch) -> Generator[dict[str, Any], None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    tenant = Tenant(id=uuid4(), slug="job-api-agency")
    owner = User(
        id=uuid4(),
        oidc_subject="oidc|job-api-owner",
        email="job-api-owner@agency.test",
        display_name="Job API Owner",
    )
    client = ClientCompany(
        tenant_id=tenant.id,
        name="API Fintech Client",
        normalized_name="api fintech client",
    )
    with Session(engine) as session:
        session.add_all((tenant, owner))
        session.flush()
        client.tenant_id = tenant.id
        session.add(client)
        session.flush()
        session.add_all(
            (
                Membership(tenant_id=tenant.id, user_id=owner.id, role=Role.OWNER),
                ClientIndustry(
                    tenant_id=tenant.id,
                    client_id=client.id,
                    industry_code="technology.fintech",
                    taxonomy_version="v1",
                ),
            )
        )
        tenant_id = tenant.id
        client_id = client.id
        session.commit()

    app = create_app(Settings.for_test(), scorecard_gateway=AlwaysInvalidGateway())
    app.state.token_verifier = StaticVerifier(
        IdentityClaims(
            subject="oidc|job-api-owner",
            email="job-api-owner@agency.test",
            name="Job API Owner",
            email_verified=True,
        )
    )

    def database_session() -> Generator[Session, None, None]:
        with Session(engine) as session:
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
        yield {"api": api, "tenant_id": tenant_id, "client_id": client_id}
    engine.dispose()


def test_double_extraction_failure_returns_editable_manual_draft(job_api) -> None:
    headers = {
        "Authorization": "Bearer signed-token",
        "X-Tenant-ID": str(job_api["tenant_id"]),
    }
    description = "Hire a product manager with payments experience."
    created = job_api["api"].post(
        "/api/v1/jobs",
        headers={**headers, "Idempotency-Key": "create-job-for-manual-draft"},
        json={
            "client_id": str(job_api["client_id"]),
            "title": "Product Manager",
            "job_description": description,
        },
    )
    assert created.status_code == 201

    generated = job_api["api"].post(
        f"/api/v1/jobs/{created.json()['id']}/scorecard/generate",
        headers={**headers, "Idempotency-Key": "generate-manual-draft"},
        json={"expected_revision": 0},
    )

    assert generated.status_code == 200
    assert generated.json()["draft"]["target_titles"] == []
    assert generated.json()["draft"]["criteria"] == []
    assert generated.json()["original_job_description"] == description
    assert generated.json()["extraction_status"] == "manual_required"
    assert generated.json()["extraction_warning"]

    manual_draft = {
        "target_titles": ["Product Manager"],
        "criteria": [
            {
                "key": "payments",
                "label": "Payments experience",
                "kind": "must_have",
                "source_text": "payments experience",
                "recruiter_entered": True,
            }
        ],
        "seniority": ["manager"],
        "minimum_years": 5,
        "maximum_years": 12,
        "locations": ["India"],
        "industry_code": "technology.fintech",
        "suggested_adjacent_industries": [],
        "uncertainties": [],
    }
    updated = job_api["api"].put(
        f"/api/v1/jobs/{created.json()['id']}/scorecard/draft",
        headers={**headers, "Idempotency-Key": "save-manual-draft"},
        json={"expected_revision": 1, "draft": manual_draft},
    )

    assert updated.status_code == 200
    assert updated.json()["extraction_status"] == "manual_required"

    confirmed = job_api["api"].post(
        f"/api/v1/jobs/{created.json()['id']}/scorecard/confirm",
        headers={**headers, "Idempotency-Key": "confirm-manual-draft"},
        json={"expected_revision": 2},
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["version"] == 1
    assert confirmed.json()["extraction_status"] == "manual_required"
