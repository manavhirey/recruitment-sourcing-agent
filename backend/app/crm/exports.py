import csv
import io
from collections.abc import Iterator
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.candidates.contacts import ContactCipher, ContactService
from app.candidates.models import Candidate, ContactPoint
from app.crm.models import CandidateStage, JobCandidate
from app.crm.service import CrmService
from app.identity.schemas import RequestContext

_HEADERS = (
    "candidate_id",
    "name",
    "current_title",
    "current_company",
    "location",
    "work_email",
    "personal_email",
    "phone",
)


def export_shortlist_csv(
    session: Session,
    cipher: ContactCipher,
    context: RequestContext,
    job_id: UUID,
    *,
    authorization_hmac_key: bytes,
    idempotency_key: str,
) -> Iterator[str]:
    service = CrmService(session, authorization_hmac_key, cipher)
    export_record, replayed = service.begin_export(
        context,
        job_id,
        idempotency_key=idempotency_key,
    )

    def rows() -> Iterator[str]:
        yield _csv_row(_HEADERS)
        statement = (
            select(JobCandidate, Candidate)
            .join(
                Candidate,
                and_(
                    Candidate.tenant_id == JobCandidate.tenant_id,
                    Candidate.id == JobCandidate.candidate_id,
                ),
            )
            .where(
                JobCandidate.tenant_id == context.tenant_id,
                JobCandidate.job_id == job_id,
                JobCandidate.stage == CandidateStage.SHORTLISTED,
            )
            .order_by(JobCandidate.score.desc(), JobCandidate.id)
            .execution_options(stream_results=True, yield_per=50)
        )
        for row, candidate in session.execute(statement).tuples():
            fields = {"work_email": "", "personal_email": "", "phone": ""}
            contacts = session.scalars(
                select(ContactPoint)
                .where(
                    ContactPoint.tenant_id == context.tenant_id,
                    ContactPoint.candidate_id == candidate.id,
                    ContactPoint.value_ciphertext.is_not(None),
                    ContactPoint.verification_state != "expired",
                )
                .order_by(
                    ContactPoint.kind, ContactPoint.classification, ContactPoint.id
                )
            )
            contact_service = ContactService(session, cipher)
            for contact in contacts:
                try:
                    value = contact_service.reveal(
                        context,
                        contact.id,
                        record_use=not replayed,
                    )
                except LookupError:
                    continue
                field = (
                    "phone"
                    if contact.kind == "phone"
                    else f"{contact.classification}_email"
                )
                if field in fields and not fields[field]:
                    fields[field] = value
            service.record_exported_candidate(context, row, export_record)
            yield _csv_row(
                (
                    str(candidate.id),
                    candidate.full_name,
                    candidate.current_title or "",
                    candidate.current_company or "",
                    candidate.location or "",
                    fields["work_email"],
                    fields["personal_email"],
                    fields["phone"],
                )
            )

    return rows()


def _csv_row(values: tuple[str, ...]) -> str:
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\r\n").writerow(
        [_formula_safe(value) for value in values]
    )
    return buffer.getvalue()


def _formula_safe(value: str) -> str:
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value
