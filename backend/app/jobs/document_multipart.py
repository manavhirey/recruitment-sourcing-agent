from dataclasses import dataclass

from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import parse_options_header
from starlette.requests import Request

from app.jobs.document_extraction import (
    MAX_FILE_BYTES,
    DocumentExtractionError,
)

MAX_MULTIPART_BODY_BYTES = MAX_FILE_BYTES + 16_384
MAX_MULTIPART_HEADERS = 8
MAX_MULTIPART_HEADER_BYTES = 1_024


@dataclass(frozen=True)
class UploadedJobDescription:
    data: bytes
    filename: str
    media_type: str | None


def _decode_header(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value.decode("latin-1")


class _FilePartCollector:
    def __init__(self) -> None:
        self.data = bytearray()
        self.filename: str | None = None
        self.media_type: str | None = None
        self.part_count = 0
        self.part_finished = False
        self.message_finished = False
        self._headers_finished = False
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._content_disposition: bytes | None = None
        self._content_type: bytes | None = None

    def on_part_begin(self) -> None:
        self.part_count += 1
        if self.part_count > 1:
            raise DocumentExtractionError("job_description_file_required")

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field.extend(data[start:end])

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value.extend(data[start:end])

    def on_header_end(self) -> None:
        field = bytes(self._header_field).lower()
        value = bytes(self._header_value)
        if field == b"content-disposition":
            if self._content_disposition is not None:
                raise DocumentExtractionError("job_description_file_required")
            self._content_disposition = value
        elif field == b"content-type":
            if self._content_type is not None:
                raise DocumentExtractionError("job_description_file_required")
            self._content_type = value
        self._header_field.clear()
        self._header_value.clear()

    def on_headers_finished(self) -> None:
        if self._content_disposition is None:
            raise DocumentExtractionError("job_description_file_required")
        disposition, options = parse_options_header(self._content_disposition)
        if (
            disposition.lower() != b"form-data"
            or options.get(b"name") != b"file"
            or b"filename" not in options
        ):
            raise DocumentExtractionError("job_description_file_required")
        self.filename = _decode_header(options[b"filename"])
        self.media_type = (
            _decode_header(self._content_type) if self._content_type else None
        )
        self._headers_finished = True

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if not self._headers_finished:
            raise DocumentExtractionError("job_description_file_required")
        chunk = data[start:end]
        if len(self.data) + len(chunk) > MAX_FILE_BYTES:
            raise DocumentExtractionError("job_description_file_too_large")
        self.data.extend(chunk)

    def on_part_end(self) -> None:
        self.part_finished = True

    def on_end(self) -> None:
        self.message_finished = True

    def result(self) -> UploadedJobDescription:
        if (
            self.part_count != 1
            or self.filename is None
            or not self.part_finished
            or not self.message_finished
        ):
            raise DocumentExtractionError("job_description_file_required")
        return UploadedJobDescription(
            data=bytes(self.data),
            filename=self.filename,
            media_type=self.media_type,
        )


async def read_job_description_upload(request: Request) -> UploadedJobDescription:
    content_type = request.headers.get("content-type")
    try:
        media_type, options = parse_options_header(content_type)
        boundary = options[b"boundary"]
    except (KeyError, TypeError, ValueError) as error:
        raise DocumentExtractionError("job_description_file_required") from error
    if media_type.lower() != b"multipart/form-data":
        raise DocumentExtractionError("job_description_file_required")

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_MULTIPART_BODY_BYTES:
                raise DocumentExtractionError("job_description_file_too_large")
        except ValueError:
            pass

    collector = _FilePartCollector()
    try:
        parser = MultipartParser(
            boundary,
            {
                "on_part_begin": collector.on_part_begin,
                "on_header_field": collector.on_header_field,
                "on_header_value": collector.on_header_value,
                "on_header_end": collector.on_header_end,
                "on_headers_finished": collector.on_headers_finished,
                "on_part_data": collector.on_part_data,
                "on_part_end": collector.on_part_end,
                "on_end": collector.on_end,
            },
            max_header_count=MAX_MULTIPART_HEADERS,
            max_header_size=MAX_MULTIPART_HEADER_BYTES,
        )
        total_bytes = 0
        async for chunk in request.stream():
            total_bytes += len(chunk)
            if total_bytes > MAX_MULTIPART_BODY_BYTES:
                raise DocumentExtractionError("job_description_file_too_large")
            parser.write(chunk)
        parser.finalize()
        return collector.result()
    except DocumentExtractionError:
        raise
    except FormParserError as error:
        raise DocumentExtractionError("job_description_file_required") from error
