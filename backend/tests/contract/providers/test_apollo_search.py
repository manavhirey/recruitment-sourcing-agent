import json
from collections.abc import Iterator

import httpx
import pytest
import respx

from app.core.config import Settings
from app.providers.apollo import APOLLO_PEOPLE_SEARCH_URL, ApolloGateway
from app.providers.base import (
    ProviderAuthenticationError,
    ProviderPayloadError,
    ProviderPermissionError,
    ProviderQuery,
    ProviderRateLimited,
    ProviderTemporaryError,
)


@pytest.fixture
def provider_query() -> ProviderQuery:
    return ProviderQuery(
        titles=("Product Manager", "Senior Product Manager"),
        seniorities=("manager", "senior"),
        person_locations=("New York, NY",),
        industry_codes=("financial_services.banking",),
        keywords=("Banking", "Payments platform experience"),
    )


@pytest.fixture
def apollo_gateway() -> Iterator[ApolloGateway]:
    gateway = ApolloGateway(Settings.for_test())
    yield gateway
    gateway.close()


def test_apollo_search_normalizes_people(
    respx_mock: respx.MockRouter,
    apollo_gateway: ApolloGateway,
    provider_query: ProviderQuery,
) -> None:
    route = respx_mock.post(APOLLO_PEOPLE_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"X-Request-ID": "apollo-request-123"},
            json={
                "people": [
                    {
                        "id": "p1",
                        "name": "Priya Sharma",
                        "title": "Senior Product Manager",
                        "linkedin_url": "https://www.linkedin.com/in/priya-sharma",
                        "city": "New York",
                        "state": "New York",
                        "country": "United States",
                        "organization": {"name": "PayFlow"},
                        "employment_history": [
                            {
                                "title": "Product Manager",
                                "organization_name": "BankCo",
                                "start_date": "2021-01-01",
                                "end_date": "2024-05-01",
                            }
                        ],
                    }
                ],
                "pagination": {
                    "page": 1,
                    "per_page": 100,
                    "total_entries": 1,
                    "total_pages": 1,
                },
            },
        )
    )

    page = apollo_gateway.search(provider_query, page=1)

    assert page.people[0].provider_person_id == "p1"
    assert page.people[0].current_title == "Senior Product Manager"
    assert page.people[0].current_company == "PayFlow"
    assert page.people[0].location == "New York, New York, United States"
    assert page.people[0].experiences[0].company_name == "BankCo"
    assert page.next_page is None
    assert page.provider_request_id == "apollo-request-123"
    assert dict(page.charged_units) == {
        "estimated_credits": 1,
        "search_pages": 1,
    }
    request = route.calls[0].request
    assert request.headers["x-api-key"] == "test-apollo-key"
    assert request.url.path == "/api/v1/mixed_people/api_search"
    assert request.read()
    assert request.headers["content-type"] == "application/json"
    assert route.calls[0].request.content
    assert json.loads(request.content) == {
        "page": 1,
        "per_page": 100,
        "person_titles": ["Product Manager", "Senior Product Manager"],
        "person_seniorities": ["manager", "senior"],
        "person_locations": ["New York, NY"],
        "q_keywords": "Banking Payments platform experience",
    }
    assert request.extensions["timeout"] == {
        "connect": 20.0,
        "read": 20.0,
        "write": 20.0,
        "pool": 20.0,
    }


def test_apollo_search_returns_empty_page(
    respx_mock: respx.MockRouter,
    apollo_gateway: ApolloGateway,
    provider_query: ProviderQuery,
) -> None:
    respx_mock.post(APOLLO_PEOPLE_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "people": [],
                "total_entries": 0,
            },
        )
    )

    page = apollo_gateway.search(provider_query, page=1)

    assert page.people == ()
    assert page.next_page is None
    assert page.total_available == 0


