from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.candidates.models import (
    Candidate,
    CandidateExperience,
    CandidateFieldProvenance,
    SourceIdentity,
)
from app.crm.models import ActivityEvent, CandidateNote, CandidateStage, JobCandidate
from app.crm.service import CrmService
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardCriterionRecord, ScorecardVersion
from app.sourcing.models import SourcingRun
from app.sourcing.state_machine import RunState


def test_ranked_review_routes_apply_all_filters_and_stable_score_cursor(
    crm_api,
) -> None:
    api = crm_api["api"]
    headers = crm_api["headers"]
    base = f"/api/v1/jobs/{crm_api['job_id']}/candidates"

    first = api.get(
        base, headers=headers, params={"classification": "main", "limit": 1}
    )
    assert first.status_code == 200
    assert [item["candidate_id"] for item in first.json()["items"]] == [
        str(crm_api["priya_id"])
    ]
    assert first.json()["next_cursor"]
    second = api.get(
        base,
        headers=headers,
        params={
            "classification": "main",
            "limit": 1,
            "cursor": first.json()["next_cursor"],
        },
    )
    assert second.status_code == 200
    assert [item["candidate_id"] for item in second.json()["items"]] == [
        str(crm_api["jamal_id"])
    ]
    cross_filter = api.get(
        base,
        headers=headers,
        params={
            "classification": "near_match",
            "limit": 1,
            "cursor": first.json()["next_cursor"],
        },
    )
    assert cross_filter.status_code == 400
    assert cross_filter.json() == {"detail": {"code": "cursor_invalid"}}
    assert api.get(
        base, headers=headers, params={"classification": "near_match"}
    ).json()["items"][0]["candidate_id"] == str(crm_api["formula_id"])

    filters = (
        ({"score_min": 85, "score_max": 95}, crm_api["priya_id"]),
        ({"stage": CandidateStage.REVIEWED.value}, crm_api["jamal_id"]),
        ({"owner": str(crm_api["assignee_id"])}, crm_api["priya_id"]),
        ({"tags": "urgent"}, crm_api["priya_id"]),
        ({"location": "New York"}, crm_api["priya_id"]),
        ({"industry": "technology.fintech"}, crm_api["priya_id"]),
        ({"has_contact": "true"}, crm_api["priya_id"]),
        ({"q": "payment processing"}, crm_api["priya_id"]),
    )
    for params, expected_candidate_id in filters:
        response = api.get(base, headers=headers, params=params)
        assert response.status_code == 200, (params, response.text)
        assert [item["candidate_id"] for item in response.json()["items"]] == [
            str(expected_candidate_id)
        ]


