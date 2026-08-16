import logging
import re

_CAPABILITY_PATH = re.compile(r"(/webhooks/apollo/)[A-Za-z0-9._~-]+")
_CALLBACK_QUERY = re.compile(r"([?&]webhook_url=)[^&\s\"]+")
_INVITATION_CLAIM_PATH = re.compile(
    r"(/api/v1/membership-invitations/)[^/?\s\"]+(/claim)"
)
_QUERY_VALUE = re.compile(r"([?&][^?&=\s\"]+=)[^&\s\"]*")


class SensitiveDataLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) == 5
        ):
            values = list(record.args)
            values[2] = _sanitize(str(values[2]))
            record.args = tuple(values)
            return True
        message = record.getMessage()
        sanitized = _sanitize(message)
        if sanitized != message:
            record.msg = sanitized
            record.args = ()
        return True


def _sanitize(message: str) -> str:
    sanitized = _CAPABILITY_PATH.sub(r"\1[REDACTED]", message)
    sanitized = _CALLBACK_QUERY.sub(r"\1[REDACTED]", sanitized)
    sanitized = _INVITATION_CLAIM_PATH.sub(r"\1[REDACTED]\2", sanitized)
    return _QUERY_VALUE.sub(r"\1[REDACTED]", sanitized)


def install_sensitive_data_log_filters() -> None:
    for logger_name in (
        "httpx",
        "httpx2",
        "httpcore",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "uvicorn.access",
    ):
        logger = logging.getLogger(logger_name)
        if not any(
            isinstance(value, SensitiveDataLogFilter) for value in logger.filters
        ):
            logger.addFilter(SensitiveDataLogFilter())