def test_apollo_search_normalizes_documented_limited_person_shape(
    respx_mock: respx.MockRouter,
    apollo_gateway: ApolloGateway,
    provider_query: ProviderQuery,
) -> None:
    respx_mock.post(APOLLO_PEOPLE_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "total_entries": 31993,
                "people": [
                    {
                        "id": "p1",
                        "first_name": "Priya",
                        "last_name_obfuscated": "Sh***a",
                        "title": "Senior Product Manager",
                        "organization": {
                            "name": "PayFlow",
                            "has_industry": True,
                        },
                    }
                ],
            },
        )
    )

    page = apollo_gateway.search(provider_query, page=1)

    assert page.people[0].full_name == "Priya Sh***a"
    assert page.people[0].current_company == "PayFlow"
    assert page.people[0].linkedin_url is None
    assert page.people[0].experiences == ()
    assert page.total_available == 31993
    assert page.next_page == 2


def test_apollo_search_pages_and_deduplicates_provider_ids_stably(
    respx_mock: respx.MockRouter,
    apollo_gateway: ApolloGateway,
    provider_query: ProviderQuery,
) -> None:
    route = respx_mock.post(APOLLO_PEOPLE_SEARCH_URL)
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "people": [
                    {"id": f"p{offset}", "name": f"Person {offset}"}
                    for offset in range(100)
                ],
                "total_entries": 101,
            },
        ),
        httpx.Response(
            200,
            json={
                "people": [
                    {"id": "p99", "name": "Person 99 duplicate"},
                    {"id": "p100", "name": "Person 100"},
                ],
                "total_entries": 101,
            },
        ),
    ]

    first = apollo_gateway.search(provider_query, page=1)
    second = apollo_gateway.search(provider_query, page=2)

    assert [person.provider_person_id for person in first.people] == [
        f"p{offset}" for offset in range(100)
    ]
    assert first.next_page == 2
    assert [person.provider_person_id for person in second.people] == ["p100"]
    assert second.next_page is None


def test_apollo_search_stops_after_300_unique_people(
    respx_mock: respx.MockRouter,
    apollo_gateway: ApolloGateway,
    provider_query: ProviderQuery,
) -> None:
    route = respx_mock.post(APOLLO_PEOPLE_SEARCH_URL)
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "people": [
                    {"id": f"p{offset}", "name": f"Person {offset}"}
                    for offset in range(start, start + 100)
                ],
                "total_entries": 400,
            },
        )
        for page, start in ((1, 0), (2, 100), (3, 200))
    ]

    first = apollo_gateway.search(provider_query, page=1)
    second = apollo_gateway.search(provider_query, page=2)
    third = apollo_gateway.search(provider_query, page=3)

    assert len(first.people) == len(second.people) == len(third.people) == 100
    assert first.next_page == 2
    assert second.next_page == 3
    assert third.next_page is None

    fourth = apollo_gateway.search(provider_query, page=4)

    assert fourth.people == ()
    assert fourth.next_page is None
    assert route.call_count == 3


def test_apollo_search_does_not_consume_ids_from_a_malformed_page(
    respx_mock: respx.MockRouter,
    apollo_gateway: ApolloGateway,
    provider_query: ProviderQuery,
) -> None:
    route = respx_mock.post(APOLLO_PEOPLE_SEARCH_URL)
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "people": [
                    {"id": "p1", "name": "Priya Sharma"},
                    {"name": "Missing ID"},
                ],
                "total_entries": 2,
            },
        ),
        httpx.Response(
            200,
            json={
                "people": [
                    {"id": "p1", "name": "Priya Sharma"},
                    {"id": "p2", "name": "Sam Lee"},
                ],
                "total_entries": 2,
            },
        ),
    ]

    with pytest.raises(ProviderPayloadError):
        apollo_gateway.search(provider_query, page=1)

    retry = apollo_gateway.search(provider_query, page=1)

    assert [person.provider_person_id for person in retry.people] == ["p1", "p2"]


def test_apollo_search_scopes_deduplication_to_a_gateway_run(
    respx_mock: respx.MockRouter,
    provider_query: ProviderQuery,
) -> None:
    respx_mock.post(APOLLO_PEOPLE_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "people": [{"id": "p1", "name": "Priya Sharma"}],
                "total_entries": 1,
            },
        )
    )

    with ApolloGateway(Settings.for_test()) as first_run:
        first = first_run.search(provider_query, page=1)
    with ApolloGateway(Settings.for_test()) as second_run:
        second = second_run.search(provider_query, page=1)

    assert [person.provider_person_id for person in first.people] == ["p1"]
    assert [person.provider_person_id for person in second.people] == ["p1"]


