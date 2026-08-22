from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.candidates.models import Candidate
from app.clients.models import ClientCompany
from app.core.database import Base
from app.crm.models import ActivityEvent, CandidateStage, JobCandidate
from app.crm.service import materialize_run_matches
from app.identity.models import Tenant, User
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardCriterionRecord, ScorecardVersion
from app.sourcing.models import RunCandidate, SourcingRun
from app.sourcing.state_machine import RunState
from app.sourcing.tasks import execute_match_run


def test_match_materialization_replays_and_preserves_human_state_on_rescore() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(slug=f"crm-materialize-{uuid4()}")
        actor = User(
            oidc_subject=f"crm-materialize|{uuid4()}",
            email=f"{uuid4()}@example.test",
            display_name="Sourcing Actor",
        )
        session.add_all((tenant, actor))
        session.flush()
        client = ClientCompany(
            tenant_id=tenant.id,
            name="Client",
            normalized_name=f"client-{uuid4()}",
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
            owner_user_id=actor.id,
            title="Product Manager",
            job_description="Find a product manager.",
        )
        session.add(job)
        session.flush()
        scorecards = [
            ScorecardVersion(
                tenant_id=tenant.id,
                job_id=job.id,
                version=version,
                target_titles=["Product Manager"],
                seniority=[],
                minimum_years=None,
                maximum_years=None,
                locations=[],
                industry_code="technology.fintech",
                suggested_adjacent_industries=[],
                uncertainties=[],
                extraction_status="ready",
                confirmed_by_user_id=actor.id,
            )
            for version in (1, 2)
        ]
        session.add_all(scorecards)
        session.flush()
        context = RequestContext(
            tenant_id=tenant.id,
            user_id=actor.id,
            role=Role.OWNER,
        )

        first_run = _run(session, job, scorecards[0], actor)
        first_match = _match(session, first_run, candidate, scorecards[0], 82, "main")
        first = materialize_run_matches(session, first_run, context)
        replay = materialize_run_matches(session, first_run, context)

        assert [row.id for row in first] == [row.id for row in replay]
        assert first[0].stage is CandidateStage.NEW
        assert first[0].latest_run_id == first_run.id
        assert first[0].score_json == first_match.evidence
        assert session.scalar(select(func.count()).select_from(JobCandidate)) == 1
        assert session.scalar(select(func.count()).select_from(ActivityEvent)) == 1

        owner_id = uuid4()
        first[0].stage = CandidateStage.REVIEWED
        first[0].owner_user_id = owner_id
        second_run = _run(session, job, scorecards[1], actor)
        second_match = _match(
            session, second_run, candidate, scorecards[1], 91, "near_match"
        )
        rescored = materialize_run_matches(session, second_run, context)

        assert rescored[0].id == first[0].id
        assert rescored[0].stage is CandidateStage.REVIEWED
        assert rescored[0].owner_user_id == owner_id
        assert rescored[0].score == 91
        assert rescored[0].classification == "near_match"
        assert rescored[0].scorecard_version_id == scorecards[1].id
        assert rescored[0].latest_run_id == second_run.id
        assert rescored[0].score_json == second_match.evidence
        assert session.scalar(select(func.count()).select_from(ActivityEvent)) == 2

    engine.dispose()


def test_matching_task_materializes_completed_matches_before_enrichment() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        tenant = Tenant(slug=f"crm-task-{uuid4()}")
        actor = User(
            oidc_subject=f"crm-task|{uuid4()}",
            email=f"{uuid4()}@example.test",
            display_name="Sourcing Actor",
        )
        session.add_all((tenant, actor))
        session.flush()
        client = ClientCompany(
            tenant_id=tenant.id,
            name="Client",
            normalized_name=f"client-{uuid4()}",
        )
        candidate = Candidate(
            tenant_id=tenant.id,
            full_name="Priya Sharma",
            normalized_name="priya sharma",
            current_title="Product Manager",
            normalized_title="product manager",
        )
        session.add_all((client, candidate))
        session.flush()
        job = Job(
            tenant_id=tenant.id,
            client_id=client.id,
            owner_user_id=actor.id,
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
            confirmed_by_user_id=actor.id,
        )
        session.add(scorecard)
        session.flush()
        session.add(
            ScorecardCriterionRecord(
                tenant_id=tenant.id,
                scorecard_version_id=scorecard.id,
                position=0,
                key="payments",
                label="Payments experience",
                kind="preference",
                evidence_required=False,
                inferred=False,
                recruiter_entered=True,
                lawful_requirement_confirmed=False,
            )
        )
        job.current_scorecard_id = scorecard.id
        run = SourcingRun(
            tenant_id=tenant.id,
            job_id=job.id,
            scorecard_version_id=scorecard.id,
            started_by_user_id=actor.id,
            state=RunState.MATCHING,
            current_stage=RunState.MATCHING.value,
        )
        session.add(run)
        session.flush()
        session.add(
            RunCandidate(
                tenant_id=tenant.id,
                run_id=run.id,
                candidate_id=candidate.id,
                scorecard_version_id=scorecard.id,
            )
        )
        session.commit()
        context = RequestContext(
            tenant_id=tenant.id,
            user_id=actor.id,
            role=Role.OWNER,
        )
        run_id = run.id

    execute_match_run(factory, run_id, context)

    with factory() as session:
        crm_row = session.scalar(select(JobCandidate))
        run = session.get(SourcingRun, run_id)
        assert crm_row is not None
        assert crm_row.stage is CandidateStage.NEW
        assert crm_row.latest_run_id == run_id
        assert run is not None and run.state is RunState.ENRICHING

    engine.dispose()


def _run(
    session: Session,
    job: Job,
    scorecard: ScorecardVersion,
    actor: User,
) -> SourcingRun:
    run = SourcingRun(
        tenant_id=job.tenant_id,
        job_id=job.id,
        scorecard_version_id=scorecard.id,
        started_by_user_id=actor.id,
        state=RunState.READY,
        current_stage=RunState.READY.value,
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    return run


def _match(
    session: Session,
    run: SourcingRun,
    candidate: Candidate,
    scorecard: ScorecardVersion,
    score: int,
    classification: str,
) -> RunCandidate:
    match = RunCandidate(
        tenant_id=run.tenant_id,
        run_id=run.id,
        candidate_id=candidate.id,
        scorecard_version_id=scorecard.id,
        match_score=score,
        classification=classification,
        evidence={"total": score, "criteria": []},
        scoring_version="matching-v1",
        matched_at=datetime.now(UTC),
    )
    session.add(match)
    session.flush()
    return match
