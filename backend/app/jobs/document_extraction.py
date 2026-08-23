from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol
from zipfile import BadZipFile, ZipFile

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.core.errors import AppError

MAX_FILE_BYTES = 10_000_000
MAX_TEXT_LENGTH = 50_000
MAX_PDF_PAGES = 200
MAX_DOCX_ENTRIES = 2_000
MAX_DOCX_EXPANDED_BYTES = 50_000_000

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
PDF_SIGNATURE = b"%PDF-"
ZIP_SIGNATURE = b"PK\x03\x04"
OLE_SIGNATURE = bytes.fromhex("D0 CF 11 E0 A1 B1 1A E1")
REQUIRED_DOCX_MEMBERS = {"[Content_Types].xml", "word/document.xml"}


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
        normalized_media_type = media_type.casefold() if media_type is not None else None

        if (
            extension == ".docx"
            and normalized_media_type == DOCX_MEDIA_TYPE
            and data.startswith(OLE_SIGNATURE)
        ):
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


def _pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise DocumentExtractionError("job_description_file_unreadable")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentExtractionError("job_description_file_too_complex")
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except DocumentExtractionError:
        raise
    except (PdfReadError, OSError, ValueError, KeyError) as error:
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
        if any(
            entry.filename.casefold().endswith(".zip")
            or package.open(entry).read(4) == ZIP_SIGNATURE
            for entry in entries
            if not entry.is_dir()
        ):
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
