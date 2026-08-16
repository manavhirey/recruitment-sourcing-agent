import logging

import pytest

from app.core.log_redaction import SensitiveDataLogFilter


@pytest.mark.parametrize(
    "message",
    [
        'POST /api/v1/membership-invitations/%s/claim HTTP/1.1',
        'HTTP Request: POST https://api.example.test/api/v1/membership-invitations/%s/claim "HTTP/1.1 200 OK"',
    ],
)
def test_invitation_claim_token_is_redacted_from_request_logs(message: str) -> None:
    token = "01915a31-5ee8-7f20-b45b-e8f2db212345.secret-invitation-token"
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(token,),
        exc_info=None,
    )

    assert SensitiveDataLogFilter().filter(record) is True
    rendered = record.getMessage()

    assert token not in rendered
    assert "/api/v1/membership-invitations/[REDACTED]/claim" in rendered
