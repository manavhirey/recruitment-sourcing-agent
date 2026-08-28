from typing import Any
from uuid import uuid4

import pytest

from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job
from app.jobs.schemas import CriterionKind, ScorecardCriterion, ScorecardDraft
from app.jobs.service import JobError, JobService


def _legacy_seniority_draft_payload() -> dict[str, Any]:
    draft = ScorecardDraft(
        target_titles=["Product Manager"],
        criteria=[
            ScorecardCriterion(
                key="payments",
                label="Payments platform experience",
                kind=CriterionKind.MUST_HAVE,
            )
        ],
        seniority=[],
        locations=[],
        industry_code="technology.software",
        suggested_adjacent_industries=[],
        uncertainties=[],
    )
    payload: dict[str, Any] = draft.model_dump(mode="json")
    payload["seniority"] = ["manager"]
    return payload


def test_confirm_scorecard_with_legacy_seniority_requires_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = Job(
        id=uuid4(),
        draft_revision=1,
        draft_payload=_legacy_seniority_draft_payload(),
    )
    service = JobService(session=object(), hmac_key=b"test-hmac-key")
    monkeypatch.setattr(
        service, "get_authorized", lambda context, job_id, **kwargs: job
    )
    context = RequestContext(tenant_id=uuid4(), user_id=uuid4(), role=Role.ADMIN)

    with pytest.raises(JobError) as exc_info:
        service.confirm_scorecard(context, job.id, expected_revision=1)

    assert exc_info.value.code == "scorecard_seniority_revision_required"