def test_apollo_search_restores_run_seen_ids_after_task_retry(
    respx_mock: respx.MockRouter,
    apollo_gateway: ApolloGateway,
    provider_query: ProviderQuery,
) -> None:
    respx_mock.post(APOLLO_PEOPLE_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "people": [
                    {"id": "p1", "name": "Already Seen"},
                    {"id": "p2", "name": "New Person"},
                ],
                "total_entries": 2,
            },
        )
    )
    apollo_gateway.restore_seen_provider_ids({"p1"})

    page = apollo_gateway.search(provider_query, page=1)

    assert [person.provider_person_id for person in page.people] == ["p2"]


def test_apollo_search_enforces_timeout_for_injected_client(
    respx_mock: respx.MockRouter,
    provider_query: ProviderQuery,
) -> None:
    route = respx_mock.post(APOLLO_PEOPLE_SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"people": [], "total_entries": 0},
        )
    )

    with httpx.Client(timeout=1.0) as client:
        gateway = ApolloGateway(Settings.for_test(), client=client)
        gateway.search(provider_query, page=1)

    assert route.calls[0].request.extensions["timeout"] == {
        "connect": 20.0,
        "read": 20.0,
        "write": 20.0,
        "pool": 20.0,
    }


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, ProviderAuthenticationError),
        (403, ProviderPermissionError),
        (500, ProviderTemporaryError),
        (503, ProviderTemporaryError),
    ],
)
def test_apollo_search_maps_status_errors(
    respx_mock: respx.MockRouter,
    apollo_gateway: ApolloGateway,
    provider_query: ProviderQuery,
    status: int,
    error_type: type[Exception],
) -> None:
    respx_mock.post(APOLLO_PEOPLE_SEARCH_URL).mock(
        return_value=httpx.Response(status, json={"error": "provider detail"})
    )

    with pytest.raises(error_type):
        apollo_gateway.search(provider_query, page=1)


def test_apollo_search_maps_rate_limit_reset(
    respx_mock: respx.MockRouter,
    apollo_gateway: ApolloGateway,
    provider_query: ProviderQuery,
) -> None:
    respx_mock.post(APOLLO_PEOPLE_SEARCH_URL).mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "17"},
            json={"error": "rate limited"},
        )
    )

    with pytest.raises(ProviderRateLimited) as caught:
        apollo_gateway.search(provider_query, page=1)

    assert caught.value.retry_after == 17


def test_apollo_search_maps_provider_reset_header(
    respx_mock: respx.MockRouter,
    apollo_gateway: ApolloGateway,
    provider_query: ProviderQuery,
) -> None:
    respx_mock.post(APOLLO_PEOPLE_SEARCH_URL).mock(
        return_value=httpx.Response(
            429,
            headers={"X-RateLimit-Reset": "19"},
            json={"error": "rate limited"},
        )
    )

    with pytest.raises(ProviderRateLimited) as caught:
        apollo_gateway.search(provider_query, page=1)

    assert caught.value.retry_after == 19


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"people": "not-a-list"}),
        httpx.Response(200, json={"people": [{"name": "Missing ID"}]}),
    ],
)
def test_apollo_search_maps_invalid_success_payloads(
    respx_mock: respx.MockRouter,
    apollo_gateway: ApolloGateway,
    provider_query: ProviderQuery,
    response: httpx.Response,
) -> None:
    respx_mock.post(APOLLO_PEOPLE_SEARCH_URL).mock(return_value=response)

    with pytest.raises(ProviderPayloadError):
        apollo_gateway.search(provider_query, page=1)


def test_apollo_search_maps_transport_errors(
    respx_mock: respx.MockRouter,
    apollo_gateway: ApolloGateway,
    provider_query: ProviderQuery,
) -> None:
    respx_mock.post(APOLLO_PEOPLE_SEARCH_URL).mock(
        side_effect=httpx.ConnectError("offline")
    )

    with pytest.raises(ProviderTemporaryError):
        apollo_gateway.search(provider_query, page=1)
