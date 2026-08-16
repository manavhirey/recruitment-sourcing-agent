from collections.abc import Generator
from typing import Any
from uuid import UUID, uuid4

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
from app.jobs.models import Job
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


def test_confirmation_rejects_an_inferred_item_without_persisted_approval(
    job_service: JobService,
    draft_job,
    owner_context: RequestContext,
) -> None:
    draft = ScorecardDraft.model_validate(draft_job.draft_payload)
    draft.criteria.append(
        ScorecardCriterion(
            key="growth",
            label="Led product-led growth",
            kind=CriterionKind.PREFERENCE,
            inferred=True,
        )
    )
    draft_job.draft_payload = draft.model_dump(mode="json")

    with pytest.raises(JobError, match="scorecard_inferences_unresolved"):
        job_service.confirm_scorecard(owner_context, draft_job.id, expected_revision=1)


def test_draft_update_cannot_launder_server_owned_inference_provenance(
    job_service: JobService,
    draft_job,
    owner_context: RequestContext,
) -> None:
    draft = ScorecardDraft.model_validate(draft_job.draft_payload)
    draft.criteria.append(
        ScorecardCriterion(
            key="growth",
            label="Led product-led growth",
            kind=CriterionKind.PREFERENCE,
            inferred=True,
        )
    )
    draft_job.draft_payload = draft.model_dump(mode="json")
    laundered_payload = draft.model_dump()
    laundered_payload["criteria"][1]["inferred"] = False
    laundered = ScorecardDraft.model_validate(laundered_payload)

    normalized = job_service.update_draft(
        owner_context,
        draft_job.id,
        laundered,
        expected_revision=1,
        idempotency_key="attempt-inference-laundering",
    )

    assert normalized.draft.criteria[1].inferred is True
    with pytest.raises(JobError, match="scorecard_inferences_unresolved"):
        job_service.confirm_scorecard(
            owner_context, draft_job.id, expected_revision=2
        )


def test_draft_update_cannot_invent_extraction_source_to_bypass_lawful_review(
    job_service: JobService,
    draft_job,
    owner_context: RequestContext,
) -> None:
    draft = ScorecardDraft.model_validate(draft_job.draft_payload)
    draft.criteria.append(
        ScorecardCriterion(
            key="onsite_attendance",
            label="Cannot work onsite",
            kind=CriterionKind.EXCLUSION,
            source_text="Invented source",
        )
    )

    with pytest.raises(JobError, match="scorecard_criterion_invalid"):
        job_service.update_draft(
            owner_context,
            draft_job.id,
            draft,
            expected_revision=1,
            idempotency_key="attempt-source-laundering",
        )


def test_unchanged_extracted_criterion_keeps_server_owned_provenance(
    job_service: JobService,
    draft_job,
    owner_context: RequestContext,
) -> None:
    extracted = ScorecardDraft(
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
    )
    draft_job.draft_payload = extracted.model_dump(mode="json")

    saved = job_service.update_draft(
        owner_context,
        draft_job.id,
        extracted,
        expected_revision=1,
        idempotency_key="keep-unchanged-extraction-provenance",
    )

    criterion = saved.draft.criteria[0]
    assert criterion.source_text == "payments experience"
    assert criterion.recruiter_entered is False


def test_persisted_inference_approval_survives_reload_and_confirmation(
    job_service: JobService,
    draft_job,
    owner_context: RequestContext,
) -> None:
    draft = ScorecardDraft.model_validate(draft_job.draft_payload)
    draft.criteria.append(
        ScorecardCriterion(
            key="growth",
            label="Led product-led growth",
            kind=CriterionKind.PREFERENCE,
            inferred=True,
        )
    )
    approved = draft.model_copy(
        update={"confirmed_inferred_items": sorted(draft.inferred_item_ids())}
    )
    draft_job.draft_payload = approved.model_dump(mode="json")

    reloaded = job_service.get_draft(owner_context, draft_job.id)
    confirmed = job_service.confirm_scorecard(
        owner_context, draft_job.id, expected_revision=1
    )

    assert reloaded.draft.confirmed_inferred_items == approved.confirmed_inferred_items
    assert confirmed.confirmed_inferred_items == approved.confirmed_inferred_items


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
        yield {
            "api": api,
            "engine": engine,
            "tenant_id": tenant_id,
            "client_id": client_id,
        }
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


