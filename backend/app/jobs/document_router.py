import asyncio
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from app.identity.dependencies import get_request_context
from app.identity.schemas import RequestContext
from app.jobs.document_extraction import (
    MAX_FILE_BYTES,
    DefaultJobDescriptionExtractor,
    DocumentExtractionError,
    JobDescriptionExtractor,
)
from app.jobs.schemas import JobDescriptionExtractionResponse

router = APIRouter(prefix="/api/v1/job-descriptions", tags=["jobs"])

READ_CHUNK_BYTES = 64 * 1024
EXTRACTION_TIMEOUT_SECONDS = 10

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


def get_document_extractor(request: Request) -> JobDescriptionExtractor:
    extractor = getattr(request.app.state, "job_description_extractor", None)
    if extractor is None:
        return DefaultJobDescriptionExtractor()
    return extractor


def _raise_document_error(error: DocumentExtractionError) -> NoReturn:
    code = error.code
    status_code = _DOCUMENT_ERROR_STATUS.get(code)
    if status_code is None:
        code = "job_description_extraction_unavailable"
        status_code = _DOCUMENT_ERROR_STATUS[code]
    raise HTTPException(status_code=status_code, detail={"code": code}) from error


async def _read_at_most(upload: UploadFile, max_bytes: int) -> bytes:
    contents = bytearray()
    while True:
        remaining = max_bytes + 1 - len(contents)
        chunk = await upload.read(min(READ_CHUNK_BYTES, remaining))
        if not chunk:
            return bytes(contents)
        contents.extend(chunk)
        if len(contents) > max_bytes:
            raise DocumentExtractionError("job_description_file_too_large")


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
)
async def extract_job_description(
    _context: Annotated[RequestContext, Depends(get_request_context)],
    extractor: Annotated[JobDescriptionExtractor, Depends(get_document_extractor)],
    files: Annotated[list[UploadFile] | None, File(alias="file")] = None,
) -> JobDescriptionExtractionResponse:
    uploads = files or []
    if len(uploads) != 1:
        for upload in uploads:
            await upload.close()
        _raise_document_error(DocumentExtractionError("job_description_file_required"))

    upload = uploads[0]
    try:
        data = await _read_at_most(upload, MAX_FILE_BYTES)
        async with asyncio.timeout(EXTRACTION_TIMEOUT_SECONDS):
            result = await asyncio.to_thread(
                extractor.extract,
                data=data,
                filename=upload.filename or "",
                media_type=upload.content_type,
            )
    except TimeoutError as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "job_description_extraction_unavailable"},
        ) from error
    except DocumentExtractionError as error:
        _raise_document_error(error)
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail={"code": "job_description_extraction_unavailable"},
        ) from error
    finally:
        await upload.close()

    return JobDescriptionExtractionResponse(
        text=result.text,
        source={"filename": result.filename, "media_type": result.media_type},
    )
