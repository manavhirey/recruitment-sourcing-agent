from datetime import UTC, datetime
from typing import Annotated, NoReturn
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.candidates.models import Candidate, ContactPoint
from app.core.config import Settings
from app.core.database import get_db
from app.crm.exports import export_shortlist_csv
from app.crm.models import CandidateStage, JobCandidate
from app.crm.schemas import (
    AcceptanceResponse,
    ActivityPage,
    ActivityResponse,
    CandidateExperienceView,
    CandidateFilter,
    CandidateProvenanceView,
    ContactRevealResponse,
    JobCandidatePage,
    JobCandidateView,
    MandatoryGapView,
    MaskedContact,
    NoteCreate,
    NoteResponse,
    OwnerUpdate,
    StageUpdate,
    TagsResponse,
    TagsUpdate,
)
from app.crm.service import CrmError, CrmService
from app.identity.dependencies import (
    get_app_settings,
    get_idempotency_key,
    get_request_context,
)
from app.identity.schemas import RequestContext

router = APIRouter(tags=["crm"])


def _service(
    session: Session,
    settings: Settings,
    request: Request,
) -> CrmService:
    return CrmService(
        session,
        settings.suppression_hmac_key.get_secret_value().encode(),
        request.app.state.contact_cipher,
    )


def _raise_crm_error(error: CrmError) -> NoReturn:
    status_code = {
        "job_candidate_not_found": status.HTTP_404_NOT_FOUND,
        "candidate_not_found": status.HTTP_404_NOT_FOUND,
        "contact_point_not_found": status.HTTP_404_NOT_FOUND,
        "contact_expired": status.HTTP_410_GONE,
        "acceptance_not_ready": status.HTTP_409_CONFLICT,
        "idempotency_conflict": status.HTTP_409_CONFLICT,
    }.get(error.code, status.HTTP_400_BAD_REQUEST)
    raise HTTPException(status_code=status_code, detail={"code": error.code}) from error


