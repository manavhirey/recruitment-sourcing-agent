import asyncio
import logging
import multiprocessing
import os
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base, get_db
from app.identity.models import Membership, Tenant, User
from app.identity.schemas import IdentityClaims, Role
from app.jobs.document_extraction import (
    DOCX_MEDIA_TYPE,
    DefaultJobDescriptionExtractor,
    DocumentExtractionError,
    ExtractedJobDescription,
    JobDescriptionExtractor,
)
from app.jobs.document_runner import (
    JobDescriptionExtractionRunner,
    ProcessJobDescriptionExtractionRunner,
)
from app.jobs.models import Job, ScorecardVersion
from app.main import create_app
from tests.job_description_fixtures import readable_docx, readable_pdf


class StaticVerifier:
    def __init__(self, claims: IdentityClaims) -> None:
        self.claims = claims

    def verify(self, token: str) -> IdentityClaims:
        return self.claims


class InlineExtractionRunner:
    def __init__(self, extractor: JobDescriptionExtractor) -> None:
        self.extractor = extractor

    async def run(
        self,
        *,
        data: bytes,
        filename: str,
        media_type: str | None,
        timeout_seconds: float,
    ) -> ExtractedJobDescription:
        return self.extractor.extract(
            data=data,
            filename=filename,
            media_type=media_type,
        )


class TimeoutExtractionRunner:
    async def run(
        self,
        *,
        data: bytes,
        filename: str,
        media_type: str | None,
        timeout_seconds: float,
    ) -> ExtractedJobDescription:
        raise TimeoutError("controlled runner timeout")


class DisconnectingExtractionRunner:
    def __init__(self) -> None:
        self.cancelled = False

    async def run(
        self,
        *,
        data: bytes,
        filename: str,
        media_type: str | None,
        timeout_seconds: float,
    ) -> ExtractedJobDescription:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def blocking_process_worker(
    _connection: Connection,
    _data: bytes,
    _filename: str,
    _media_type: str | None,
) -> None:
    Path(os.environ["DOCUMENT_TEST_WORKER_MARKER"]).write_text(
        str(os.getpid()),
        encoding="utf-8",
    )
    while True:
        time.sleep(1)


class RaisingExtractor:
    def extract(
        self, *, data: bytes, filename: str, media_type: str | None
    ) -> ExtractedJobDescription:
        raise RuntimeError(f"parser exposed {filename}: confidential extracted text")


class SuccessfulExtractor:
    def extract(
        self, *, data: bytes, filename: str, media_type: str | None
    ) -> ExtractedJobDescription:
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