def test_near_match_list_exposes_only_safe_mandatory_gap_summaries(crm_api) -> None:
    with Session(crm_api["engine"]) as session:
        row = session.get(JobCandidate, crm_api["formula_row_id"])
        assert row is not None
        session.add_all(
            (
                ScorecardCriterionRecord(
                    tenant_id=crm_api["tenant_id"],
                    scorecard_version_id=row.scorecard_version_id,
                    position=0,
                    key="payments",
                    label="Payments experience",
                    kind="must_have",
                    evidence_required=False,
                    source_text="private job description text",
                    inferred=False,
                    recruiter_entered=False,
                    lawful_requirement_confirmed=False,
                ),
                ScorecardCriterionRecord(
                    tenant_id=crm_api["tenant_id"],
                    scorecard_version_id=row.scorecard_version_id,
                    position=1,
                    key="work_eligibility",
                    label="Work eligibility",
                    kind="must_have",
                    evidence_required=True,
                    source_text="private eligibility text",
                    inferred=False,
                    recruiter_entered=False,
                    lawful_requirement_confirmed=True,
                ),
            )
        )
        row.score_json = {
            "total": 70,
            "criteria": [
                {
                    "key": "payments",
                    "label": "Payments experience",
                    "state": "failed",
                    "summary": "Missing required payments experience",
                    "evidence": ["provider-private-evidence"],
                    "source_refs": ["provider:private:1"],
                },
                {
                    "key": "work_eligibility",
                    "label": "Work eligibility",
                    "state": "unknown",
                    "summary": "Work eligibility is unknown",
                    "evidence": [],
                    "source_refs": [],
                },
                {
                    "key": "optional_unknown",
                    "label": "Optional unknown",
                    "state": "unknown",
                    "summary": "Optional evidence is unknown",
                    "evidence": [],
                    "source_refs": [],
                },
            ],
            "failed_must_haves": ["payments"],
            "unknown_keys": ["optional_unknown", "work_eligibility"],
            "provider_body": {"secret": "never-return"},
        }
        session.commit()

    response = crm_api["api"].get(
        f"/api/v1/jobs/{crm_api['job_id']}/candidates",
        headers=crm_api["headers"],
        params={"classification": "near_match"},
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["score_json"] is None
    assert item["mandatory_gaps"] == [
        {
            "key": "payments",
            "label": "Payments experience",
            "state": "failed",
            "summary": "Stored evidence does not support Payments experience.",
        },
        {
            "key": "work_eligibility",
            "label": "Work eligibility",
            "state": "unknown",
            "summary": "Evidence for Work eligibility is unknown.",
        },
    ]
    assert "provider-private-evidence" not in response.text
    assert "private job description text" not in response.text
    assert "never-return" not in response.text


def test_detail_masks_contact_and_hidden_resources_return_same_not_found(
    crm_api,
) -> None:
    api = crm_api["api"]
    headers = crm_api["headers"]
    detail = api.get(
        f"/api/v1/job-candidates/{crm_api['priya_row_id']}", headers=headers
    )

    assert detail.status_code == 200
    assert detail.json()["contacts"] == [
        {
            "id": str(crm_api["work_email_id"]),
            "kind": "email",
            "classification": "work",
            "verification_state": "verified",
            "masked_value": "••••@••••",
            "expires_at": detail.json()["contacts"][0]["expires_at"],
        }
    ]
    serialized = detail.text.casefold()
    assert "priya@example.test" not in serialized
    assert "ciphertext" not in serialized
    hidden_detail = api.get(
        f"/api/v1/job-candidates/{crm_api['hidden_row_id']}", headers=headers
    )
    hidden_job = api.get(
        f"/api/v1/jobs/{crm_api['hidden_job_id']}/candidates", headers=headers
    )
    assert hidden_detail.status_code == hidden_job.status_code == 404
    assert (
        hidden_detail.json()
        == hidden_job.json()
        == {"detail": {"code": "job_candidate_not_found"}}
    )


def test_detail_returns_normalized_experience_provenance_notes_and_run_candidate_id(
    crm_api,
) -> None:
    observed_at = datetime(2026, 8, 10, tzinfo=UTC)
    with Session(crm_api["engine"]) as session:
        row = session.get(JobCandidate, crm_api["priya_row_id"])
        assert row is not None
        run = SourcingRun(
            tenant_id=crm_api["tenant_id"],
            job_id=row.job_id,
            scorecard_version_id=row.scorecard_version_id,
            started_by_user_id=crm_api["recruiter_id"],
            state=RunState.READY,
            current_stage=RunState.READY.value,
        )
        session.add(run)
        session.flush()
        row.latest_run_id = run.id
        from app.sourcing.models import RunCandidate

        run_candidate = RunCandidate(
            tenant_id=crm_api["tenant_id"],
            run_id=run.id,
            candidate_id=row.candidate_id,
            scorecard_version_id=row.scorecard_version_id,
            match_score=row.score,
            classification=row.classification,
            scoring_version=row.scoring_version,
            enrichment_status="available",
        )
        source = SourceIdentity(
            tenant_id=crm_api["tenant_id"],
            candidate_id=row.candidate_id,
            provider="apollo",
            provider_person_id="provider-private-id",
            source_timestamp=observed_at,
            confidence=0.9,
        )
        session.add_all((run_candidate, source))
        session.flush()
        session.add_all(
            (
                CandidateExperience(
                    tenant_id=crm_api["tenant_id"],
                    candidate_id=row.candidate_id,
                    source_identity_id=source.id,
                    position=0,
                    title="Senior Product Manager",
                    company_name="PayFlow",
                    start_date="2021-01",
                    end_date=None,
                    provider="apollo",
                    source_timestamp=observed_at,
                    observed_value_hash="a" * 64,
                    confidence=0.9,
                ),
                CandidateFieldProvenance(
                    tenant_id=crm_api["tenant_id"],
                    candidate_id=row.candidate_id,
                    source_identity_id=source.id,
                    field_name="current_title",
                    provider="apollo",
                    source_timestamp=observed_at,
                    observed_value_hash="b" * 64,
                    confidence=0.9,
                    is_current=True,
                ),
                CandidateNote(
                    tenant_id=crm_api["tenant_id"],
                    job_candidate_id=row.id,
                    actor_user_id=crm_api["recruiter_id"],
                    body="Strong payments discovery examples.",
                ),
            )
        )
        session.commit()
        run_candidate_id = run_candidate.id

    response = crm_api["api"].get(
        f"/api/v1/job-candidates/{crm_api['priya_row_id']}",
        headers=crm_api["headers"],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_candidate_id"] == str(run_candidate_id)
    assert payload["experiences"] == [
        {
            "title": "Senior Product Manager",
            "company_name": "PayFlow",
            "start_date": "2021-01",
            "end_date": None,
            "provider": "apollo",
            "source_timestamp": observed_at.isoformat().replace("+00:00", "Z"),
        }
    ]
    assert payload["provenance"] == [
        {
            "field_name": "current_title",
            "provider": "apollo",
            "source_timestamp": observed_at.isoformat().replace("+00:00", "Z"),
        }
    ]
    assert payload["notes"][0]["body"] == "Strong payments discovery examples."
    assert "provider-private-id" not in response.text
    assert "observed_value_hash" not in response.text


def test_activity_response_allowlists_actions_and_omits_stored_payloads(crm_api) -> None:
    with Session(crm_api["engine"]) as session:
        session.add_all(
            (
                ActivityEvent(
                    tenant_id=crm_api["tenant_id"],
                    job_candidate_id=crm_api["priya_row_id"],
                    actor_user_id=crm_api["recruiter_id"],
                    event_key="safe-action-with-private-payload",
                    action="candidate.stage_changed",
                    payload={"provider_error": "private-error", "token": "secret"},
                ),
                ActivityEvent(
                    tenant_id=crm_api["tenant_id"],
                    job_candidate_id=crm_api["priya_row_id"],
                    actor_user_id=crm_api["recruiter_id"],
                    event_key="private-action",
                    action="provider.raw_response_received",
                    payload={"body": "private-provider-body"},
                ),
            )
        )
        session.commit()

    response = crm_api["api"].get(
        f"/api/v1/job-candidates/{crm_api['priya_row_id']}/activity",
        headers=crm_api["headers"],
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["action"] == "candidate.stage_changed"
    assert set(response.json()["items"][0]) == {"id", "action", "created_at"}
    assert "provider.raw_response_received" not in response.text
    assert "private-error" not in response.text
    assert "private-provider-body" not in response.text


def test_mutations_require_idempotency_replay_and_append_actor_activity(
    crm_api,
) -> None:
    api = crm_api["api"]
    headers = crm_api["headers"]
    row_url = f"/api/v1/job-candidates/{crm_api['priya_row_id']}"

    missing = api.patch(f"{row_url}/stage", headers=headers, json={"stage": "Rejected"})
    invalid = api.patch(
        f"{row_url}/stage",
        headers={**headers, "Idempotency-Key": "reject-invalid"},
        json={"stage": "Rejected"},
    )
    assert missing.status_code == 400
    assert invalid.status_code == 400
    changed = api.patch(
        f"{row_url}/stage",
        headers={**headers, "Idempotency-Key": "reject-priya"},
        json={
            "stage": "Rejected",
            "reason_code": "not_qualified",
            "note": "Missing the required depth.",
        },
    )
    replay = api.patch(
        f"{row_url}/stage",
        headers={**headers, "Idempotency-Key": "reject-priya"},
        json={
            "stage": "Rejected",
            "reason_code": "not_qualified",
            "note": "Missing the required depth.",
        },
    )
    assert changed.status_code == replay.status_code == 200
    assert changed.json() == replay.json()
    assert changed.json()["stage"] == "Rejected"

    note = api.post(
        f"{row_url}/notes",
        headers={**headers, "Idempotency-Key": "note-priya"},
        json={"body": "Strong discovery examples."},
    )
    owner = api.patch(
        f"{row_url}/owner",
        headers={**headers, "Idempotency-Key": "assign-priya"},
        json={"owner_user_id": str(crm_api["recruiter_id"])},
    )
    tags = api.put(
        f"{row_url}/tags",
        headers={**headers, "Idempotency-Key": "tag-priya"},
        json={"tags": ["Priority", "Payments"]},
    )
    assert note.status_code == owner.status_code == tags.status_code == 200
    assert tags.json()["tags"] == ["Payments", "Priority"]

    activity = api.get(f"{row_url}/activity", headers=headers, params={"limit": 2})
    assert activity.status_code == 200
    assert len(activity.json()["items"]) == 2
    assert activity.json()["next_cursor"]
    after = api.get(
        f"{row_url}/activity",
        headers=headers,
        params={"limit": 2, "cursor": activity.json()["next_cursor"]},
    )
    assert len(after.json()["items"]) == 2

    with Session(crm_api["engine"]) as session:
        row = session.get(JobCandidate, crm_api["priya_row_id"])
        assert row is not None
        assert row.rejection_note == "Missing the required depth."
        assert session.scalar(select(func.count()).select_from(CandidateNote)) == 1
        assert session.scalar(select(func.count()).select_from(ActivityEvent)) == 4
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 4


def test_acceptance_uses_fixed_top_twenty_and_exact_finalization_boundaries(
    crm_api,
) -> None:
    ready_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    with Session(crm_api["engine"], expire_on_commit=False) as session:
        job = Job(
            tenant_id=crm_api["tenant_id"],
            client_id=crm_api["granted_client_id"],
            owner_user_id=crm_api["recruiter_id"],
            title="Acceptance Cohort",
            job_description="Measure review acceptance.",
        )
        session.add(job)
        session.flush()
        scorecard = ScorecardVersion(
            tenant_id=crm_api["tenant_id"],
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
            confirmed_by_user_id=crm_api["recruiter_id"],
        )
        session.add(scorecard)
        session.flush()
        job.current_scorecard_id = scorecard.id
        run = SourcingRun(
            tenant_id=crm_api["tenant_id"],
            job_id=job.id,
            scorecard_version_id=scorecard.id,
            started_by_user_id=crm_api["recruiter_id"],
            state=RunState.READY,
            current_stage=RunState.READY.value,
            completed_at=ready_at,
        )
        session.add(run)
        session.flush()
        stages = (
            [CandidateStage.REVIEWED] * 5
            + [CandidateStage.SHORTLISTED] * 3
            + [CandidateStage.REJECTED] * 2
            + [CandidateStage.NEW] * 10
        )
        rows = []
        for position, stage in enumerate(stages):
            candidate = Candidate(
                tenant_id=crm_api["tenant_id"],
                full_name=f"Acceptance {position}",
                normalized_name=f"acceptance {position}",
            )
            session.add(candidate)
            session.flush()
            row = JobCandidate(
                tenant_id=crm_api["tenant_id"],
                job_id=job.id,
                candidate_id=candidate.id,
                latest_run_id=run.id,
                scorecard_version_id=scorecard.id,
                classification="main",
                score=100 - position,
                score_json={"total": 100 - position},
                scoring_version="matching-v1",
                stage=stage,
                rejection_reason_code=(
                    "not_qualified" if stage is CandidateStage.REJECTED else None
                ),
            )
            session.add(row)
            rows.append(row)
        session.commit()
        job_id = job.id

    context = RequestContext(
        tenant_id=crm_api["tenant_id"],
        user_id=crm_api["recruiter_id"],
        role=Role.RECRUITER,
        allowed_client_ids=frozenset((crm_api["granted_client_id"],)),
    )
    with Session(crm_api["engine"]) as session:
        service = CrmService(session, b"acceptance-boundary")
        before = service.acceptance_report(
            context,
            job_id,
            as_of=ready_at + timedelta(days=6, hours=23, minutes=59),
        )
        boundary = service.acceptance_report(
            context,
            job_id,
            as_of=ready_at + timedelta(days=7),
        )
        session.commit()
    assert before.denominator == 20
    assert before.accepted == 8
    assert before.accepted == before.reviewed + before.shortlisted
    assert before.rate == 0.4
    assert before.final is False
    assert boundary.final is True

    with Session(crm_api["engine"]) as session:
        second_run = SourcingRun(
            tenant_id=crm_api["tenant_id"],
            job_id=job_id,
            scorecard_version_id=rows[0].scorecard_version_id,
            started_by_user_id=crm_api["recruiter_id"],
            state=RunState.READY,
            current_stage=RunState.READY.value,
            completed_at=ready_at + timedelta(days=8),
        )
        session.add(second_run)
        for row in session.scalars(
            select(JobCandidate).where(
                JobCandidate.job_id == job_id,
                JobCandidate.stage == CandidateStage.NEW,
            )
        ):
            row.stage = CandidateStage.REJECTED
            row.rejection_reason_code = "other"
        session.commit()
    with Session(crm_api["engine"]) as session:
        early = CrmService(session, b"acceptance-boundary").acceptance_report(
            context,
            job_id,
            as_of=ready_at + timedelta(days=9),
        )
        session.commit()
    assert early.final is True
    assert early.accepted == 8
    assert early.new == 0
    assert early.rejected == 12

    http_report = crm_api["api"].get(
        f"/api/v1/jobs/{job_id}/acceptance",
        headers=crm_api["headers"],
    )
    assert http_report.status_code == 200
    assert http_report.json()["denominator"] == 20

    with Session(crm_api["engine"]) as session:
        changed_after_final = session.scalar(
            select(JobCandidate)
            .where(
                JobCandidate.job_id == job_id,
                JobCandidate.stage == CandidateStage.REVIEWED,
            )
            .limit(1)
        )
        assert changed_after_final is not None
        changed_after_final.stage = CandidateStage.REJECTED
        changed_after_final.rejection_reason_code = "other"
        session.commit()
    with Session(crm_api["engine"]) as session:
        immutable = CrmService(session, b"acceptance-boundary").acceptance_report(
            context,
            job_id,
            as_of=ready_at + timedelta(days=10),
        )
    assert immutable == early


def test_acceptance_route_uses_server_time_not_caller_supplied_as_of(crm_api) -> None:
    now = datetime.now(UTC)
    with Session(crm_api["engine"], expire_on_commit=False) as session:
        job = session.get(Job, crm_api["job_id"])
        assert job is not None and job.current_scorecard_id is not None
        session.add(
            SourcingRun(
                tenant_id=crm_api["tenant_id"],
                job_id=job.id,
                scorecard_version_id=job.current_scorecard_id,
                started_by_user_id=crm_api["recruiter_id"],
                state=RunState.READY,
                current_stage=RunState.READY.value,
                completed_at=now,
            )
        )
        session.commit()

    response = crm_api["api"].get(
        f"/api/v1/jobs/{crm_api['job_id']}/acceptance",
        headers=crm_api["headers"],
        params={"as_of": (now + timedelta(days=365)).isoformat()},
    )

    assert response.status_code == 200
    assert response.json()["final"] is False
