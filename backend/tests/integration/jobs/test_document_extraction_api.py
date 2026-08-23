import asyncio
import logging
import threading
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from io import BytesIO
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.core.config import Settings
from app.core.database import Base, get_db
from app.identity.models import Membership, Tenant, User
from app.identity.schemas import IdentityClaims, RequestContext, Role
from app.jobs.document_extraction import (
    DOCX_MEDIA_TYPE,
    DefaultJobDescriptionExtractor,
    DocumentExtractionError,
    ExtractedJobDescription,
    JobDescriptionExtractor,
)
from app.jobs.document_router import extract_job_description
from app.jobs.models import Job, ScorecardVersion
from app.main import create_app
from tests.job_description_fixtures import readable_docx, readable_pdf


class StaticVerifier:
    def __init__(self, claims: IdentityClaims) -> None:
        self.claims = claims

    def verify(self, token: str) -> IdentityClaims:
        return self.claims


class BlockingExtractor:
    def extract(
        self, *, data: bytes, filename: str, media_type: str | None
    ) -> ExtractedJobDescription:
        time.sleep(0.1)
        return ExtractedJobDescription(
            text="late text",
            filename=filename,
            media_type=media_type or "application/pdf",
        )


class RaisingExtractor:
    def extract(
        self, *, data: bytes, filename: str, media_type: str | None
    ) -> ExtractedJobDescription:
        raise RuntimeError(f"parser exposed {filename}: confidential extracted text")


class ThreadRecordingExtractor:
    def __init__(self) -> None:
        self.thread_id: int | None = None

    def extract(
        self, *, data: bytes, filename: str, media_type: str | None
    ) -> ExtractedJobDescription:
        self.thread_id = threading.get_ident()
        return ExtractedJobDescription(
            text="Senior Product Designer",
            filename=filename,
            media_type=media_type or "application/pdf",
        )


class TypedErrorExtractor:
    def __init__(self, code: str) -> None:
        self.code = code

    def extract(
        self, *, data: bytes, filename: str, media_type: str | None
    ) -> ExtractedJobDescription:
        raise DocumentExtractionError(self.code)


class LifecycleUpload(UploadFile):
    def __init__(self) -> None:
        super().__init__(
            file=BytesIO(readable_pdf()),
            filename="close-me.pdf",
            headers=Headers({"content-type": "application/pdf"}),
        )
        self.close_completed = False

    async def close(self) -> None:
        await asyncio.sleep(0)
        await super().close()
        self.close_completed = True


