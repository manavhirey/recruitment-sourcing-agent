import logging
import re

_CAPABILITY_PATH = re.compile(r"(/webhooks/apollo/)[A-Za-z0-9._~-]+")
_CALLBACK_QUERY = re.compile(r"([?&]webhook_url=)[^&\s\"]+")


class SensitiveDataLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        sanitized = _CAPABILITY_PATH.sub(r"\1[REDACTED]", message)
        sanitized = _CALLBACK_QUERY.sub(r"\1[REDACTED]", sanitized)
        if sanitized != message:
            record.msg = sanitized
            record.args = ()
        return True


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
