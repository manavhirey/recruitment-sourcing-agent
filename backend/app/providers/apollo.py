import math
import time
from collections.abc import Iterable, Mapping
from typing import Any, Self

import httpx

from app.core.config import Settings
from app.providers.base import (
    ProviderAuthenticationError,
    ProviderExperience,
    ProviderPayloadError,
    ProviderPermissionError,
    ProviderPerson,
    ProviderQuery,
    ProviderRateLimited,
    ProviderTemporaryError,
    SearchPage,
)

APOLLO_PEOPLE_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
_MAX_PEOPLE_PER_PAGE = 100
_MAX_UNIQUE_PEOPLE = 300


class ApolloGateway:
    """One Apollo search session, scoped to one sourcing run."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = settings.apollo_api_key.get_secret_value()
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None
        self._seen_provider_ids: set[str] = set()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def restore_seen_provider_ids(self, provider_ids: Iterable[str]) -> None:
        restored = {provider_id for provider_id in provider_ids if provider_id}
        if len(self._seen_provider_ids | restored) > _MAX_UNIQUE_PEOPLE:
            raise ValueError("restored provider IDs exceed the run limit")
        self._seen_provider_ids.update(restored)

    def search(self, query: ProviderQuery, page: int) -> SearchPage:
        if page < 1:
            raise ValueError("page must be positive")
        if len(self._seen_provider_ids) >= _MAX_UNIQUE_PEOPLE:
            return SearchPage(
                people=(),
                page=page,
                next_page=None,
                total_available=None,
                charged_units=(("estimated_credits", 0), ("search_pages", 0)),
            )

        try:
            response = self._client.post(
                APOLLO_PEOPLE_SEARCH_URL,
                headers={"accept": "application/json", "x-api-key": self._api_key},
                json=_search_payload(query, page),
                timeout=20.0,
            )
        except httpx.HTTPError as error:
            raise ProviderTemporaryError("provider request failed") from error

        _raise_for_status(response)
        document = _response_document(response)
        raw_people = document.get("people")
        if not isinstance(raw_people, list):
            raise ProviderPayloadError("provider people payload is invalid")

        normalized_people = tuple(_person(raw_person) for raw_person in raw_people)
        total_available, total_pages = _pagination(document)

        people: list[ProviderPerson] = []
        newly_seen_ids: set[str] = set()
        for person in normalized_people:
            if person.provider_person_id in self._seen_provider_ids:
                continue
            if person.provider_person_id in newly_seen_ids:
                continue
            newly_seen_ids.add(person.provider_person_id)
            people.append(person)
            if len(self._seen_provider_ids) + len(newly_seen_ids) == _MAX_UNIQUE_PEOPLE:
                break
        self._seen_provider_ids.update(newly_seen_ids)

        next_page = _next_page(
            page=page,
            raw_count=len(raw_people),
            total_pages=total_pages,
            unique_count=len(self._seen_provider_ids),
        )
        return SearchPage(
            people=tuple(people),
            page=page,
            next_page=next_page,
            total_available=total_available,
            provider_request_id=_request_id(response.headers),
        )


def _request_id(headers: httpx.Headers) -> str | None:
    value = headers.get("x-request-id") or headers.get("request-id")
    if value is None:
        return None
    return value.strip()[:255] or None


def _search_payload(query: ProviderQuery, page: int) -> dict[str, object]:
    payload: dict[str, object] = {
        "page": page,
        "per_page": _MAX_PEOPLE_PER_PAGE,
    }
    if query.titles:
        payload["person_titles"] = list(query.titles)
    if query.seniorities:
        payload["person_seniorities"] = list(query.seniorities)
    if query.person_locations:
        payload["person_locations"] = list(query.person_locations)
    if query.keywords:
        payload["q_keywords"] = " ".join(query.keywords)
    return payload


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise ProviderAuthenticationError("provider authentication failed")
    if response.status_code == 403:
        raise ProviderPermissionError("provider permission denied")
    if response.status_code == 429:
        raise ProviderRateLimited(_retry_after(response.headers))
    if response.status_code >= 500:
        raise ProviderTemporaryError("provider is temporarily unavailable")
    if response.status_code >= 400:
        raise ProviderPayloadError("provider rejected the search request")


def _retry_after(headers: httpx.Headers) -> int | None:
    retry_after = headers.get("retry-after")
    if retry_after is not None:
        try:
            return max(0, math.ceil(float(retry_after)))
        except ValueError:
            pass

    reset = headers.get("x-ratelimit-reset")
    if reset is None:
        return None
    try:
        reset_value = float(reset)
    except ValueError:
        return None
    if reset_value > time.time():
        reset_value -= time.time()
    return max(0, math.ceil(reset_value))


def _response_document(response: httpx.Response) -> Mapping[str, Any]:
    try:
        document = response.json()
    except ValueError as error:
        raise ProviderPayloadError("provider returned malformed JSON") from error
    if not isinstance(document, dict):
        raise ProviderPayloadError("provider response must be an object")
    return document


def _person(value: object) -> ProviderPerson:
    if not isinstance(value, dict):
        raise ProviderPayloadError("provider person must be an object")
    provider_id = _required_string(value, "id")
    full_name = _full_name(value)
    organization = value.get("organization")
    if organization is not None and not isinstance(organization, dict):
        raise ProviderPayloadError("provider organization must be an object")
    company_name = (
        _optional_string(organization, "name") if organization is not None else None
    )
    experiences = _experiences(value.get("employment_history", []))
    return ProviderPerson(
        provider="apollo",
        provider_person_id=provider_id,
        full_name=full_name,
        current_title=_optional_string(value, "title"),
        current_company=company_name,
        location=_location(value),
        linkedin_url=_optional_string(value, "linkedin_url"),
        experiences=experiences,
    )


def _full_name(person: Mapping[str, object]) -> str:
    name = _optional_string(person, "name")
    if name is not None:
        return name
    first_name = _required_string(person, "first_name")
    last_name = _optional_string(person, "last_name_obfuscated")
    return " ".join(part for part in (first_name, last_name) if part)


def _experiences(value: object) -> tuple[ProviderExperience, ...]:
    if not isinstance(value, list):
        raise ProviderPayloadError("provider employment history must be a list")
    experiences: list[ProviderExperience] = []
    for item in value:
        if not isinstance(item, dict):
            raise ProviderPayloadError("provider experience must be an object")
        experiences.append(
            ProviderExperience(
                title=_optional_string(item, "title"),
                company_name=_optional_string(item, "organization_name"),
                start_date=_optional_string(item, "start_date"),
                end_date=_optional_string(item, "end_date"),
            )
        )
    return tuple(experiences)


def _location(person: Mapping[str, object]) -> str | None:
    explicit = _optional_string(person, "location")
    if explicit is not None:
        return explicit
    parts = [
        part
        for key in ("city", "state", "country")
        if (part := _optional_string(person, key)) is not None
    ]
    return ", ".join(parts) or None


def _required_string(value: Mapping[str, object], key: str) -> str:
    parsed = _optional_string(value, key)
    if parsed is None:
        raise ProviderPayloadError(f"provider person {key} is missing")
    return parsed


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise ProviderPayloadError(f"provider field {key} must be a string")
    normalized = item.strip()
    return normalized or None


def _pagination(document: Mapping[str, Any]) -> tuple[int | None, int | None]:
    top_level_total = _non_negative_int(document.get("total_entries"), "total_entries")
    value = document.get("pagination")
    if value is None:
        total_pages = (
            math.ceil(top_level_total / _MAX_PEOPLE_PER_PAGE)
            if top_level_total is not None
            else None
        )
        return top_level_total, total_pages
    if not isinstance(value, dict):
        raise ProviderPayloadError("provider pagination must be an object")
    nested_total = _non_negative_int(value.get("total_entries"), "total_entries")
    total_available = top_level_total if top_level_total is not None else nested_total
    total_pages = _non_negative_int(value.get("total_pages"), "total_pages")
    if total_pages is None and total_available is not None:
        total_pages = math.ceil(total_available / _MAX_PEOPLE_PER_PAGE)
    return total_available, total_pages


def _non_negative_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProviderPayloadError(f"provider {field} must be non-negative")
    return value


def _next_page(
    *, page: int, raw_count: int, total_pages: int | None, unique_count: int
) -> int | None:
    if unique_count >= _MAX_UNIQUE_PEOPLE or raw_count == 0:
        return None
    if total_pages is not None:
        return page + 1 if page < total_pages else None
    return page + 1 if raw_count == _MAX_PEOPLE_PER_PAGE else None