@contextmanager
def _document_api(
    monkeypatch: pytest.MonkeyPatch,
    extractor: JobDescriptionExtractor | None = None,
    *,
    runner: JobDescriptionExtractionRunner | None = None,
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
    other_tenant = Tenant(id=uuid4(), slug=f"document-api-other-{uuid4().hex}")
    with Session(engine) as session:
        session.add_all((tenant, other_tenant, owner))
        session.flush()
        session.add(Membership(tenant_id=tenant.id, user_id=owner.id, role=Role.OWNER))
        tenant_id = tenant.id
        other_tenant_id = other_tenant.id
        session.commit()

    selected_runner = runner or InlineExtractionRunner(
        extractor or DefaultJobDescriptionExtractor()
    )
    app = create_app(
        Settings.for_test(),
        job_description_extraction_runner=selected_runner,
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
                "app": app,
                "api": api,
                "headers": {
                    "Authorization": "Bearer signed-token",
                    "X-Tenant-ID": str(tenant_id),
                },
                "other_tenant_id": other_tenant_id,
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


def _multipart_part(
    boundary: str,
    *,
    name: str,
    contents: bytes,
    filename: str | None = None,
    media_type: str | None = None,
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> bytes:
    disposition = f'Content-Disposition: form-data; name="{name}"'
    if filename is not None:
        disposition += f'; filename="{filename}"'
    headers = [disposition]
    if media_type is not None:
        headers.append(f"Content-Type: {media_type}")
    headers.extend(f"{key}: {value}" for key, value in extra_headers)
    header_block = "\r\n".join(headers)
    return f"--{boundary}\r\n{header_block}\r\n\r\n".encode() + contents + b"\r\n"


def _multipart_part_with_header_bytes(boundary: str, header_bytes: int) -> bytes:
    headers = [
        'Content-Disposition: form-data; name="file"; filename="role.pdf"',
        "Content-Type: application/pdf",
        "X-Padding: ",
    ]
    fixed_bytes = len("\r\n".join(headers).encode())
    if header_bytes < fixed_bytes:
        raise ValueError("header_bytes is too small")
    return _multipart_part(
        boundary,
        name="file",
        filename="role.pdf",
        media_type="application/pdf",
        contents=b"%PDF-1.4",
        extra_headers=(("X-Padding", "x" * (header_bytes - fixed_bytes)),),
    )


def _streaming_post(
    document_api: dict[str, Any],
    chunks: list[tuple[str, bytes]],
    *,
    headers: dict[str, str],
) -> tuple[Any, list[str]]:
    consumed: list[str] = []

    async def invoke() -> Response:
        chunk_index = 0
        response_complete = asyncio.Event()
        response_status = 500
        response_headers: list[tuple[bytes, bytes]] = []
        response_body = bytearray()

        async def receive() -> dict[str, object]:
            nonlocal chunk_index
            if chunk_index < len(chunks):
                label, chunk = chunks[chunk_index]
                chunk_index += 1
                consumed.append(label)
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": chunk_index < len(chunks),
                }
            await response_complete.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            nonlocal response_status, response_headers
            if message["type"] == "http.response.start":
                response_status = message["status"]
                response_headers = message.get("headers", [])
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    response_complete.set()

        raw_headers = [
            (key.lower().encode(), value.encode()) for key, value in headers.items()
        ]
        await document_api["app"](
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/api/v1/job-descriptions/extract",
                "raw_path": b"/api/v1/job-descriptions/extract",
                "query_string": b"",
                "root_path": "",
                "headers": raw_headers,
                "client": ("127.0.0.1", 50000),
                "server": ("testserver", 443),
                "state": {},
            },
            receive,
            send,
        )
        return Response(
            response_status,
            headers=response_headers,
            content=bytes(response_body),
        )

    return asyncio.run(invoke()), consumed


def _disconnecting_post(
    document_api: dict[str, Any],
    body: bytes,
    *,
    headers: dict[str, str],
) -> Response:
    async def invoke() -> Response:
        messages: Iterator[dict[str, object]] = iter(
            (
                {"type": "http.request", "body": body, "more_body": False},
                {"type": "http.disconnect"},
            )
        )
        response_status = 500
        response_headers: list[tuple[bytes, bytes]] = []
        response_body = bytearray()

        async def receive() -> dict[str, object]:
            return next(messages)

        async def send(message: dict[str, Any]) -> None:
            nonlocal response_status, response_headers
            if message["type"] == "http.response.start":
                response_status = message["status"]
                response_headers = message.get("headers", [])
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))

        raw_headers = [
            (key.lower().encode(), value.encode()) for key, value in headers.items()
        ]
        await document_api["app"](
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/api/v1/job-descriptions/extract",
                "raw_path": b"/api/v1/job-descriptions/extract",
                "query_string": b"",
                "root_path": "",
                "headers": raw_headers,
                "client": ("127.0.0.1", 50000),
                "server": ("testserver", 443),
                "state": {},
            },
            receive,
            send,
        )
        return Response(
            response_status,
            headers=response_headers,
            content=bytes(response_body),
        )

    return asyncio.run(invoke())


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


