import asyncio
from contextlib import suppress
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.requests import ClientDisconnect

from app.identity.dependencies import get_request_context
from app.identity.schemas import RequestContext
from app.jobs.document_extraction import (
    DocumentExtractionError,
    ExtractedJobDescription,
)
from app.jobs.document_multipart import read_job_description_upload
from app.jobs.document_runner import JobDescriptionExtractionRunner
from app.jobs.schemas import JobDescriptionExtractionResponse

router = APIRouter(prefix="/api/v1/job-descriptions", tags=["jobs"])

EXTRACTION_TIMEOUT_SECONDS = 10
DISCONNECT_POLL_SECONDS = 0.05

_DOCUMENT_ERROR_STATUS = {
    "job_description_file_required": 400,
    "job_description_file_too_large": 413,
    "job_description_type_unsupported": 415,
    "job_description_file_unreadable": 422,
    "job_description_text_missing": 422,
    "job_description_text_too_long": 422,
    "job_description_file_too_complex": 422,
    "job_description_extraction_unavailable": 503,
}


def get_document_extraction_runner(
    request: Request,
) -> JobDescriptionExtractionRunner:
    return request.app.state.job_description_extraction_runner


def _raise_document_error(error: DocumentExtractionError) -> NoReturn:
    code = error.code
    status_code = _DOCUMENT_ERROR_STATUS.get(code)
    if status_code is None:
        code = "job_description_extraction_unavailable"
        status_code = _DOCUMENT_ERROR_STATUS[code]
    raise HTTPException(status_code=status_code, detail={"code": code}) from error


async def _wait_for_disconnect(request: Request) -> None:
    while True:
        try:
            message = await asyncio.wait_for(
                request.receive(),
                timeout=DISCONNECT_POLL_SECONDS,
            )
        except TimeoutError:
            continue
        if message["type"] == "http.disconnect":
            return


async def _run_extraction(
    request: Request,
    runner: JobDescriptionExtractionRunner,
    *,
    data: bytes,
    filename: str,
    media_type: str | None,
) -> ExtractedJobDescription:
    worker = asyncio.create_task(
        runner.run(
            data=data,
            filename=filename,
            media_type=media_type,
            timeout_seconds=EXTRACTION_TIMEOUT_SECONDS,
        )
    )
    disconnect = asyncio.create_task(_wait_for_disconnect(request))
    try:
        finished, _ = await asyncio.wait(
            (worker, disconnect),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disconnect in finished:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            raise ClientDisconnect()
        return await worker
    finally:
        disconnect.cancel()
        if not worker.done():
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        with suppress(asyncio.CancelledError):
            await disconnect


@router.post(
    "/extract",
    response_model=JobDescriptionExtractionResponse,
    responses={
        400: {"description": "Exactly one job description file is required."},
        401: {"description": "Authentication is required."},
        413: {"description": "The uploaded file exceeds 10,000,000 bytes."},
        415: {"description": "The uploaded document type is unsupported."},
        422: {"description": "The uploaded document could not be extracted safely."},
        503: {"description": "Document extraction is temporarily unavailable."},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
async def extract_job_description(
    request: Request,
    _context: Annotated[RequestContext, Depends(get_request_context)],
    runner: Annotated[
        JobDescriptionExtractionRunner,
        Depends(get_document_extraction_runner),
    ],
) -> JobDescriptionExtractionResponse:
    try:
        upload = await read_job_description_upload(request)
        result = await _run_extraction(
            request,
            runner,
            data=upload.data,
            filename=upload.filename,
            media_type=upload.media_type,
        )
    except TimeoutError as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "job_description_extraction_unavailable"},
        ) from error
    except DocumentExtractionError as error:
        _raise_document_error(error)
    except ClientDisconnect:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "job_description_extraction_unavailable"},
        ) from error

    return JobDescriptionExtractionResponse(
        text=result.text,
        source={"filename": result.filename, "media_type": result.media_type},
    )
