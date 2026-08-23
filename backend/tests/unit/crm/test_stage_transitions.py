from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.candidates.models import Candidate
from app.clients.models import ClientCompany
from app.core.database import Base
from app.crm.models import ActivityEvent, CandidateStage, JobCandidate
from app.crm.service import CrmError, CrmService
from app.identity.models import Tenant, User
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardVersion


@pytest.fixture
def crm_scenario() -> tuple[Session, CrmService, JobCandidate, RequestContext]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    tenant = Tenant(slug=f"crm-unit-{uuid4()}")
    user = User(
        oidc_subject=f"crm-unit|{uuid4()}",
        email=f"{uuid4()}@example.test",
        display_name="CRM Reviewer",
    )
    session.add_all((tenant, user))
    session.flush()
    client = ClientCompany(
        tenant_id=tenant.id,
        name="CRM Client",
        normalized_name="crm client",
    )
    candidate = Candidate(
        tenant_id=tenant.id,
        full_name="Priya Sharma",
        normalized_name="priya sharma",
    )
    session.add_all((client, candidate))
    session.flush()
    job = Job(
        tenant_id=tenant.id,
        client_id=client.id,
        owner_user_id=user.id,
        title="Product Manager",
        job_description="Find a product manager.",
    )
    session.add(job)
    session.flush()
    scorecard = ScorecardVersion(
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
        confirmed_by_user_id=user.id,
    )
    session.add(scorecard)
    session.flush()
    context = RequestContext(
        tenant_id=tenant.id,
        user_id=user.id,
        role=Role.RECRUITER,
        allowed_client_ids=frozenset((client.id,)),
    )
    row = JobCandidate(
        tenant_id=tenant.id,
        job_id=job.id,
        candidate_id=candidate.id,
        scorecard_version_id=scorecard.id,
        classification="main",
        score=82,
        score_json={"total": 82},
        scoring_version="matching-v1",
    )
    session.add(row)
    session.flush()
    yield session, CrmService(session, b"unit-idempotency"), row, context
    session.close()
    engine.dispose()


def test_rejection_requires_controlled_reason(
    crm_scenario: tuple[Session, CrmService, JobCandidate, RequestContext],
) -> None:
    _, service, row, context = crm_scenario

    with pytest.raises(CrmError, match="rejection_reason_required"):
        service.transition(
            context,
            row.id,
            CandidateStage.REJECTED,
            reason_code=None,
            note=None,
            idempotency_key="reject-without-reason",
        )

    with pytest.raises(CrmError, match="rejection_reason_invalid"):
        service.transition(
            context,
            row.id,
            CandidateStage.REJECTED,
            reason_code="free form reason",
            note=None,
            idempotency_key="reject-free-form",
        )


@pytest.mark.parametrize(
    ("start", "target"),
    (
        (CandidateStage.NEW, CandidateStage.REVIEWED),
        (CandidateStage.NEW, CandidateStage.SHORTLISTED),
        (CandidateStage.NEW, CandidateStage.REJECTED),
        (CandidateStage.REVIEWED, CandidateStage.SHORTLISTED),
        (CandidateStage.REVIEWED, CandidateStage.REJECTED),
        (CandidateStage.SHORTLISTED, CandidateStage.REVIEWED),
        (CandidateStage.SHORTLISTED, CandidateStage.REJECTED),
        (CandidateStage.REJECTED, CandidateStage.REVIEWED),
    ),
)
def test_allowed_transition_records_one_actor_activity_and_replays_safely(
    crm_scenario: tuple[Session, CrmService, JobCandidate, RequestContext],
    start: CandidateStage,
    target: CandidateStage,
) -> None:
    session, service, row, context = crm_scenario
    row.stage = start
    row.rejection_reason_code = (
        "not_qualified" if start is CandidateStage.REJECTED else None
    )
    reason = "not_qualified" if target is CandidateStage.REJECTED else None

    first = service.transition(
        context,
        row.id,
        target,
        reason_code=reason,
        note="Does not meet the must-haves" if reason else None,
        idempotency_key=f"transition-{start.value}-{target.value}",
    )
    replay = service.transition(
        context,
        row.id,
        target,
        reason_code=reason,
        note="Does not meet the must-haves" if reason else None,
        idempotency_key=f"transition-{start.value}-{target.value}",
    )

    assert first.id == replay.id == row.id
    assert first.stage is target
    assert session.scalar(select(func.count()).select_from(ActivityEvent)) == 1
    activity = session.scalar(select(ActivityEvent))
    assert activity is not None
    assert activity.actor_user_id == context.user_id
    assert activity.payload == {
        "from_stage": start.value,
        "to_stage": target.value,
        "reason_code": reason,
    }


@pytest.mark.parametrize(
    ("start", "target"),
    (
        (CandidateStage.REVIEWED, CandidateStage.NEW),
        (CandidateStage.SHORTLISTED, CandidateStage.NEW),
        (CandidateStage.REJECTED, CandidateStage.NEW),
        (CandidateStage.REJECTED, CandidateStage.SHORTLISTED),
    ),
)
def test_forbidden_transition_does_not_mutate_or_record_activity(
    crm_scenario: tuple[Session, CrmService, JobCandidate, RequestContext],
    start: CandidateStage,
    target: CandidateStage,
) -> None:
    session, service, row, context = crm_scenario
    row.stage = start
    row.rejection_reason_code = (
        "not_qualified" if start is CandidateStage.REJECTED else None
    )

    with pytest.raises(CrmError, match="stage_transition_invalid"):
        service.transition(
            context,
            row.id,
            target,
            reason_code=None,
            note=None,
            idempotency_key=f"forbidden-{start.value}-{target.value}",
        )

    assert row.stage is start
    assert session.scalar(select(func.count()).select_from(ActivityEvent)) == 0
