from io import BytesIO
from struct import pack
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def readable_docx() -> bytes:
    output = BytesIO()
    document = Document()
    document.add_heading("Senior Product Designer", level=1)
    document.add_paragraph("Lead product design for the growth team.")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Experience"
    table.cell(0, 1).text = "10+ years"
    document.save(output)
    return output.getvalue()


def interleaved_docx() -> bytes:
    output = BytesIO()
    document = Document()
    document.add_paragraph("Before table")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "First cell"
    table.cell(0, 1).text = "Second cell"
    document.add_paragraph("After table")
    document.save(output)
    return output.getvalue()


def empty_docx() -> bytes:
    output = BytesIO()
    Document().save(output)
    return output.getvalue()


def docx_with_text(text: str) -> bytes:
    output = BytesIO()
    document = Document()
    document.add_paragraph(text)
    document.save(output)
    return output.getvalue()


def docx_with_external_relationship() -> bytes:
    output = BytesIO()
    document = Document()
    document.add_paragraph("Local job description text")
    document.part.relate_to(
        "https://invalid.example/job-description",
        RELATIONSHIP_TYPE.HYPERLINK,
        is_external=True,
    )
    document.save(output)
    return output.getvalue()


def docx_with_malformed_document_xml() -> bytes:
    source = ZipFile(BytesIO(readable_docx()))
    output = BytesIO()
    with source, ZipFile(output, "w") as target:
        for entry in source.infolist():
            contents = source.read(entry)
            if entry.filename == "word/document.xml":
                contents = b"<w:document"
            target.writestr(entry, contents)
    return output.getvalue()


def readable_pdf() -> bytes:
    return text_pdf("Senior Product Designer")


def text_pdf(text: str) -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(output)
    return output.getvalue()


def empty_pdf() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


def encrypted_pdf() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("secret")
    writer.write(output)
    return output.getvalue()


def pdf_with_pages(count: int) -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


def docx_package_with_entries(count: int) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", b"<Types />")
        package.writestr("word/document.xml", b"<document />")
        for index in range(count - 2):
            package.writestr(f"padding/{index}.txt", b"")
    return output.getvalue()


def docx_with_declared_expanded_bytes(size: int) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", b"<Types />")
        package.writestr("word/document.xml", b"<document />")
        package.writestr("padding.bin", b"x")

    data = bytearray(output.getvalue())
    marker = b"PK\x01\x02"
    offset = 0
    while (offset := data.find(marker, offset)) != -1:
        name_length = int.from_bytes(data[offset + 28 : offset + 30], "little")
        filename = bytes(data[offset + 46 : offset + 46 + name_length])
        if filename == b"padding.bin":
            data[offset + 24 : offset + 28] = pack("<I", size)
            return bytes(data)
        offset += 4
    raise AssertionError("padding.bin central-directory record not found")


def docx_with_nested_archive(*, extension: str = ".zip") -> bytes:
    output = BytesIO(readable_docx())
    output.seek(0, 2)
    with ZipFile(output, "a", ZIP_DEFLATED) as package:
        package.writestr(f"word/embeddings/nested{extension}", b"PK\x03\x04nested")
    return output.getvalue()