def test_unauthenticated_raw_stream_consumes_zero_body_chunks(
    document_api: dict[str, Any],
) -> None:
    boundary = "unauthenticated-boundary"
    response, consumed = _streaming_post(
        document_api,
        [
            (
                "file",
                _multipart_part(
                    boundary,
                    name="file",
                    filename="role.pdf",
                    media_type="application/pdf",
                    contents=readable_pdf(),
                ),
            ),
            ("closing", f"--{boundary}--\r\n".encode()),
        ],
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    assert response.status_code == 401
    assert consumed == []


def test_extraction_denies_an_authenticated_principal_outside_the_tenant(
    document_api: dict[str, Any],
) -> None:
    response = document_api["api"].post(
        "/api/v1/job-descriptions/extract",
        headers={
            **document_api["headers"],
            "X-Tenant-ID": str(document_api["other_tenant_id"]),
        },
        files={"file": ("role.pdf", readable_pdf(), "application/pdf")},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": {"code": "tenant_not_found"}}


def test_non_member_raw_stream_consumes_zero_body_chunks(
    document_api: dict[str, Any],
) -> None:
    boundary = "non-member-boundary"
    response, consumed = _streaming_post(
        document_api,
        [
            (
                "file",
                _multipart_part(
                    boundary,
                    name="file",
                    filename="role.pdf",
                    media_type="application/pdf",
                    contents=readable_pdf(),
                ),
            ),
            ("closing", f"--{boundary}--\r\n".encode()),
        ],
        headers={
            "Authorization": "Bearer signed-token",
            "X-Tenant-ID": str(document_api["other_tenant_id"]),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )

    assert response.status_code == 404
    assert consumed == []


def test_chunked_oversized_raw_stream_stops_at_the_first_excess_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _document_api(monkeypatch, SuccessfulExtractor()) as document_api:
        boundary = "oversized-stream-boundary"
        opening = _multipart_part(
            boundary,
            name="file",
            filename="role.pdf",
            media_type="application/pdf",
            contents=b"",
        )[:-2]
        response, consumed = _streaming_post(
            document_api,
            [
                ("opening", opening),
                ("limit", b"x" * 10_000_000),
                ("overflow", b"x"),
                ("unread-file-tail", b"private tail"),
                ("unread-closing", f"\r\n--{boundary}--\r\n".encode()),
            ],
            headers={
                **document_api["headers"],
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

    assert response.status_code == 413
    assert response.json() == {"detail": {"code": "job_description_file_too_large"}}
    assert consumed == ["opening", "limit", "overflow"]


def test_raw_stream_accepts_the_exact_ten_million_byte_file_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _document_api(monkeypatch, SuccessfulExtractor()) as document_api:
        boundary = "exact-file-boundary"
        opening = _multipart_part(
            boundary,
            name="file",
            filename="role.pdf",
            media_type="application/pdf",
            contents=b"",
        )[:-2]
        response, consumed = _streaming_post(
            document_api,
            [
                ("opening", opening),
                ("file", b"x" * 10_000_000),
                ("closing", f"\r\n--{boundary}--\r\n".encode()),
            ],
            headers={
                **document_api["headers"],
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

    assert response.status_code == 200
    assert consumed == ["opening", "file", "closing"]


@pytest.mark.parametrize("unexpected_position", ["before", "after"])
def test_raw_multipart_rejects_an_extra_non_file_part(
    monkeypatch: pytest.MonkeyPatch,
    unexpected_position: str,
) -> None:
    with _document_api(monkeypatch, SuccessfulExtractor()) as document_api:
        boundary = "extra-field-boundary"
        file_part = _multipart_part(
            boundary,
            name="file",
            filename="role.pdf",
            media_type="application/pdf",
            contents=b"%PDF-1.4",
        )
        field_part = _multipart_part(
            boundary,
            name="job_id",
            contents=b"private-job",
        )
        ordered = (
            field_part + file_part
            if unexpected_position == "before"
            else file_part + field_part
        )
        response, _ = _streaming_post(
            document_api,
            [("body", ordered + f"--{boundary}--\r\n".encode())],
            headers={
                **document_api["headers"],
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "job_description_file_required"}}


def test_raw_multipart_rejects_oversized_part_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _document_api(monkeypatch, SuccessfulExtractor()) as document_api:
        boundary = "header-limit-boundary"
        response, _ = _streaming_post(
            document_api,
            [
                (
                    "body",
                    _multipart_part(
                        boundary,
                        name="file",
                        filename="role.pdf",
                        media_type="application/pdf",
                        contents=b"%PDF-1.4",
                        extra_headers=(("X-Oversized", "x" * 8_193),),
                    )
                    + f"--{boundary}--\r\n".encode(),
                )
            ],
            headers={
                **document_api["headers"],
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "job_description_file_required"}}


@pytest.mark.parametrize(
    ("header_bytes", "expected_status"),
    [(8_192, 200), (8_193, 400)],
)
def test_raw_multipart_enforces_the_eight_kibibyte_part_header_boundary(
    monkeypatch: pytest.MonkeyPatch,
    header_bytes: int,
    expected_status: int,
) -> None:
    with _document_api(monkeypatch, SuccessfulExtractor()) as document_api:
        boundary = "exact-header-limit-boundary"
        response, _ = _streaming_post(
            document_api,
            [
                (
                    "body",
                    _multipart_part_with_header_bytes(boundary, header_bytes)
                    + f"--{boundary}--\r\n".encode(),
                )
            ],
            headers={
                **document_api["headers"],
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

    assert response.status_code == expected_status


def test_raw_multipart_rejects_too_many_part_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _document_api(monkeypatch, SuccessfulExtractor()) as document_api:
        boundary = "header-count-boundary"
        response, _ = _streaming_post(
            document_api,
            [
                (
                    "body",
                    _multipart_part(
                        boundary,
                        name="file",
                        filename="role.pdf",
                        media_type="application/pdf",
                        contents=b"%PDF-1.4",
                        extra_headers=tuple(
                            (f"X-Header-{index}", "value") for index in range(7)
                        ),
                    )
                    + f"--{boundary}--\r\n".encode(),
                )
            ],
            headers={
                **document_api["headers"],
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "job_description_file_required"}}


def test_raw_multipart_requires_a_complete_closing_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _document_api(monkeypatch, SuccessfulExtractor()) as document_api:
        boundary = "malformed-boundary"
        response, _ = _streaming_post(
            document_api,
            [
                (
                    "body",
                    _multipart_part(
                        boundary,
                        name="file",
                        filename="role.pdf",
                        media_type="application/pdf",
                        contents=b"%PDF-1.4",
                    ),
                )
            ],
            headers={
                **document_api["headers"],
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "job_description_file_required"}}


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
        "499",
        "503",
    }
    assert operation["requestBody"] == {
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


def test_extractor_timeout_returns_stable_unavailable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _document_api(
        monkeypatch,
        runner=TimeoutExtractionRunner(),
    ) as document_api:
        response = _post_files(
            document_api,
            [("slow.pdf", readable_pdf(), "application/pdf")],
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "job_description_extraction_unavailable"}
    }


def test_client_disconnect_during_extraction_is_an_intentional_empty_499(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = DisconnectingExtractionRunner()
    filename = "confidential-disconnected-role.pdf"
    boundary = "disconnect-boundary"
    body = (
        _multipart_part(
            boundary,
            name="file",
            filename=filename,
            media_type="application/pdf",
            contents=readable_pdf(),
        )
        + f"--{boundary}--\r\n".encode()
    )
    with (
        _document_api(monkeypatch, runner=runner) as document_api,
        caplog.at_level(logging.DEBUG),
    ):
        response = _disconnecting_post(
            document_api,
            body,
            headers={
                **document_api["headers"],
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

    assert response.status_code == 499
    assert response.content == b""
    assert runner.cancelled is True
    assert filename not in caplog.text
    assert filename not in response.text


def test_timeout_does_not_return_while_real_parser_work_is_still_alive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "app.jobs.document_router.EXTRACTION_TIMEOUT_SECONDS",
        3.0,
        raising=False,
    )
    marker = tmp_path / "blocking-worker.pid"
    monkeypatch.setenv("DOCUMENT_TEST_WORKER_MARKER", str(marker))
    runner = ProcessJobDescriptionExtractionRunner(blocking_process_worker)
    with _document_api(monkeypatch, runner=runner) as document_api:
        response = _post_files(
            document_api,
            [("slow.pdf", b"blocking worker input", "application/pdf")],
        )

    assert response.status_code == 503
    worker_pid = int(marker.read_text(encoding="utf-8"))
    assert worker_pid not in {child.pid for child in multiprocessing.active_children()}
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)


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
