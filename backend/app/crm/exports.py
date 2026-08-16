import csv
import io
from collections.abc import Iterator
from uuid import UUID

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
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
    export_record, _replayed = service.begin_export(
        context,
        job_id,
        idempotency_key=idempotency_key,
    )
    session.commit()

    def rows() -> Iterator[str]:
        completed = False
        cursor_score: int | None = None
        cursor_id: UUID | None = None
        try:
            yield _csv_row(_HEADERS)
            while True:
                _restore_tenant_context(session, context.tenant_id)
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
                    .limit(1)
                )
                if cursor_score is not None and cursor_id is not None:
                    statement = statement.where(
                        or_(
                            JobCandidate.score < cursor_score,
                            and_(
                                JobCandidate.score == cursor_score,
                                JobCandidate.id > cursor_id,
                            ),
                        )
                    )
                result = session.execute(statement).one_or_none()
                if result is None:
                    service.record_export_outcome(
                        context, job_id, export_record, "completed"
                    )
                    session.commit()
                    completed = True
                    return
                row, candidate = result
                cursor_score = row.score
                cursor_id = row.id
                candidate_values = (
                    str(candidate.id),
                    candidate.full_name,
                    candidate.current_title or "",
                    candidate.current_company or "",
                    candidate.location or "",
                )
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
                        ContactPoint.kind,
                        ContactPoint.classification,
                        ContactPoint.id,
                    )
                )
                contact_service = ContactService(session, cipher)
                for contact in contacts:
                    field = (
                        "phone"
                        if contact.kind == "phone"
                        else f"{contact.classification}_email"
                    )
                    if field not in fields or fields[field]:
                        continue
                    _restore_tenant_context(session, context.tenant_id)
                    contact_replayed = service.exported_contact_recorded(
                        context,
                        row,
                        export_record,
                        contact.id,
                    )
                    try:
                        authorized = contact_service.authorize_reveal(
                            context,
                            contact.id,
                            record_use=not contact_replayed,
                        )
                    except LookupError:
                        continue
                    if not contact_replayed:
                        service.record_exported_contact(
                            context, row, export_record, contact.id
                        )
                        try:
                            session.commit()
                        except SQLAlchemyError:
                            session.rollback()
                            raise
                    fields[field] = contact_service.decrypt_authorized(authorized)
                _restore_tenant_context(session, context.tenant_id)
                service.record_exported_candidate(context, row, export_record)
                try:
                    session.commit()
                except SQLAlchemyError:
                    session.rollback()
                    raise
                csv_row = _csv_row(
                    (
                        *candidate_values,
                        fields["work_email"],
                        fields["personal_email"],
                        fields["phone"],
                    )
                )
                yield csv_row
        finally:
            if not completed:
                session.rollback()
                try:
                    _restore_tenant_context(session, context.tenant_id)
                    service.record_export_outcome(
                        context, job_id, export_record, "aborted"
                    )
                    session.commit()
                except SQLAlchemyError:
                    session.rollback()

    return rows()


def _restore_tenant_context(session: Session, tenant_id: UUID) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )


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