@contextmanager
def _document_api(
    monkeypatch: pytest.MonkeyPatch,
    extractor: JobDescriptionExtractor,
) -> Iterator[dict[str, Any]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    tenant = Tenant(id=uuid4(), slug=f"document-api-{uuid4().hex}")
    owner = User(
        id=uuid4(),
        oidc_subject="oidc|document-api-owner",
        email="document-api-owner@agency.test",
        display_name="Document API Owner",
    )
    with Session(engine) as session:
        session.add_all((tenant, owner))
        session.flush()
        session.add(Membership(tenant_id=tenant.id, user_id=owner.id, role=Role.OWNER))
        tenant_id = tenant.id
        session.commit()

    app = create_app(
        Settings.for_test(),
        job_description_extractor=extractor,
    )
    app.state.token_verifier = StaticVerifier(
        IdentityClaims(
            subject="oidc|document-api-owner",
            email="document-api-owner@agency.test",
            name="Document API Owner",
            email_verified=True,
        )
    )

    def database_session() -> Generator[Session, None, None]:
        with Session(engine) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def count_rows(model: type[Job] | type[ScorecardVersion]) -> int:
        with Session(engine) as session:
            return session.scalar(select(func.count()).select_from(model)) or 0

    app.dependency_overrides[get_db] = database_session
    monkeypatch.setattr(
        "app.identity.dependencies.apply_tenant_context",
        lambda session, tenant_id: None,
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as api:
            yield {
                "api": api,
                "headers": {
                    "Authorization": "Bearer signed-token",
                    "X-Tenant-ID": str(tenant_id),
                },
                "count_jobs": lambda: count_rows(Job),
                "count_scorecards": lambda: count_rows(ScorecardVersion),
            }
    finally:
        engine.dispose()


@pytest.fixture
def document_api(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    with _document_api(monkeypatch, DefaultJobDescriptionExtractor()) as api:
        yield api


def _post_files(document_api: dict[str, Any], parts: list[tuple[str, bytes, str]]):
    return document_api["api"].post(
        "/api/v1/job-descriptions/extract",
        headers=document_api["headers"],
        files=[("file", part) for part in parts],
    )


def test_pdf_extraction_returns_text_without_persisting_domain_rows(
    document_api: dict[str, Any],
) -> None:
    jobs_before = document_api["count_jobs"]()
    scorecards_before = document_api["count_scorecards"]()

    response = _post_files(
        document_api,
        [("role.pdf", readable_pdf(), "application/pdf")],
    )

    assert response.status_code == 200
    assert "Senior Product Designer" in response.json()["text"]
    assert response.json()["source"] == {
        "filename": "role.pdf",
        "media_type": "application/pdf",
    }
    assert document_api["count_jobs"]() == jobs_before
    assert document_api["count_scorecards"]() == scorecards_before


def test_docx_extraction_returns_text_and_canonical_source(
    document_api: dict[str, Any],
) -> None:
    response = _post_files(
        document_api,
        [("role.docx", readable_docx(), DOCX_MEDIA_TYPE)],
    )

    assert response.status_code == 200
    assert "Senior Product Designer" in response.json()["text"]
    assert response.json()["source"] == {
        "filename": "role.docx",
        "media_type": DOCX_MEDIA_TYPE,
    }


@pytest.mark.parametrize(
    ("parts", "status_code", "code"),
    [
        ([], 400, "job_description_file_required"),
        (
            [
                ("one.pdf", b"%PDF-1.4", "application/pdf"),
                ("two.pdf", b"%PDF-1.4", "application/pdf"),
            ],
            400,
            "job_description_file_required",
        ),
        (
            [("oversized.pdf", b"x" * 10_000_001, "application/pdf")],
            413,
            "job_description_file_too_large",
        ),
        (
            [("role.txt", b"plain text", "text/plain")],
            415,
            "job_description_type_unsupported",
        ),
        (
            [("corrupt.pdf", b"%PDF-1.4 corrupt", "application/pdf")],
            422,
            "job_description_file_unreadable",
        ),
    ],
)
def test_upload_errors_are_stable(
    document_api: dict[str, Any],
    parts: list[tuple[str, bytes, str]],
    status_code: int,
    code: str,
) -> None:
    response = _post_files(document_api, parts)

    assert response.status_code == status_code
    assert response.json() == {"detail": {"code": code}}


@pytest.mark.parametrize(
    "code",
    [
        "job_description_text_missing",
        "job_description_text_too_long",
        "job_description_file_too_complex",
    ],
)
def test_extractor_validation_errors_are_stable_422_responses(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    with _document_api(monkeypatch, TypedErrorExtractor(code)) as document_api:
        response = _post_files(
            document_api,
            [("role.pdf", readable_pdf(), "application/pdf")],
        )

    assert response.status_code == 422
    assert response.json() == {"detail": {"code": code}}


def test_extraction_requires_authentication(document_api: dict[str, Any]) -> None:
    response = document_api["api"].post(
        "/api/v1/job-descriptions/extract",
        files={"file": ("role.pdf", readable_pdf(), "application/pdf")},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": {"code": "invalid_token"}}


def test_openapi_documents_stable_extraction_responses() -> None:
    operation = create_app().openapi()["paths"]["/api/v1/job-descriptions/extract"][
        "post"
    ]

    assert set(operation["responses"]) == {
        "200",
        "400",
        "401",
        "413",
        "415",
        "422",
        "503",
    }


def test_oversized_upload_reads_no_more_than_limit_plus_one_byte(
    document_api: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read = StarletteUploadFile.read
    requested_sizes: list[int] = []
    returned_bytes = 0

    async def tracked_read(self: StarletteUploadFile, size: int = -1) -> bytes:
        nonlocal returned_bytes
        requested_sizes.append(size)
        chunk = await original_read(self, size)
        returned_bytes += len(chunk)
        return chunk

    monkeypatch.setattr(StarletteUploadFile, "read", tracked_read)

    response = _post_files(
        document_api,
        [("oversized.pdf", b"x" * 10_100_000, "application/pdf")],
    )

    assert response.status_code == 413
    assert returned_bytes == 10_000_001
    assert max(requested_sizes) == 64 * 1024


def test_extractor_runs_off_the_request_event_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extractor = ThreadRecordingExtractor()
    original_read = StarletteUploadFile.read
    read_thread_ids: set[int] = set()

    async def tracked_read(self: StarletteUploadFile, size: int = -1) -> bytes:
        read_thread_ids.add(threading.get_ident())
        return await original_read(self, size)

    monkeypatch.setattr(StarletteUploadFile, "read", tracked_read)
    with _document_api(monkeypatch, extractor) as document_api:
        response = _post_files(
            document_api,
            [("role.pdf", readable_pdf(), "application/pdf")],
        )

    assert response.status_code == 200
    assert len(read_thread_ids) == 1
    assert extractor.thread_id is not None
    assert extractor.thread_id not in read_thread_ids


def test_extractor_timeout_returns_stable_unavailable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.jobs.document_router.EXTRACTION_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )
    with _document_api(monkeypatch, BlockingExtractor()) as document_api:
        response = _post_files(
            document_api,
            [("slow.pdf", readable_pdf(), "application/pdf")],
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "job_description_extraction_unavailable"}
    }


def test_unexpected_extractor_error_is_redacted_from_response_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    filename = "confidential-customer-role.pdf"
    with (
        _document_api(monkeypatch, RaisingExtractor()) as document_api,
        caplog.at_level(logging.DEBUG),
    ):
        response = _post_files(
            document_api,
            [(filename, readable_pdf(), "application/pdf")],
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "job_description_extraction_unavailable"}
    }
    serialized = caplog.text
    assert filename not in serialized
    assert "confidential extracted text" not in serialized
    assert filename not in response.text
    assert "confidential extracted text" not in response.text


@pytest.mark.parametrize(
    ("extractor", "expected_status"),
    [
        (DefaultJobDescriptionExtractor(), 200),
        (RaisingExtractor(), 503),
    ],
)
def test_upload_close_is_awaited_on_success_and_extractor_failure(
    extractor: JobDescriptionExtractor,
    expected_status: int,
) -> None:
    upload = LifecycleUpload()
    context = RequestContext(tenant_id=uuid4(), user_id=uuid4(), role=Role.OWNER)

    async def invoke() -> int:
        try:
            await extract_job_description(
                _context=context,
                extractor=extractor,
                files=[upload],
            )
        except HTTPException as error:
            return error.status_code
        return 200

    assert asyncio.run(invoke()) == expected_status
    assert upload.close_completed is True
