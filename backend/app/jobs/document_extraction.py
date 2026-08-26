import threading
from binascii import crc32
from copy import copy
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pypdf import filters as pdf_filters
from pypdf._page import PageObject
from pypdf.errors import LimitReachedError, PyPdfError
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    IndirectObject,
    PdfObject,
    StreamObject,
)

from app.core.errors import AppError

MAX_FILE_BYTES = 10_000_000
MAX_TEXT_LENGTH = 50_000
MAX_PDF_PAGES = 200
MAX_PDF_DECODED_BYTES = 50_000_000
MAX_DOCX_ENTRIES = 2_000
MAX_DOCX_EXPANDED_BYTES = 50_000_000
DOCX_VALIDATION_CHUNK_BYTES = 64 * 1024

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
PDF_SIGNATURE = b"%PDF-"
ZIP_SIGNATURE = b"PK\x03\x04"
ZIP_SIGNATURES = (
    ZIP_SIGNATURE,
    b"PK\x01\x02",
    b"PK\x05\x06",
    b"PK\x07\x08",
)
OLE_SIGNATURE = bytes.fromhex("D0 CF 11 E0 A1 B1 1A E1")
REQUIRED_DOCX_MEMBERS = {"[Content_Types].xml", "word/document.xml"}
SUPPORTED_DOCX_COMPRESSION = {ZIP_STORED, ZIP_DEFLATED}
_PDF_DECODE_LOCK = threading.Lock()


@dataclass(frozen=True)
class ExtractedJobDescription:
    text: str
    filename: str
    media_type: str