def test_direct_api_cannot_clear_server_owned_inference_before_confirmation(
    job_api,
) -> None:
    headers = {
        "Authorization": "Bearer signed-token",
        "X-Tenant-ID": str(job_api["tenant_id"]),
    }
    created = job_api["api"].post(
        "/api/v1/jobs",
        headers={**headers, "Idempotency-Key": "create-inference-api-job"},
        json={
            "client_id": str(job_api["client_id"]),
            "title": "Product Manager",
            "job_description": "Payments role.",
        },
    )
    job_id = UUID(created.json()["id"])
    server_draft = ScorecardDraft(
        target_titles=["Product Manager"],
        criteria=[
            ScorecardCriterion(
                key="growth",
                label="Led product-led growth",
                kind=CriterionKind.PREFERENCE,
                inferred=True,
            )
        ],
        seniority=[],
        locations=[],
        industry_code="technology.fintech",
        suggested_adjacent_industries=[],
        uncertainties=[],
    )
    with Session(job_api["engine"]) as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.draft_payload = server_draft.model_dump(mode="json")
        job.draft_revision = 1
        session.commit()

    laundered = server_draft.model_dump(mode="json")
    laundered["criteria"][0]["inferred"] = False
    saved = job_api["api"].put(
        f"/api/v1/jobs/{job_id}/scorecard/draft",
        headers={**headers, "Idempotency-Key": "launder-inference-api"},
        json={"expected_revision": 1, "draft": laundered},
    )
    bypass = job_api["api"].post(
        f"/api/v1/jobs/{job_id}/scorecard/confirm",
        headers={**headers, "Idempotency-Key": "confirm-laundered-api"},
        json={"expected_revision": 2},
    )

    assert saved.status_code == 200
    assert saved.json()["draft"]["criteria"][0]["inferred"] is True
    assert bypass.status_code == 409
    assert bypass.json() == {
        "detail": {"code": "scorecard_inferences_unresolved"}
    }


def test_job_list_is_bounded_deterministic_and_paginated(job_api) -> None:
    headers = {
        "Authorization": "Bearer signed-token",
        "X-Tenant-ID": str(job_api["tenant_id"]),
    }
    for index in range(3):
        response = job_api["api"].post(
            "/api/v1/jobs",
            headers={**headers, "Idempotency-Key": f"create-listed-job-{index}"},
            json={
                "client_id": str(job_api["client_id"]),
                "title": f"Product Manager {index}",
                "job_description": "Payments platform product role.",
            },
        )
        assert response.status_code == 201

    first = job_api["api"].get("/api/v1/jobs?limit=2&offset=0", headers=headers)
    repeat = job_api["api"].get("/api/v1/jobs?limit=2&offset=0", headers=headers)
    second = job_api["api"].get("/api/v1/jobs?limit=2&offset=2", headers=headers)

    assert first.status_code == 200
    assert first.json() == repeat.json()
    assert len(first.json()["items"]) == 2
    assert first.json()["next_offset"] == 2
    assert len(second.json()["items"]) == 1
    assert second.json()["next_offset"] is None
    assert all(
        "job_description" not in item and "owner_user_id" not in item
        for item in first.json()["items"]
    )
    assert {item["id"] for item in first.json()["items"]}.isdisjoint(
        {item["id"] for item in second.json()["items"]}
    )


def test_job_list_restricts_recruiter_to_allowed_clients(
    job_session: Session,
    job_service: JobService,
    owner_context: RequestContext,
) -> None:
    allowed = ClientCompany(
        tenant_id=owner_context.tenant_id,
        name="Allowed Client",
        normalized_name="allowed client",
    )
    hidden = ClientCompany(
        tenant_id=owner_context.tenant_id,
        name="Hidden Client",
        normalized_name="hidden client",
    )
    job_session.add_all((allowed, hidden))
    job_session.flush()
    for client, suffix in ((allowed, "allowed"), (hidden, "hidden")):
        job_service.create(
            owner_context,
            client_id=client.id,
            title=f"{suffix.title()} role",
            job_description="A valid job description.",
            idempotency_key=f"create-{suffix}-job",
        )
    recruiter_context = RequestContext(
        tenant_id=owner_context.tenant_id,
        user_id=owner_context.user_id,
        role=Role.RECRUITER,
        allowed_client_ids=frozenset({allowed.id}),
    )

    jobs, next_offset = job_service.list_authorized(
        recruiter_context, limit=50, offset=0
    )

    assert [job.client_id for job in jobs] == [allowed.id]
    assert next_offset is None


