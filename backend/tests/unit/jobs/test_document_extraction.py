import pytest

from app.jobs.document_extraction import (
    DefaultJobDescriptionExtractor,
    DocumentExtractionError,
    _normalize_text,
)
from tests.job_description_fixtures import (
    docx_package_with_entries,
    docx_with_declared_expanded_bytes,
    docx_with_encrypted_member,
    docx_with_external_relationship,
    docx_with_forged_small_expanded_size,
    docx_with_malformed_document_xml,
    docx_with_nested_archive,
    docx_with_text,
    docx_with_unsupported_compression,
    empty_docx,
    empty_pdf,
    encrypted_pdf,
    interleaved_docx,
    pdf_with_pages,
    readable_docx,
    readable_pdf,
)

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


@pytest.fixture
def extractor() -> DefaultJobDescriptionExtractor:
    return DefaultJobDescriptionExtractor()


def assert_extraction_error(
    extractor: DefaultJobDescriptionExtractor,
    *,
    data: bytes,
    filename: str,
    media_type: str | None,
    code: str,
) -> None:
    with pytest.raises(DocumentExtractionError) as caught:
        extractor.extract(data=data, filename=filename, media_type=media_type)

    assert caught.value.code == code
    assert str(caught.value) == code


def test_extracts_text_from_a_real_pdf_with_canonical_metadata(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    result = extractor.extract(
        data=readable_pdf(),
        filename="role.PDF",
        media_type="APPLICATION/PDF",
    )

    assert result.text == "Senior Product Designer"
    assert result.filename == "role.PDF"
    assert result.media_type == PDF_MEDIA_TYPE


def test_extracts_paragraphs_and_table_cells_from_a_real_docx(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    result = extractor.extract(
        data=readable_docx(),
        filename="role.docx",
        media_type=DOCX_MEDIA_TYPE,
    )

    assert result.text == (
        "Senior Product Designer\n\n"
        "Lead product design for the growth team.\n\n"
        "Experience\t10+ years"
    )
    assert result.filename == "role.docx"
    assert result.media_type == DOCX_MEDIA_TYPE


def test_preserves_docx_paragraph_and_table_document_order(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    result = extractor.extract(
        data=interleaved_docx(),
        filename="role.docx",
        media_type=DOCX_MEDIA_TYPE,
    )

    assert result.text == "Before table\n\nFirst cell\tSecond cell\n\nAfter table"


def test_does_not_dereference_external_docx_relationships(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    result = extractor.extract(
        data=docx_with_external_relationship(),
        filename="role.docx",
        media_type=DOCX_MEDIA_TYPE,
    )

    assert result.text == "Local job description text"


def test_rejects_file_over_ten_million_bytes_before_parsing(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    assert_extraction_error(
        extractor,
        data=b"%PDF-" + b"x" * 9_999_996,
        filename="role.pdf",
        media_type=PDF_MEDIA_TYPE,
        code="job_description_file_too_large",
    )


@pytest.mark.parametrize(
    ("data", "filename", "media_type"),
    [
        (readable_pdf(), "role.txt", PDF_MEDIA_TYPE),
        (readable_pdf(), "role.pdf", DOCX_MEDIA_TYPE),
        (readable_docx(), "role.pdf", PDF_MEDIA_TYPE),
        (readable_pdf(), "role.docx", DOCX_MEDIA_TYPE),
        (readable_docx(), "role.docm", DOCX_MEDIA_TYPE),
        (readable_pdf(), "role.pdf", None),
    ],
    ids=[
        "extension",
        "media-type",
        "pdf-signature",
        "docx-signature",
        "macro-enabled-extension",
        "missing-media-type",
    ],
)
def test_rejects_extension_media_type_or_signature_mismatch(
    extractor: DefaultJobDescriptionExtractor,
    data: bytes,
    filename: str,
    media_type: str | None,
) -> None:
    assert_extraction_error(
        extractor,
        data=data,
        filename=filename,
        media_type=media_type,
        code="job_description_type_unsupported",
    )


@pytest.mark.parametrize(
    ("data", "filename", "media_type"),
    [
        (b"%PDF-corrupt", "role.pdf", PDF_MEDIA_TYPE),
        (b"PK\x03\x04corrupt", "role.docx", DOCX_MEDIA_TYPE),
        (docx_with_malformed_document_xml(), "role.docx", DOCX_MEDIA_TYPE),
    ],
    ids=["pdf", "docx-zip", "docx-xml"],
)
def test_classifies_corrupt_parser_input_as_unreadable(
    extractor: DefaultJobDescriptionExtractor,
    data: bytes,
    filename: str,
    media_type: str,
) -> None:
    assert_extraction_error(
        extractor,
        data=data,
        filename=filename,
        media_type=media_type,
        code="job_description_file_unreadable",
    )


def test_classifies_encrypted_pdf_as_unreadable(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    assert_extraction_error(
        extractor,
        data=encrypted_pdf(),
        filename="role.pdf",
        media_type=PDF_MEDIA_TYPE,
        code="job_description_file_unreadable",
    )


def test_classifies_ole_docx_as_unreadable(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    assert_extraction_error(
        extractor,
        data=bytes.fromhex("D0 CF 11 E0 A1 B1 1A E1") + b"encrypted",
        filename="role.docx",
        media_type=DOCX_MEDIA_TYPE,
        code="job_description_file_unreadable",
    )


@pytest.mark.parametrize("media_type", [None, PDF_MEDIA_TYPE])
def test_classifies_ole_docx_as_unreadable_before_trusting_media_type(
    extractor: DefaultJobDescriptionExtractor,
    media_type: str | None,
) -> None:
    assert_extraction_error(
        extractor,
        data=bytes.fromhex("D0 CF 11 E0 A1 B1 1A E1") + b"encrypted",
        filename="role.docx",
        media_type=media_type,
        code="job_description_file_unreadable",
    )


def test_classifies_encrypted_docx_zip_member_as_unreadable(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    assert_extraction_error(
        extractor,
        data=docx_with_encrypted_member(),
        filename="role.docx",
        media_type=DOCX_MEDIA_TYPE,
        code="job_description_file_unreadable",
    )


def test_rejects_unsupported_docx_compression_as_too_complex(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    assert_extraction_error(
        extractor,
        data=docx_with_unsupported_compression(),
        filename="role.docx",
        media_type=DOCX_MEDIA_TYPE,
        code="job_description_file_too_complex",
    )


@pytest.mark.parametrize(
    ("data", "filename", "media_type"),
    [
        (empty_pdf(), "role.pdf", PDF_MEDIA_TYPE),
        (empty_docx(), "role.docx", DOCX_MEDIA_TYPE),
    ],
    ids=["pdf", "docx"],
)
def test_rejects_documents_without_readable_text(
    extractor: DefaultJobDescriptionExtractor,
    data: bytes,
    filename: str,
    media_type: str,
) -> None:
    assert_extraction_error(
        extractor,
        data=data,
        filename=filename,
        media_type=media_type,
        code="job_description_text_missing",
    )


def test_rejects_pdf_with_more_than_two_hundred_pages(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    assert_extraction_error(
        extractor,
        data=pdf_with_pages(201),
        filename="role.pdf",
        media_type=PDF_MEDIA_TYPE,
        code="job_description_file_too_complex",
    )


def test_rejects_docx_with_more_than_two_thousand_archive_entries(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    assert_extraction_error(
        extractor,
        data=docx_package_with_entries(2_001),
        filename="role.docx",
        media_type=DOCX_MEDIA_TYPE,
        code="job_description_file_too_complex",
    )


def test_rejects_docx_with_more_than_fifty_million_declared_expanded_bytes(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    assert_extraction_error(
        extractor,
        data=docx_with_declared_expanded_bytes(50_000_001),
        filename="role.docx",
        media_type=DOCX_MEDIA_TYPE,
        code="job_description_file_too_complex",
    )


def test_rejects_actual_docx_expansion_when_central_metadata_underreports(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    assert_extraction_error(
        extractor,
        data=docx_with_forged_small_expanded_size(50_000_001),
        filename="role.docx",
        media_type=DOCX_MEDIA_TYPE,
        code="job_description_file_too_complex",
    )


@pytest.mark.parametrize("extension", [".zip", ".bin"])
def test_rejects_nested_archive_by_name_or_signature(
    extractor: DefaultJobDescriptionExtractor,
    extension: str,
) -> None:
    assert_extraction_error(
        extractor,
        data=docx_with_nested_archive(extension=extension),
        filename="role.docx",
        media_type=DOCX_MEDIA_TYPE,
        code="job_description_file_too_complex",
    )


def test_normalizes_line_endings_trailing_space_and_nonprintable_characters() -> None:
    assert _normalize_text(" \r\nSenior\tDesigner  \x00\rLead\x07 team \n ") == (
        "Senior\tDesigner\nLead team"
    )


def test_accepts_exactly_fifty_thousand_normalized_characters(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    result = extractor.extract(
        data=docx_with_text("x" * 50_000),
        filename="role.docx",
        media_type=DOCX_MEDIA_TYPE,
    )

    assert len(result.text) == 50_000


def test_rejects_fifty_thousand_and_one_extracted_characters(
    extractor: DefaultJobDescriptionExtractor,
) -> None:
    assert_extraction_error(
        extractor,
        data=docx_with_text("x" * 50_001),
        filename="role.docx",
        media_type=DOCX_MEDIA_TYPE,
        code="job_description_text_too_long",
    )