class DocumentExtractionError(AppError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class JobDescriptionExtractor(Protocol):
    def extract(
        self, *, data: bytes, filename: str, media_type: str | None
    ) -> ExtractedJobDescription:
        pass


class DefaultJobDescriptionExtractor:
    def extract(
        self, *, data: bytes, filename: str, media_type: str | None
    ) -> ExtractedJobDescription:
        if len(data) > MAX_FILE_BYTES:
            raise DocumentExtractionError("job_description_file_too_large")

        extension = Path(filename).suffix.casefold()
        normalized_media_type = (
            media_type.casefold() if media_type is not None else None
        )

        if extension == ".docx" and data.startswith(OLE_SIGNATURE):
            raise DocumentExtractionError("job_description_file_unreadable")

        if (
            extension == ".pdf"
            and normalized_media_type == PDF_MEDIA_TYPE
            and data.startswith(PDF_SIGNATURE)
        ):
            text = _pdf_text(data)
            canonical_media_type = PDF_MEDIA_TYPE
        elif (
            extension == ".docx"
            and normalized_media_type == DOCX_MEDIA_TYPE
            and data.startswith(ZIP_SIGNATURE)
        ):
            text = _docx_text(data)
            canonical_media_type = DOCX_MEDIA_TYPE
        else:
            raise DocumentExtractionError("job_description_type_unsupported")

        return ExtractedJobDescription(
            text=_normalize_text(text),
            filename=filename,
            media_type=canonical_media_type,
        )


def _bounded_pdf_stream_data(stream: StreamObject, remaining_bytes: int) -> bytes:
    with _PDF_DECODE_LOCK:
        previous_limit = pdf_filters.RUN_LENGTH_MAX_OUTPUT_LENGTH
        pdf_filters.RUN_LENGTH_MAX_OUTPUT_LENGTH = min(
            previous_limit,
            max(0, remaining_bytes),
        )
        try:
            return stream.get_data()
        except LimitReachedError as error:
            raise DocumentExtractionError("job_description_file_too_complex") from error
        finally:
            pdf_filters.RUN_LENGTH_MAX_OUTPUT_LENGTH = previous_limit


def _pdf_decoded_stream_bytes(page: PageObject, remaining_bytes: int) -> int:
    decoded_bytes = 0
    pending_resources: list[PdfObject | None] = [page.get("/Resources")]
    seen_streams: set[tuple[int, int] | int] = set()
    while pending_resources:
        resource_reference = pending_resources.pop()
        if resource_reference is None:
            continue
        resources = resource_reference.get_object()
        if not isinstance(resources, DictionaryObject):
            raise DocumentExtractionError("job_description_file_unreadable")

        xobject_reference = resources.get("/XObject")
        if xobject_reference is None:
            continue
        if not isinstance(xobject_reference, PdfObject):
            raise DocumentExtractionError("job_description_file_unreadable")
        xobjects = xobject_reference.get_object()
        if not isinstance(xobjects, DictionaryObject):
            raise DocumentExtractionError("job_description_file_unreadable")

        for stream_reference in xobjects.values():
            if not isinstance(stream_reference, PdfObject):
                raise DocumentExtractionError("job_description_file_unreadable")
            stream = stream_reference.get_object()
            if not isinstance(stream, StreamObject):
                raise DocumentExtractionError("job_description_file_unreadable")
            if stream.get("/Subtype") != "/Form":
                continue

            indirect_reference = (
                stream_reference
                if isinstance(stream_reference, IndirectObject)
                else stream.indirect_reference
            )
            stream_key: tuple[int, int] | int = (
                (indirect_reference.idnum, indirect_reference.generation)
                if isinstance(indirect_reference, IndirectObject)
                else id(stream)
            )
            if stream_key in seen_streams:
                continue
            seen_streams.add(stream_key)

            decoded_bytes += len(
                _bounded_pdf_stream_data(stream, remaining_bytes - decoded_bytes)
            )
            if decoded_bytes > MAX_PDF_DECODED_BYTES:
                raise DocumentExtractionError("job_description_file_too_complex")

            form_resources = stream.get("/Resources")
            if form_resources is not None and not isinstance(form_resources, PdfObject):
                raise DocumentExtractionError("job_description_file_unreadable")
            pending_resources.append(form_resources)
    return decoded_bytes


def _pdf_decoded_content_bytes(page: PageObject, remaining_bytes: int) -> int:
    contents_reference = page.get("/Contents")
    if contents_reference is None:
        return 0
    if not isinstance(contents_reference, PdfObject):
        raise DocumentExtractionError("job_description_file_unreadable")
    contents = contents_reference.get_object()
    if isinstance(contents, StreamObject):
        streams = (contents,)
    elif isinstance(contents, ArrayObject):
        streams = tuple(item.get_object() for item in contents)
        if not all(isinstance(stream, StreamObject) for stream in streams):
            raise DocumentExtractionError("job_description_file_unreadable")
    else:
        raise DocumentExtractionError("job_description_file_unreadable")

    decoded_bytes = 0
    for stream in streams:
        decoded_bytes += len(
            _bounded_pdf_stream_data(stream, remaining_bytes - decoded_bytes)
        )
        if decoded_bytes > remaining_bytes:
            raise DocumentExtractionError("job_description_file_too_complex")
    return decoded_bytes


def _pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise DocumentExtractionError("job_description_file_unreadable")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentExtractionError("job_description_file_too_complex")
        decoded_bytes = 0
        for page in reader.pages:
            decoded_bytes += _pdf_decoded_content_bytes(
                page,
                MAX_PDF_DECODED_BYTES - decoded_bytes,
            )
            decoded_bytes += _pdf_decoded_stream_bytes(
                page,
                MAX_PDF_DECODED_BYTES - decoded_bytes,
            )
            if decoded_bytes > MAX_PDF_DECODED_BYTES:
                raise DocumentExtractionError("job_description_file_too_complex")
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except DocumentExtractionError:
        raise
    except (PyPdfError, NotImplementedError, OSError, ValueError, KeyError) as error:
        raise DocumentExtractionError("job_description_file_unreadable") from error


def _validate_docx_package(data: bytes) -> None:
    with ZipFile(BytesIO(data)) as package:
        entries = package.infolist()
        if len(entries) > MAX_DOCX_ENTRIES:
            raise DocumentExtractionError("job_description_file_too_complex")
        if sum(entry.file_size for entry in entries) > MAX_DOCX_EXPANDED_BYTES:
            raise DocumentExtractionError("job_description_file_too_complex")
        names = {entry.filename for entry in entries}
        if not REQUIRED_DOCX_MEMBERS <= names:
            raise DocumentExtractionError("job_description_file_unreadable")
        if any(entry.flag_bits & 1 for entry in entries):
            raise DocumentExtractionError("job_description_file_unreadable")
        if any(
            entry.compress_type not in SUPPORTED_DOCX_COMPRESSION for entry in entries
        ):
            raise DocumentExtractionError("job_description_file_too_complex")

        actual_expanded_bytes = 0
        for entry in entries:
            if entry.is_dir():
                if entry.file_size or entry.compress_size:
                    raise DocumentExtractionError("job_description_file_unreadable")
                continue
            if entry.filename.casefold().endswith(".zip"):
                raise DocumentExtractionError("job_description_file_too_complex")

            probe = copy(entry)
            probe.file_size = MAX_DOCX_EXPANDED_BYTES + DOCX_VALIDATION_CHUNK_BYTES
            entry_size = 0
            entry_crc = 0
            signature = b""
            try:
                with package.open(probe) as contents:
                    while chunk := contents.read(DOCX_VALIDATION_CHUNK_BYTES):
                        if len(signature) < len(ZIP_SIGNATURE):
                            signature = (signature + chunk)[: len(ZIP_SIGNATURE)]
                        entry_size += len(chunk)
                        actual_expanded_bytes += len(chunk)
                        if actual_expanded_bytes > MAX_DOCX_EXPANDED_BYTES:
                            raise DocumentExtractionError(
                                "job_description_file_too_complex"
                            )
                        entry_crc = crc32(chunk, entry_crc)
            except DocumentExtractionError:
                raise
            except NotImplementedError as error:
                raise DocumentExtractionError(
                    "job_description_file_too_complex"
                ) from error
            except RuntimeError as error:
                raise DocumentExtractionError(
                    "job_description_file_unreadable"
                ) from error

            if entry_size != entry.file_size or entry_crc != entry.CRC:
                raise DocumentExtractionError("job_description_file_unreadable")
            if signature in ZIP_SIGNATURES:
                raise DocumentExtractionError("job_description_file_too_complex")


def _docx_text(data: bytes) -> str:
    try:
        _validate_docx_package(data)
        document = Document(BytesIO(data))
        blocks: list[str] = []
        for block in document.iter_inner_content():
            if isinstance(block, Paragraph):
                if text := block.text.strip():
                    blocks.append(text)
            elif isinstance(block, Table):
                for row in block.rows:
                    cells = [cell.text.strip() for cell in row.cells]
                    if any(cells):
                        blocks.append("\t".join(cells))
        return "\n\n".join(blocks)
    except DocumentExtractionError:
        raise
    except (
        BadZipFile,
        PackageNotFoundError,
        OSError,
        ValueError,
        KeyError,
        SyntaxError,
    ) as error:
        raise DocumentExtractionError("job_description_file_unreadable") from error


def _normalize_text(value: str) -> str:
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cleaned = "\n".join(
        "".join(
            character
            for character in line
            if character == "\t" or character.isprintable()
        ).rstrip()
        for line in lines
    ).strip()
    if not cleaned:
        raise DocumentExtractionError("job_description_text_missing")
    if len(cleaned) > MAX_TEXT_LENGTH:
        raise DocumentExtractionError("job_description_text_too_long")
    return cleaned
