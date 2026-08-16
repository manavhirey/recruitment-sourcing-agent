import csv
import io

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.candidates.models import ContactPoint
from app.crm.exports import export_shortlist_csv
from app.crm.models import ActivityEvent
from app.identity.models import IdentityIdempotencyKey
from app.identity.schemas import RequestContext, Role


def test_export_streams_only_authorized_shortlist_with_formula_safe_plaintext(
    crm_api,
) -> None:
    headers = {**crm_api["headers"], "Idempotency-Key": "export-shortlist"}
    url = f"/api/v1/jobs/{crm_api['job_id']}/export.csv"

    response = crm_api["api"].get(url, headers=headers)
    with Session(crm_api["engine"]) as session:
        first_point = session.get(ContactPoint, crm_api["phone_id"])
        assert first_point is not None
        first_last_used_at = first_point.last_used_at
        first_expires_at = first_point.expires_at
    replay = crm_api["api"].get(url, headers=headers)

    assert response.status_code == replay.status_code == 200
    assert response.text == replay.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"].endswith('shortlist.csv"')
    assert "content-length" not in response.headers
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert list(rows[0]) == [
        "candidate_id",
        "name",
        "current_title",
        "current_company",
        "location",
        "work_email",
        "personal_email",
        "phone",
    ]
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == str(crm_api["formula_id"])
    assert rows[0]["name"] == "'=2+3"
    assert rows[0]["phone"] == "'+12125550112"
    assert rows[0]["work_email"] == ""
    serialized = response.text.casefold()
    assert "priya sharma" not in serialized
    assert str(crm_api["hidden_candidate_id"]) not in serialized
    for forbidden in (
        "ciphertext",
        "raw_snapshot",
        "encrypted_data_key",
        "lookup_hmac",
        "suppression_hmac_key",
    ):
        assert forbidden not in serialized

    with Session(crm_api["engine"]) as session:
        replayed_point = session.get(ContactPoint, crm_api["phone_id"])
        assert replayed_point is not None
        assert replayed_point.last_used_at == first_last_used_at
        assert replayed_point.expires_at == first_expires_at
        audit_counts = dict(
            session.execute(
                select(AuditEvent.action, func.count())
                .where(
                    AuditEvent.action.in_(
                        (
                            "candidate.shortlist_export_started",
                            "candidate.contact_exported",
                            "candidate.shortlist_export_completed",
                        )
                    )
                )
                .group_by(AuditEvent.action)
            ).all()
        )
        assert audit_counts == {
            "candidate.shortlist_export_started": 1,
            "candidate.contact_exported": 1,
            "candidate.shortlist_export_completed": 1,
        }
        assert (
            session.scalar(
                select(func.count())
                .select_from(ActivityEvent)
                .where(ActivityEvent.action == "candidate.shortlist_exported")
            )
            == 1
        )
        ledger = session.scalar(
            select(IdentityIdempotencyKey).where(
                IdentityIdempotencyKey.operation.like("crm_export:%")
            )
        )
        assert ledger is not None
        assert "+12125550112" not in str(ledger.response_payload)


def test_export_derives_idempotency_and_hides_ungranted_job(crm_api) -> None:
    derived = crm_api["api"].get(
        f"/api/v1/jobs/{crm_api['job_id']}/export.csv",
        headers=crm_api["headers"],
    )
    hidden = crm_api["api"].get(
        f"/api/v1/jobs/{crm_api['hidden_job_id']}/export.csv",
        headers=crm_api["headers"],
    )

    assert derived.status_code == 200
    assert hidden.status_code == 404
    assert hidden.json() == {"detail": {"code": "job_candidate_not_found"}}


def test_aborted_stream_keeps_previously_emitted_plaintext_audited(crm_api) -> None:
    context = RequestContext(
        tenant_id=crm_api["tenant_id"],
        user_id=crm_api["recruiter_id"],
        role=Role.RECRUITER,
        allowed_client_ids=frozenset((crm_api["granted_client_id"],)),
    )
    with Session(crm_api["engine"], expire_on_commit=False) as session:
        stream = export_shortlist_csv(
            session,
            crm_api["cipher"],
            context,
            crm_api["job_id"],
            authorization_hmac_key=b"test-suppression-key",
            idempotency_key="abort-after-first-row",
        )
        header = next(stream)
        emitted_row = next(stream)
        stream.close()

    assert "candidate_id" in header
    assert "+12125550112" in emitted_row
    with Session(crm_api["engine"]) as session:
        point = session.get(ContactPoint, crm_api["phone_id"])
        assert point is not None
        assert point.last_used_at is not None
        actions = session.scalars(
            select(AuditEvent.action).where(
                AuditEvent.action.in_(
                    (
                        "candidate.shortlist_export_started",
                        "candidate.contact_exported",
                        "candidate.shortlist_export_aborted",
                    )
                )
            )
        ).all()
        assert set(actions) == {
            "candidate.shortlist_export_started",
            "candidate.contact_exported",
            "candidate.shortlist_export_aborted",
        }
        assert (
            session.scalar(
                select(func.count())
                .select_from(ActivityEvent)
                .where(ActivityEvent.action == "candidate.shortlist_exported")
            )
            == 1
        )
        payloads = session.scalars(
            select(AuditEvent.payload).where(
                AuditEvent.action.in_(actions)
            )
        ).all()
        assert "+12125550112" not in str(payloads)


