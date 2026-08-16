import json
import logging
from collections.abc import Iterator

import httpx
import pytest
import respx

from app.core.config import Settings
from app.providers.apollo import (
    APOLLO_BULK_ENRICHMENT_URL,
    APOLLO_WEBHOOK_RESULT_URL,
    ApolloGateway,
    normalize_enrichment_payload,
)
from app.providers.base import (
    EnrichmentInput,
    EnrichmentPending,
    ProviderPayloadError,
)


@pytest.fixture
def gateway() -> Iterator[ApolloGateway]:
    value = ApolloGateway(Settings.for_test())
    yield value
    value.close()


def _people(count: int) -> tuple[EnrichmentInput, ...]:
    return tuple(
        EnrichmentInput(f"person-{index}", f"https://linkedin.test/in/{index}")
        for index in range(count)
    )


def test_apollo_bulk_enrichment_limits_batch_to_ten_without_calling_provider(
    respx_mock: respx.MockRouter, gateway: ApolloGateway
) -> None:
    route = respx_mock.post(APOLLO_BULK_ENRICHMENT_URL).mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(ValueError, match="at most 10"):
        gateway.enrich_batch(_people(11), "https://callbacks.test/token")

    assert route.call_count == 0


def test_apollo_bulk_enrichment_sends_headers_flags_details_and_https_callback(
    respx_mock: respx.MockRouter, gateway: ApolloGateway
) -> None:
    route = respx_mock.post(APOLLO_BULK_ENRICHMENT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "request_id": 1039995589705121900,
                "credits_consumed": 1.25,
                "matches": [
                    {
                        "id": "person-0",
                        "name": "Priya Sharma",
                        "email": "priya@work.example",
                        "email_status": "verified",
                    }
                ],
            },
        )
    )

    receipt = gateway.enrich_batch(
        _people(1),
        "https://callbacks.test/webhooks/apollo/opaque",
        reveal_personal_emails=True,
        reveal_phone_number=True,
    )

    request = route.calls[0].request
    assert request.headers["x-api-key"] == "test-apollo-key"
    assert dict(request.url.params) == {
        "reveal_personal_emails": "true",
        "reveal_phone_number": "true",
        "webhook_url": "https://callbacks.test/webhooks/apollo/opaque",
    }
    assert json.loads(request.content) == {
        "details": [
            {
                "id": "person-0",
                "linkedin_url": "https://linkedin.test/in/0",
            }
        ]
    }
    assert receipt.request_id == "1039995589705121900"
    assert receipt.submitted_count == 1
    assert receipt.result is not None
    assert receipt.result.people[0].contacts[0].value == "priya@work.example"
    assert dict(receipt.charged_units)["estimated_credits"] == 2


def test_apollo_applies_configured_shorter_retention_to_normalized_contacts(
    respx_mock: respx.MockRouter,
) -> None:
    settings = Settings.for_test().model_copy(
        update={"apollo_contact_retention_days": 45}
    )
    gateway = ApolloGateway(settings)
    respx_mock.post(APOLLO_BULK_ENRICHMENT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "request_id": 123,
                "matches": [
                    {
                        "id": "person-0",
                        "email": "configured@example.test",
                        "email_status": "verified",
                    }
                ],
            },
        )
    )

    try:
        receipt = gateway.enrich_batch(
            _people(1), "https://callbacks.test/webhooks/apollo/opaque"
        )
    finally:
        gateway.close()

    assert receipt.result is not None
    assert receipt.result.people[0].contacts[0].retention_days == 45


def test_apollo_bulk_enrichment_rejects_non_https_callback(
    gateway: ApolloGateway,
) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        gateway.enrich_batch(_people(1), "http://callbacks.test/token")


def test_callback_url_and_capability_token_are_redacted_from_http_logs(
    gateway: ApolloGateway, caplog: pytest.LogCaptureFixture
) -> None:
    token = "secret-capability-token"
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(
            "POST %s",
            "https://api.apollo.io/api/v1/people/bulk_match?"
            "webhook_url=https%3A%2F%2Fapi.example.test%2Fwebhooks%2Fapollo%2F" + token,
        )

    assert token not in caplog.text
    assert "webhook_url=[REDACTED]" in caplog.text


def test_apollo_poll_returns_pending_with_retry_after(
    respx_mock: respx.MockRouter, gateway: ApolloGateway
) -> None:
    respx_mock.get(f"{APOLLO_WEBHOOK_RESULT_URL}/123").mock(
        return_value=httpx.Response(
            404,
            json={"error_code": "result_pending", "retry_after_seconds": 17},
        )
    )

    result = gateway.poll_enrichment("123")

    assert result == EnrichmentPending(retry_after_seconds=17)


def test_apollo_poll_normalizes_ready_phone_result(
    respx_mock: respx.MockRouter, gateway: ApolloGateway
) -> None:
    respx_mock.get(f"{APOLLO_WEBHOOK_RESULT_URL}/123").mock(
        return_value=httpx.Response(
            200,
            json={
                "credits_consumed": 8,
                "people": [
                    {
                        "id": "person-0",
                        "phone_numbers": [
                            {
                                "raw_number": "+1 212 555 0112",
                                "type_cd": "mobile",
                                "status_cd": "valid_number",
                            }
                        ],
                    }
                ],
            },
        )
    )

    result = gateway.poll_enrichment("123")

    assert not isinstance(result, EnrichmentPending)
    assert result.request_id == "123"
    assert result.charged_credits == 8
    assert result.people[0].contacts[0].kind == "phone"
    assert result.people[0].contacts[0].value == "+1 212 555 0112"


def test_apollo_normalizes_native_phone_webhook_shape() -> None:
    result = normalize_enrichment_payload(
        {
            "status": "success",
            "total_requested_enrichments": 1,
            "unique_enriched_records": 1,
            "missing_records": 0,
            "credits_consumed": 8,
            "people": [
                {
                    "id": "person-0",
                    "status": "success",
                    "phone_numbers": [
                        {
                            "raw_number": "+1 212 555 0112",
                            "sanitized_number": "+12125550112",
                            "type_cd": "mobile",
                            "status_cd": "valid_number",
                        }
                    ],
                }
            ],
        },
        expected_request_id="123",
    )

    assert result.people[0].provider_person_id == "person-0"
    assert result.people[0].contacts[0].value == "+1 212 555 0112"
    assert result.people[0].contacts[0].classification == "personal"
    assert result.people[0].contacts[0].verification_state == "verified"
    assert result.charged_credits == 8


@pytest.mark.parametrize(
    ("status_code", "error_code"),
    [
        (400, "invalid_request_id"),
        (404, "request_id_unknown"),
        (410, "request_id_expired"),
    ],
)
def test_apollo_poll_maps_terminal_errors_without_provider_payload_leak(
    respx_mock: respx.MockRouter,
    gateway: ApolloGateway,
    status_code: int,
    error_code: str,
) -> None:
    respx_mock.get(f"{APOLLO_WEBHOOK_RESULT_URL}/123").mock(
        return_value=httpx.Response(
            status_code,
            json={"error_code": error_code, "secret": "raw-provider-secret"},
        )
    )

    with pytest.raises(ProviderPayloadError) as raised:
        gateway.poll_enrichment("123")

    assert error_code in str(raised.value)
    assert "raw-provider-secret" not in str(raised.value)