def test_saved_draft_is_non_disclosing_for_an_unauthorized_recruiter(
    job_service: JobService,
    draft_job,
    owner_context: RequestContext,
) -> None:
    recruiter_context = RequestContext(
        tenant_id=owner_context.tenant_id,
        user_id=owner_context.user_id,
        role=Role.RECRUITER,
        allowed_client_ids=frozenset(),
    )

    with pytest.raises(JobError, match="job_not_found"):
        job_service.get_draft(recruiter_context, draft_job.id)


def test_saved_scorecard_draft_can_be_reloaded_without_regeneration(job_api) -> None:
    headers = {
        "Authorization": "Bearer signed-token",
        "X-Tenant-ID": str(job_api["tenant_id"]),
    }
    created = job_api["api"].post(
        "/api/v1/jobs",
        headers={**headers, "Idempotency-Key": "create-reloadable-job"},
        json={
            "client_id": str(job_api["client_id"]),
            "title": "Product Manager",
            "job_description": "Payments platform product role.",
        },
    )
    job_id = created.json()["id"]

    missing = job_api["api"].get(
        f"/api/v1/jobs/{job_id}/scorecard/draft", headers=headers
    )
    generated = job_api["api"].post(
        f"/api/v1/jobs/{job_id}/scorecard/generate",
        headers={**headers, "Idempotency-Key": "generate-reloadable-draft"},
        json={"expected_revision": 0},
    )
    reloaded = job_api["api"].get(
        f"/api/v1/jobs/{job_id}/scorecard/draft", headers=headers
    )

    assert missing.status_code == 404
    assert missing.json() == {"detail": {"code": "scorecard_draft_not_found"}}
    assert generated.status_code == 200
    assert reloaded.status_code == 200
    assert reloaded.json() == generated.json()


def _draft_payload(title: str, criterion_key: str) -> dict[str, object]:
    return {
        "target_titles": [title],
        "criteria": [
            {
                "key": criterion_key,
                "label": f"{criterion_key.title()} experience",
                "kind": "must_have",
                "source_text": f"{criterion_key} experience",
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


def _create_job_for_replay(job_api, idempotency_key: str) -> tuple[dict, dict]:
    headers = {
        "Authorization": "Bearer signed-token",
        "X-Tenant-ID": str(job_api["tenant_id"]),
    }
    response = job_api["api"].post(
        "/api/v1/jobs",
        headers={**headers, "Idempotency-Key": idempotency_key},
        json={
            "client_id": str(job_api["client_id"]),
            "title": "Product Manager",
            "job_description": "Hire a product manager with payments experience.",
        },
    )
    assert response.status_code == 201
    return headers, response.json()


def test_generate_replay_returns_original_snapshot_after_later_edit(job_api) -> None:
    headers, job = _create_job_for_replay(job_api, "create-generate-replay-job")
    endpoint = f"/api/v1/jobs/{job['id']}/scorecard/generate"
    first = job_api["api"].post(
        endpoint,
        headers={**headers, "Idempotency-Key": "generate-replay-snapshot"},
        json={"expected_revision": 0},
    )
    assert first.status_code == 200
    later = job_api["api"].put(
        f"/api/v1/jobs/{job['id']}/scorecard/draft",
        headers={**headers, "Idempotency-Key": "edit-after-generate"},
        json={
            "expected_revision": 1,
            "draft": _draft_payload("Product Manager", "payments"),
        },
    )
    assert later.status_code == 200

    replay = job_api["api"].post(
        endpoint,
        headers={**headers, "Idempotency-Key": "generate-replay-snapshot"},
        json={"expected_revision": 0},
    )

    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.json()["draft_revision"] == 1


def test_update_replay_returns_original_snapshot_after_later_edit(job_api) -> None:
    headers, job = _create_job_for_replay(job_api, "create-update-replay-job")
    endpoint = f"/api/v1/jobs/{job['id']}/scorecard/draft"
    first_payload = _draft_payload("Product Manager", "payments")
    first = job_api["api"].put(
        endpoint,
        headers={**headers, "Idempotency-Key": "update-replay-snapshot"},
        json={"expected_revision": 0, "draft": first_payload},
    )
    assert first.status_code == 200
    later = job_api["api"].put(
        endpoint,
        headers={**headers, "Idempotency-Key": "later-draft-edit"},
        json={
            "expected_revision": 1,
            "draft": _draft_payload("Senior Product Manager", "platform"),
        },
    )
    assert later.status_code == 200

    replay = job_api["api"].put(
        endpoint,
        headers={**headers, "Idempotency-Key": "update-replay-snapshot"},
        json={"expected_revision": 0, "draft": first_payload},
    )

    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.json()["draft_revision"] == 1
