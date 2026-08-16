import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.candidates.contacts import ContactService
from app.candidates.models import ContactPoint
from app.core import database
from app.core.database import get_db
from app.core.telemetry import Telemetry, identifier_digest
from app.crm.models import ActivityEvent, CandidateStage, JobCandidate
from app.identity.models import IdentityIdempotencyKey
from app.identity.schemas import RequestContext, Role
from app.providers.base import ProviderContact


class _CommitFailureSession(Session):
    def commit(self) -> None:
        self.rollback()
        raise RuntimeError("forced commit failure")


def _client_with_commit_failure(
    crm_api: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    engine = crm_api["engine"]

    def failing_session_factory() -> _CommitFailureSession:
        return _CommitFailureSession(bind=engine, expire_on_commit=False)

    app = crm_api["api"].app  # type: ignore[union-attr]
    app.dependency_overrides.pop(get_db)
    monkeypatch.setattr(database, "session_factory", failing_session_factory)
    return TestClient(app, raise_server_exceptions=False)


def test_commit_failure_emits_no_success_for_ordinary_mutation(
    crm_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = {
        **crm_api["headers"],
        "Idempotency-Key": "commit-failure-stage-change",
    }
    with _client_with_commit_failure(crm_api, monkeypatch) as api:
        response = api.patch(
            f"/api/v1/job-candidates/{crm_api['priya_row_id']}/stage",
            headers=headers,
            json={"stage": CandidateStage.SHORTLISTED.value},
        )

    assert response.status_code == 500
    assert '"stage":"Shortlisted"' not in response.text
    with Session(crm_api["engine"]) as session:
        row = session.get(JobCandidate, crm_api["priya_row_id"])
        assert row is not None
        assert row.stage is CandidateStage.NEW


def test_commit_failure_emits_no_revealed_plaintext(
    crm_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = {
        **crm_api["headers"],
        "Idempotency-Key": "commit-failure-contact-reveal",
    }
    with _client_with_commit_failure(crm_api, monkeypatch) as api:
        response = api.post(
            f"/api/v1/contact-points/{crm_api['work_email_id']}/reveal",
            headers=headers,
        )

    assert response.status_code == 500
    assert "priya@example.test" not in response.text


def test_reveal_updates_legitimate_use_audits_once_and_never_caches_plaintext(
    crm_api,
) -> None:
    captured: list[dict[str, object]] = []
    crm_api["api"].app.state.telemetry = Telemetry(
        hmac_key=b"telemetry-test-key", sink=captured.append
    )
    headers = {**crm_api["headers"], "Idempotency-Key": "reveal-priya-work-email"}
    url = f"/api/v1/contact-points/{crm_api['work_email_id']}/reveal"

    first = crm_api["api"].post(url, headers=headers)
    with Session(crm_api["engine"]) as session:
        first_point = session.get(ContactPoint, crm_api["work_email_id"])
        assert first_point is not None
        first_last_used_at = first_point.last_used_at
        first_expires_at = first_point.expires_at
    replay = crm_api["api"].post(url, headers=headers)

    assert first.status_code == replay.status_code == 200
    assert (
        first.json()
        == replay.json()
        == {
            "id": str(crm_api["work_email_id"]),
            "value": "priya@example.test",
        }
    )
    with Session(crm_api["engine"]) as session:
        point = session.get(ContactPoint, crm_api["work_email_id"])
        assert point is not None
        assert point.last_used_at == first_last_used_at
        assert point.expires_at == first_expires_at
        assert point.expires_at > point.last_used_at
        assert (
            session.scalar(
                select(func.count())
                .select_from(ActivityEvent)
                .where(ActivityEvent.action == "candidate.contact_revealed")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "candidate.contact_revealed")
            )
            == 1
        )
        ledger = session.scalar(
            select(IdentityIdempotencyKey).where(
                IdentityIdempotencyKey.operation.like("crm_contact_reveal:%")
            )
        )
        assert ledger is not None
        assert "priya@example.test" not in str(ledger.response_payload)
    reveal_events = [
        event for event in captured if event["event"] == "contact_revealed"
    ]
    assert reveal_events
    assert reveal_events[0]["user_hash"] == identifier_digest(
        crm_api["recruiter_id"], b"telemetry-test-key"
    )
    assert reveal_events[0]["candidate_hash"] == identifier_digest(
        crm_api["priya_id"], b"telemetry-test-key"
    )
    assert "priya@example.test" not in json.dumps(captured)


def test_reveal_hides_ungranted_contact_existence_and_rejects_exact_expiry(
    crm_api,
) -> None:
    context = RequestContext(
        tenant_id=crm_api["tenant_id"],
        user_id=crm_api["recruiter_id"],
        role=Role.RECRUITER,
        allowed_client_ids=frozenset((crm_api["granted_client_id"],)),
    )
    with Session(crm_api["engine"], expire_on_commit=False) as session:
        contacts = ContactService(session, crm_api["cipher"])
        hidden = contacts.store(
            context,
            crm_api["hidden_candidate_id"],
            ProviderContact(
                kind="email",
                value="hidden@example.test",
                verification_state="verified",
                observed_at=datetime.now(UTC),
            ),
        ).contact_point
        expired = contacts.store(
            context,
            crm_api["jamal_id"],
            ProviderContact(
                kind="phone",
                value="+1 646 555 0101",
                verification_state="verified",
                observed_at=datetime.now(UTC) - timedelta(days=181),
            ),
            processed_at=datetime.now(UTC) - timedelta(days=181),
        ).contact_point
        deadline = expired.expires_at
        session.commit()
        hidden_id = hidden.id
        expired_id = expired.id

    hidden_response = crm_api["api"].post(
        f"/api/v1/contact-points/{hidden_id}/reveal",
        headers={**crm_api["headers"], "Idempotency-Key": "reveal-hidden"},
    )
    absent_response = crm_api["api"].post(
        f"/api/v1/contact-points/{uuid4()}/reveal",
        headers={**crm_api["headers"], "Idempotency-Key": "reveal-absent"},
    )
    expired_response = crm_api["api"].post(
        f"/api/v1/contact-points/{expired_id}/reveal",
        headers={**crm_api["headers"], "Idempotency-Key": "reveal-expired"},
    )

    assert hidden_response.status_code == absent_response.status_code == 404
    assert (
        hidden_response.json()
        == absent_response.json()
        == {"detail": {"code": "contact_point_not_found"}}
    )
    assert expired_response.status_code == 410
    assert expired_response.json() == {"detail": {"code": "contact_expired"}}
    with Session(crm_api["engine"]) as session:
        point = session.get(ContactPoint, expired_id)
        assert point is not None
        assert point.expires_at == deadline.replace(tzinfo=None)
        assert point.value_ciphertext is None
        assert point.encrypted_data_key is None