def test_plaintext_row_is_not_yielded_when_its_audit_commit_fails(crm_api) -> None:
    class FailRowCommitSession(Session):
        commit_calls = 0

        def commit(self) -> None:
            self.commit_calls += 1
            if self.commit_calls == 2:
                self.rollback()
                raise RuntimeError("forced row audit commit failure")
            super().commit()

    context = RequestContext(
        tenant_id=crm_api["tenant_id"],
        user_id=crm_api["recruiter_id"],
        role=Role.RECRUITER,
        allowed_client_ids=frozenset((crm_api["granted_client_id"],)),
    )
    emitted: list[str] = []
    with FailRowCommitSession(crm_api["engine"], expire_on_commit=False) as session:
        stream = export_shortlist_csv(
            session,
            crm_api["cipher"],
            context,
            crm_api["job_id"],
            authorization_hmac_key=b"test-suppression-key",
            idempotency_key="fail-before-yield",
        )
        emitted.append(next(stream))
        with pytest.raises(RuntimeError, match="forced row audit commit failure"):
            emitted.append(next(stream))

    assert "+12125550112" not in "".join(emitted)
    with Session(crm_api["engine"]) as session:
        point = session.get(ContactPoint, crm_api["phone_id"])
        assert point is not None
        assert point.last_used_at is None
        actions = session.scalars(
            select(AuditEvent.action).where(
                AuditEvent.action.in_(
                    (
                        "candidate.shortlist_export_started",
                        "candidate.contact_exported",
                        "candidate.shortlist_export_completed",
                        "candidate.shortlist_export_aborted",
                    )
                )
            )
        ).all()
        assert set(actions) == {
            "candidate.shortlist_export_started",
            "candidate.shortlist_export_aborted",
        }


def test_header_only_abort_same_key_retry_audits_each_contact_before_yield(
    crm_api,
) -> None:
    context = RequestContext(
        tenant_id=crm_api["tenant_id"],
        user_id=crm_api["recruiter_id"],
        role=Role.RECRUITER,
        allowed_client_ids=frozenset((crm_api["granted_client_id"],)),
    )
    intent = "header-only-abort-retry"
    with Session(crm_api["engine"], expire_on_commit=False) as session:
        first = export_shortlist_csv(
            session,
            crm_api["cipher"],
            context,
            crm_api["job_id"],
            authorization_hmac_key=b"test-suppression-key",
            idempotency_key=intent,
        )
        assert "candidate_id" in next(first)
        first.close()

    with Session(crm_api["engine"], expire_on_commit=False) as session:
        retried = export_shortlist_csv(
            session,
            crm_api["cipher"],
            context,
            crm_api["job_id"],
            authorization_hmac_key=b"test-suppression-key",
            idempotency_key=intent,
        )
        output = "".join(retried)

    assert "+12125550112" in output
    with Session(crm_api["engine"]) as session:
        point = session.get(ContactPoint, crm_api["phone_id"])
        assert point is not None and point.last_used_at is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "candidate.contact_exported")
            )
            == 1
        )


def test_failed_row_commit_same_key_retry_records_missing_contact_audit(
    crm_api,
) -> None:
    class FailRowCommitSession(Session):
        commit_calls = 0

        def commit(self) -> None:
            self.commit_calls += 1
            if self.commit_calls == 2:
                self.rollback()
                raise RuntimeError("forced row audit commit failure")
            super().commit()

    context = RequestContext(
        tenant_id=crm_api["tenant_id"],
        user_id=crm_api["recruiter_id"],
        role=Role.RECRUITER,
        allowed_client_ids=frozenset((crm_api["granted_client_id"],)),
    )
    intent = "failed-row-commit-retry"
    with FailRowCommitSession(crm_api["engine"], expire_on_commit=False) as session:
        first = export_shortlist_csv(
            session,
            crm_api["cipher"],
            context,
            crm_api["job_id"],
            authorization_hmac_key=b"test-suppression-key",
            idempotency_key=intent,
        )
        next(first)
        with pytest.raises(RuntimeError, match="forced row audit commit failure"):
            next(first)

    with Session(crm_api["engine"], expire_on_commit=False) as session:
        retried = export_shortlist_csv(
            session,
            crm_api["cipher"],
            context,
            crm_api["job_id"],
            authorization_hmac_key=b"test-suppression-key",
            idempotency_key=intent,
        )
        output = "".join(retried)

    assert "+12125550112" in output
    with Session(crm_api["engine"]) as session:
        point = session.get(ContactPoint, crm_api["phone_id"])
        assert point is not None and point.last_used_at is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "candidate.contact_exported")
            )
            == 1
        )