def _view(
    service: CrmService,
    context: RequestContext,
    row: JobCandidate,
    candidate: Candidate,
    *,
    detail: bool,
) -> JobCandidateView:
    contact_rows = service.masked_contacts(context, candidate.id) if detail else []
    run_candidate_id = service.run_candidate_id(context, row) if detail else None
    enrichment = service.enrichment_eligibility(context, run_candidate_id)
    return JobCandidateView(
        id=row.id,
        job_id=row.job_id,
        candidate_id=row.candidate_id,
        run_candidate_id=run_candidate_id,
        full_name=candidate.full_name,
        current_title=candidate.current_title,
        current_company=candidate.current_company,
        location=candidate.location,
        classification=row.classification,
        score=row.score,
        score_json=service.safe_score_json(row) if detail else None,
        mandatory_gaps=[
            MandatoryGapView(
                key=gap.key,
                label=gap.label,
                state=gap.state,
                summary=gap.summary,
            )
            for gap in service.mandatory_gaps(context, row)
        ],
        scorecard_version_id=row.scorecard_version_id,
        scorecard_version=(
            service.scorecard_version_number(context, row) if detail else None
        ),
        scoring_version=row.scoring_version,
        stage=row.stage,
        owner_user_id=row.owner_user_id,
        rejection_reason_code=row.rejection_reason_code,
        rejection_note=row.rejection_note,
        tags=service.tags_for(context, row.id),
        has_contact=service.has_contact(context, candidate.id),
        enrichment_eligible=enrichment.eligible,
        estimated_enrichment_credits=enrichment.estimated_credits,
        contacts=(
            [
                MaskedContact(
                    id=contact.id,
                    kind=contact.kind,
                    classification=contact.classification,
                    verification_state=contact.verification_state,
                    masked_value=(
                        "••••@••••" if contact.kind == "email" else "••••••••"
                    ),
                    expires_at=_utc(contact.expires_at),
                )
                for contact in contact_rows
            ]
            if detail
            else None
        ),
        experiences=(
            [
                CandidateExperienceView(
                    title=experience.title,
                    company_name=experience.company_name,
                    start_date=experience.start_date,
                    end_date=experience.end_date,
                    provider=experience.provider,
                    source_timestamp=_utc(experience.source_timestamp),
                )
                for experience in service.candidate_experiences(
                    context, candidate.id
                )
            ]
            if detail
            else None
        ),
        provenance=(
            [
                CandidateProvenanceView(
                    field_name=item.field_name,
                    provider=item.provider,
                    source_timestamp=_utc(item.source_timestamp),
                )
                for item in service.candidate_provenance(context, candidate.id)
            ]
            if detail
            else None
        ),
        notes=(
            [
                NoteResponse.model_validate(note, from_attributes=True)
                for note in service.notes_for(context, row.id)
            ]
            if detail
            else None
        ),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@router.get(
    "/api/v1/jobs/{job_id}/candidates",
    response_model=JobCandidatePage,
)
def list_job_candidates(
    job_id: UUID,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    classification: str = "main",
    sort: str = "-score",
    score_min: Annotated[int | None, Query(ge=0, le=100)] = None,
    score_max: Annotated[int | None, Query(ge=0, le=100)] = None,
    stage: CandidateStage | None = None,
    owner: UUID | None = None,
    tags: str | None = None,
    location: str | None = None,
    industry: str | None = None,
    has_contact: bool | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> JobCandidatePage:
    service = _service(session, settings, request)
    filters = CandidateFilter(
        classification=classification,
        sort=sort,
        score_min=score_min,
        score_max=score_max,
        stage=stage,
        owner_user_id=owner,
        tags=tuple(tags.split(",")) if tags else (),
        location=location,
        industry=industry,
        has_contact=has_contact,
        query=q,
    )
    try:
        rows, next_cursor = service.list_job_candidates(
            context,
            job_id,
            classification=filters.classification,
            sort=filters.sort,
            score_min=filters.score_min,
            score_max=filters.score_max,
            stage=filters.stage,
            owner_user_id=filters.owner_user_id,
            tags=filters.tags,
            location=filters.location,
            industry=filters.industry,
            has_contact=filters.has_contact,
            query=filters.query,
            cursor=cursor,
            limit=limit,
        )
    except CrmError as error:
        _raise_crm_error(error)
    return JobCandidatePage(
        items=[
            _view(service, context, row, candidate, detail=False)
            for row, candidate in rows
        ],
        next_cursor=next_cursor,
    )


@router.get(
    "/api/v1/job-candidates/{job_candidate_id}",
    response_model=JobCandidateView,
)
def get_job_candidate(
    job_candidate_id: UUID,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> JobCandidateView:
    service = _service(session, settings, request)
    try:
        row = service.get_authorized(context, job_candidate_id)
        candidate = service.candidate(context, row.candidate_id)
    except CrmError as error:
        _raise_crm_error(error)
    return _view(service, context, row, candidate, detail=True)


@router.patch(
    "/api/v1/job-candidates/{job_candidate_id}/stage",
    response_model=JobCandidateView,
)
def update_stage(
    job_candidate_id: UUID,
    body: StageUpdate,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> JobCandidateView:
    service = _service(session, settings, request)
    try:
        row = service.transition(
            context,
            job_candidate_id,
            body.stage,
            reason_code=body.reason_code,
            note=body.note,
            idempotency_key=idempotency_key,
        )
        candidate = service.candidate(context, row.candidate_id)
    except CrmError as error:
        _raise_crm_error(error)
    return _view(service, context, row, candidate, detail=False)


@router.post(
    "/api/v1/job-candidates/{job_candidate_id}/notes",
    response_model=NoteResponse,
)
def add_note(
    job_candidate_id: UUID,
    body: NoteCreate,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> NoteResponse:
    service = _service(session, settings, request)
    try:
        note = service.add_note(
            context,
            job_candidate_id,
            body.body,
            idempotency_key=idempotency_key,
        )
    except CrmError as error:
        _raise_crm_error(error)
    return NoteResponse.model_validate(note, from_attributes=True)


@router.patch(
    "/api/v1/job-candidates/{job_candidate_id}/owner",
    response_model=JobCandidateView,
)
def update_owner(
    job_candidate_id: UUID,
    body: OwnerUpdate,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> JobCandidateView:
    service = _service(session, settings, request)
    try:
        row = service.assign(
            context,
            job_candidate_id,
            body.owner_user_id,
            idempotency_key=idempotency_key,
        )
        candidate = service.candidate(context, row.candidate_id)
    except CrmError as error:
        _raise_crm_error(error)
    return _view(service, context, row, candidate, detail=False)


@router.put(
    "/api/v1/job-candidates/{job_candidate_id}/tags",
    response_model=TagsResponse,
)
def update_tags(
    job_candidate_id: UUID,
    body: TagsUpdate,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> TagsResponse:
    service = _service(session, settings, request)
    try:
        names = service.set_tags(
            context,
            job_candidate_id,
            body.tags,
            idempotency_key=idempotency_key,
        )
    except CrmError as error:
        _raise_crm_error(error)
    return TagsResponse(job_candidate_id=job_candidate_id, tags=names)


@router.get(
    "/api/v1/job-candidates/{job_candidate_id}/activity",
    response_model=ActivityPage,
)
def list_activity(
    job_candidate_id: UUID,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ActivityPage:
    service = _service(session, settings, request)
    try:
        events, next_cursor = service.activity(
            context, job_candidate_id, cursor=cursor, limit=limit
        )
    except CrmError as error:
        _raise_crm_error(error)
    return ActivityPage(
        items=[
            ActivityResponse(
                id=event.id,
                action=event.action,
                created_at=_utc(event.created_at),
            )
            for event in events
        ],
        next_cursor=next_cursor,
    )


@router.get(
    "/api/v1/jobs/{job_id}/acceptance",
    response_model=AcceptanceResponse,
)
def acceptance(
    job_id: UUID,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> AcceptanceResponse:
    service = _service(session, settings, request)
    try:
        report = service.acceptance_report(context, job_id)
    except CrmError as error:
        _raise_crm_error(error)
    return AcceptanceResponse.model_validate(report, from_attributes=True)


@router.post(
    "/api/v1/contact-points/{contact_point_id}/reveal",
    response_model=ContactRevealResponse,
)
def reveal_contact(
    contact_point_id: UUID,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    idempotency_key: Annotated[str, Depends(get_idempotency_key)],
) -> ContactRevealResponse:
    service = _service(session, settings, request)
    try:
        value = service.reveal_contact(
            context,
            contact_point_id,
            idempotency_key=idempotency_key,
        )
    except CrmError as error:
        if error.code == "contact_expired":
            session.commit()
        _raise_crm_error(error)
    contact = session.get(ContactPoint, contact_point_id)
    if contact is not None:
        request.app.state.telemetry.emit(
            "contact_revealed",
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            candidate_id=contact.candidate_id,
            outcome="success",
        )
    return ContactRevealResponse(id=contact_point_id, value=value)


@router.get("/api/v1/jobs/{job_id}/export.csv")
def export_shortlist(
    job_id: UUID,
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> StreamingResponse:
    supplied_key = request.headers.get("Idempotency-Key")
    if supplied_key is not None and (
        not supplied_key.strip() or len(supplied_key) > 255
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "idempotency_key_invalid"},
        )
    idempotency_key = supplied_key or f"derived-export-{uuid4()}"
    try:
        content = export_shortlist_csv(
            session,
            request.app.state.contact_cipher,
            context,
            job_id,
            authorization_hmac_key=(
                settings.suppression_hmac_key.get_secret_value().encode()
            ),
            idempotency_key=idempotency_key,
        )
    except CrmError as error:
        _raise_crm_error(error)
    return StreamingResponse(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="shortlist.csv"'},
    )
