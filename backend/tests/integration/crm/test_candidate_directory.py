from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.candidates.service import CandidateService
from app.core.database import Base
from app.crm.models import JobCandidate
from app.identity.models import Membership, Tenant
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job
from app.providers.base import ProviderPerson


def test_candidate_profile_preserves_normalized_skill_and_industry_facts() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(slug=f"directory-facts-{uuid4()}")
        session.add(tenant)
        session.flush()
        context = RequestContext(
            tenant_id=tenant.id,
            user_id=uuid4(),
            role=Role.OWNER,
        )
        result = CandidateService(session).ingest(
            context,
            ProviderPerson(
                provider="apollo",
                provider_person_id=f"directory-facts-{uuid4()}",
                full_name="Priya Sharma",
                current_title="Product Manager",
                current_company="PayFlow",
                location="New York",
                linkedin_url=None,
                experiences=(),
                skills=(" Payment Processing ", "SQL", "sql"),
                industry_codes=("Technology.Fintech",),
            ),
        )

        profile = CandidateService(session).get_profile(
            context,
            result.candidate_id,
        )

        assert profile is not None
        assert profile.skills == ("payment processing", "sql")
        assert profile.industry_codes == ("technology.fintech",)

    engine.dispose()


def test_directory_search_and_jobs_never_expose_ungranted_client_matches(
    crm_api,
) -> None:
    api = crm_api["api"]
    headers = crm_api["headers"]

    result = api.get("/api/v1/candidates", headers=headers, params={"q": "Priya"})

    assert result.status_code == 200
    assert "total" not in result.json()
    assert [item["id"] for item in result.json()["items"]] == [str(crm_api["priya_id"])]
    assert set(result.json()["items"][0]["job_ids"]) == {
        str(crm_api["job_id"]),
        str(crm_api["second_job_id"]),
    }
    assert str(crm_api["hidden_job_id"]) not in result.text
    assert str(crm_api["hidden_candidate_id"]) not in result.text

    jobs = api.get(f"/api/v1/candidates/{crm_api['priya_id']}/jobs", headers=headers)
    hidden_jobs = api.get(
        f"/api/v1/candidates/{crm_api['hidden_candidate_id']}/jobs", headers=headers
    )
    assert jobs.status_code == 200
    assert {item["job_id"] for item in jobs.json()} == {
        str(crm_api["job_id"]),
        str(crm_api["second_job_id"]),
    }
    assert hidden_jobs.status_code == 404
    assert hidden_jobs.json() == {"detail": {"code": "candidate_not_found"}}

    hidden_search = api.get(
        "/api/v1/candidates", headers=headers, params={"q": "Secret Role"}
    )
    assert hidden_search.status_code == 200
    assert hidden_search.json() == {"items": [], "next_cursor": None}


def test_ingested_skill_and_industry_facts_are_searchable_in_directory(crm_api) -> None:
    context = RequestContext(
        tenant_id=crm_api["tenant_id"],
        user_id=crm_api["recruiter_id"],
        role=Role.RECRUITER,
        allowed_client_ids=frozenset((crm_api["granted_client_id"],)),
    )
    with Session(crm_api["engine"], expire_on_commit=False) as session:
        result = CandidateService(session).ingest(
            context,
            ProviderPerson(
                provider="apollo",
                provider_person_id=f"directory-search-{uuid4()}",
                full_name="Avery Facts",
                current_title="Treasury Director",
                current_company="Atlas",
                location="New York",
                linkedin_url=None,
                experiences=(),
                skills=("Treasury Automation",),
                industry_codes=("Technology.Fintech",),
            ),
        )
        job = session.get(Job, crm_api["job_id"])
        assert job is not None and job.current_scorecard_id is not None
        session.add(
            JobCandidate(
                tenant_id=crm_api["tenant_id"],
                job_id=job.id,
                candidate_id=result.candidate_id,
                classification="main",
                score=77,
                score_json={"total": 77},
                scorecard_version_id=job.current_scorecard_id,
                scoring_version="matching-v1",
            )
        )
        session.commit()
        candidate_id = result.candidate_id

    by_skill = crm_api["api"].get(
        "/api/v1/candidates",
        headers=crm_api["headers"],
        params={"q": "treasury automation"},
    )
    by_industry = crm_api["api"].get(
        "/api/v1/candidates",
        headers=crm_api["headers"],
        params={"q": "Avery Facts", "industry": "technology.fintech"},
    )

    assert by_skill.status_code == by_industry.status_code == 200
    assert [item["id"] for item in by_skill.json()["items"]] == [str(candidate_id)]
    assert [item["id"] for item in by_industry.json()["items"]] == [str(candidate_id)]


def test_directory_filters_canonical_facts_and_uses_stable_updated_cursor(
    crm_api,
) -> None:
    api = crm_api["api"]
    headers = crm_api["headers"]

    for params in (
        {"q": "payment processing"},
        {"location": "New York"},
        {"industry": "technology.fintech", "q": "Priya"},
    ):
        response = api.get("/api/v1/candidates", headers=headers, params=params)
        assert response.status_code == 200, (params, response.text)
        assert [item["id"] for item in response.json()["items"]] == [
            str(crm_api["priya_id"])
        ]

    first = api.get("/api/v1/candidates", headers=headers, params={"limit": 1})
    assert first.status_code == 200
    assert len(first.json()["items"]) == 1
    assert first.json()["next_cursor"]
    second = api.get(
        "/api/v1/candidates",
        headers=headers,
        params={"limit": 1, "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1
    assert second.json()["items"][0]["id"] != first.json()["items"][0]["id"]

    tampered = api.get(
        "/api/v1/candidates",
        headers=headers,
        params={"cursor": first.json()["next_cursor"] + "x"},
    )
    assert tampered.status_code == 400
    assert tampered.json() == {"detail": {"code": "cursor_invalid"}}

    cross_filter = api.get(
        "/api/v1/candidates",
        headers=headers,
        params={"q": "Priya", "cursor": first.json()["next_cursor"]},
    )
    assert cross_filter.status_code == 400
    assert cross_filter.json() == {"detail": {"code": "cursor_invalid"}}


def test_recruiter_without_client_grants_cannot_discover_directory_matches(
    crm_api,
) -> None:
    with Session(crm_api["engine"]) as session:
        membership = session.scalar(
            select(Membership).where(
                Membership.tenant_id == crm_api["tenant_id"],
                Membership.user_id == crm_api["recruiter_id"],
            )
        )
        assert membership is not None
        membership.allowed_client_ids = None
        session.commit()

    directory = crm_api["api"].get(
        "/api/v1/candidates", headers=crm_api["headers"], params={"q": "Priya"}
    )
    jobs = crm_api["api"].get(
        f"/api/v1/candidates/{crm_api['priya_id']}/jobs",
        headers=crm_api["headers"],
    )
    reveal = crm_api["api"].post(
        f"/api/v1/contact-points/{crm_api['work_email_id']}/reveal",
        headers={**crm_api["headers"], "Idempotency-Key": "no-grant-reveal"},
    )

    assert directory.status_code == 200
    assert directory.json() == {"items": [], "next_cursor": None}
    assert jobs.status_code == 404
    assert jobs.json() == {"detail": {"code": "candidate_not_found"}}
    assert reveal.status_code == 404
    assert reveal.json() == {"detail": {"code": "contact_point_not_found"}}
