import csv
import io

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.candidates.models import ContactPoint
from app.crm.models import ActivityEvent
from app.identity.models import IdentityIdempotencyKey


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
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "candidate.shortlist_exported")
            )
            == 1
        )
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
