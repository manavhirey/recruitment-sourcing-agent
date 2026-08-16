from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.crm.schemas import (
    CandidateDirectoryItem,
    CandidateDirectoryPage,
    CandidateJobView,
)
from app.crm.service import CrmError, CrmService
from app.identity.dependencies import get_app_settings, get_request_context
from app.identity.schemas import RequestContext

router = APIRouter(prefix="/api/v1/candidates", tags=["candidates"])


def _raise_directory_error(error: CrmError) -> NoReturn:
    status_code = (
        status.HTTP_404_NOT_FOUND
        if error.code in {"candidate_not_found", "job_candidate_not_found"}
        else status.HTTP_400_BAD_REQUEST
    )
    raise HTTPException(status_code=status_code, detail={"code": error.code}) from error


def _service(session: Session, settings: Settings) -> CrmService:
    return CrmService(
        session,
        settings.suppression_hmac_key.get_secret_value().encode(),
    )


@router.get("", response_model=CandidateDirectoryPage)
def directory(
    request: Request,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    q: str | None = None,
    location: str | None = None,
    industry: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> CandidateDirectoryPage:
    del request
    service = _service(session, settings)
    try:
        candidates, next_cursor = service.directory(
            context,
            query=q,
            location=location,
            industry=industry,
            cursor=cursor,
            limit=limit,
        )
        items = [
            CandidateDirectoryItem(
                id=candidate.id,
                name=candidate.full_name,
                current_title=candidate.current_title,
                current_company=candidate.current_company,
                location=candidate.location,
                industry_codes=list(candidate.industry_codes),
                job_ids=[
                    row.job_id
                    for row, _ in service.candidate_jobs(context, candidate.id)
                ],
                updated_at=candidate.updated_at,
            )
            for candidate in candidates
        ]
    except CrmError as error:
        _raise_directory_error(error)
    return CandidateDirectoryPage(items=items, next_cursor=next_cursor)


@router.get("/{candidate_id}/jobs", response_model=list[CandidateJobView])
def candidate_jobs(
    candidate_id: UUID,
    context: Annotated[RequestContext, Depends(get_request_context)],
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> list[CandidateJobView]:
    service = _service(session, settings)
    try:
        rows = service.candidate_jobs(context, candidate_id)
    except CrmError as error:
        _raise_directory_error(error)
    return [
        CandidateJobView(
            job_candidate_id=row.id,
            job_id=job.id,
            job_title=job.title,
            classification=row.classification,
            score=row.score,
            stage=row.stage,
            updated_at=row.updated_at,
        )
        for row, job in rows
    ]
