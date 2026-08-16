import math
import time
from collections.abc import Iterable, Mapping
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Self

import httpx

from app.clients.taxonomy import IndustryTaxonomy
from app.core.config import WorkerSettings
from app.core.log_redaction import install_sensitive_data_log_filters
from app.providers.base import (
    EnrichedContactSet,
    EnrichmentInput,
    EnrichmentPending,
    EnrichmentReceipt,
    EnrichmentResult,
    ProviderAuthenticationError,
    ProviderContact,
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
APOLLO_BULK_ENRICHMENT_URL = "https://api.apollo.io/api/v1/people/bulk_match"
APOLLO_WEBHOOK_RESULT_URL = "https://api.apollo.io/api/v1/webhook_result"
_MAX_PEOPLE_PER_PAGE = 100
_MAX_UNIQUE_PEOPLE = 300
_INDUSTRY_TAXONOMY = IndustryTaxonomy.load_version("v1")


class ApolloGateway:
    """One Apollo search session, scoped to one sourcing run."""

    def __init__(
        self,
        settings: WorkerSettings,
        client: httpx.Client | None = None,
    ) -> None:
        install_sensitive_data_log_filters()
        self._api_key = settings.apollo_api_key.get_secret_value()
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None
        self._seen_provider_ids: set[str] = set()
        self._contact_retention_days = settings.apollo_contact_retention_days

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
        except httpx.HTTPError:
            raise ProviderTemporaryError("provider request failed") from None

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

    def enrich_batch(
        self,
        people: tuple[EnrichmentInput, ...],
        webhook_url: str,
        *,
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
    ) -> EnrichmentReceipt:
        if not people:
            raise ValueError("enrichment batch must not be empty")
        if len(people) > 10:
            raise ValueError("enrichment batch may contain at most 10 people")
        if not webhook_url.startswith("https://"):
            raise ValueError("enrichment callback must use HTTPS")
        parameters: dict[str, str | bool] = {
            "reveal_personal_emails": reveal_personal_emails,
            "reveal_phone_number": reveal_phone_number,
        }
        if reveal_phone_number:
            parameters["webhook_url"] = webhook_url
        try:
            response = self._client.post(
                APOLLO_BULK_ENRICHMENT_URL,
                headers={"accept": "application/json", "x-api-key": self._api_key},
                params=parameters,
                json={"details": [_enrichment_input(person) for person in people]},
                timeout=20.0,
            )
        except httpx.HTTPError:
            raise ProviderTemporaryError("provider request failed") from None
        _raise_for_status(response)
        document = _response_document(response)
        request_id = _enrichment_request_id(document)
        result = normalize_enrichment_payload(
            document,
            expected_request_id=request_id,
            contact_retention_days=self._contact_retention_days,
        )
        charged_credits = _charged_credits(document, len(people))
        assert charged_credits is not None
        return EnrichmentReceipt(
            provider="apollo",
            request_id=request_id,
            submitted_count=len(people),
            result=result,
            charged_units=(
                ("enrichments", len(people)),
                ("estimated_credits", charged_credits),
            ),
        )

    def poll_enrichment(self, request_id: str) -> EnrichmentResult | EnrichmentPending:
        if not request_id or not request_id.lstrip("-").isdigit():
            raise ValueError("provider request ID must be a signed integer")
        try:
            response = self._client.get(
                f"{APOLLO_WEBHOOK_RESULT_URL}/{request_id}",
                headers={"accept": "application/json", "x-api-key": self._api_key},
                timeout=20.0,
            )
        except httpx.HTTPError:
            raise ProviderTemporaryError("provider request failed") from None
        document = _response_document(response)
        if (
            response.status_code == 404
            and document.get("error_code") == "result_pending"
        ):
            retry_after = document.get("retry_after_seconds")
            if (
                not isinstance(retry_after, int)
                or isinstance(retry_after, bool)
                or retry_after < 0
            ):
                raise ProviderPayloadError("provider pending retry interval is invalid")
            return EnrichmentPending(retry_after)
        if response.status_code in (400, 404, 410):
            error_code = document.get("error_code")
            allowed = {"invalid_request_id", "request_id_unknown", "request_id_expired"}
            if error_code in allowed:
                raise ProviderPayloadError(f"provider enrichment {error_code}")
        _raise_for_status(response)
        return normalize_enrichment_payload(
            document,
            expected_request_id=request_id,
            contact_retention_days=self._contact_retention_days,
        )


def _request_id(headers: httpx.Headers) -> str | None:
    value = headers.get("x-request-id") or headers.get("request-id")
    if value is None:
        return None
    return value.strip()[:255] or None


def _enrichment_input(value: EnrichmentInput) -> dict[str, str]:
    detail = {"id": value.provider_person_id}
    if value.linkedin_url:
        detail["linkedin_url"] = value.linkedin_url
    return detail


def _enrichment_request_id(document: Mapping[str, Any]) -> str:
    value = document.get("request_id")
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ProviderPayloadError("provider enrichment request ID is missing")
    normalized = str(value).strip()
    if not normalized or len(normalized) > 255:
        raise ProviderPayloadError("provider enrichment request ID is invalid")
    return normalized


def _charged_credits(document: Mapping[str, Any], fallback: int | None) -> int | None:
    value = document.get("credits_consumed", fallback)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ProviderPayloadError("provider credit usage is invalid")
    try:
        credits = Decimal(str(value))
    except InvalidOperation:
        raise ProviderPayloadError("provider credit usage is invalid") from None
    if not credits.is_finite() or credits < 0:
        raise ProviderPayloadError("provider credit usage is invalid")
    return int(credits.to_integral_value(rounding=ROUND_CEILING))


def normalize_enrichment_payload(
    payload: Mapping[str, Any],
    *,
    expected_request_id: str,
    contact_retention_days: int = 180,
) -> EnrichmentResult:
    supplied_request_id = payload.get("request_id")
    actual_request_id = (
        _enrichment_request_id(payload)
        if supplied_request_id is not None
        else expected_request_id
    )
    if supplied_request_id is not None and actual_request_id != expected_request_id:
        raise ProviderPayloadError("provider enrichment request ID does not match")
    raw_people = _enriched_people(payload)
    people = tuple(
        _enriched_person(value, contact_retention_days) for value in raw_people
    )
    return EnrichmentResult(
        provider="apollo",
        request_id=actual_request_id,
        people=people,
        snapshot_payload=dict(payload),
        charged_credits=_charged_credits(payload, None),
    )


def _enriched_people(document: Mapping[str, Any]) -> list[object]:
    for key in ("people", "matches"):
        value = document.get(key)
        if value is not None:
            if not isinstance(value, list):
                raise ProviderPayloadError("provider enrichment people must be a list")
            return value
    person = document.get("person")
    if person is not None:
        return [person]
    return []


def _enriched_person(value: object, retention_days: int) -> EnrichedContactSet:
    if not isinstance(value, dict):
        raise ProviderPayloadError("provider enriched person must be an object")
    nested = value.get("person")
    if nested is not None:
        if not isinstance(nested, dict):
            raise ProviderPayloadError("provider enriched person must be an object")
        value = nested
    contacts = tuple(
        _email_contacts(value, retention_days) + _phone_contacts(value, retention_days)
    )
    return EnrichedContactSet(
        provider_person_id=_required_string(value, "id"),
        contacts=contacts,
    )


def _enriched_company(person: Mapping[str, object]) -> str | None:
    organization = person.get("organization")
    if organization is None:
        return None
    if not isinstance(organization, dict):
        raise ProviderPayloadError("provider organization must be an object")
    return _optional_string(organization, "name")


def _email_contacts(
    person: Mapping[str, object], retention_days: int
) -> list[ProviderContact]:
    contacts: list[ProviderContact] = []
    primary = _optional_string(person, "email")
    if primary:
        contacts.append(
            ProviderContact(
                kind="email",
                value=primary,
                classification="work",
                verification_state=_verification(person.get("email_status")),
                retention_days=retention_days,
            )
        )
    raw_personal = person.get("personal_emails", [])
    if not isinstance(raw_personal, list):
        raise ProviderPayloadError("provider personal emails must be a list")
    for value in raw_personal:
        if not isinstance(value, str) or not value.strip():
            raise ProviderPayloadError("provider personal email is invalid")
        contacts.append(
            ProviderContact(
                kind="email",
                value=value.strip(),
                classification="personal",
                verification_state="verified",
                retention_days=retention_days,
            )
        )
    return contacts


def _phone_contacts(
    person: Mapping[str, object], retention_days: int
) -> list[ProviderContact]:
    raw_phones = person.get("phone_numbers", [])
    if not isinstance(raw_phones, list):
        raise ProviderPayloadError("provider phone numbers must be a list")
    contacts: list[ProviderContact] = []
    for phone in raw_phones:
        if not isinstance(phone, dict):
            raise ProviderPayloadError("provider phone number must be an object")
        raw = (
            _optional_string(phone, "raw_number")
            or _optional_string(phone, "sanitized_number")
            or _optional_string(phone, "number")
        )
        if raw is None:
            raise ProviderPayloadError("provider phone number value is missing")
        phone_type = (
            _optional_string(phone, "type_cd")
            or _optional_string(phone, "type")
            or "work"
        ).casefold()
        contacts.append(
            ProviderContact(
                kind="phone",
                value=raw,
                classification="personal"
                if phone_type in {"mobile", "personal"}
                else "work",
                verification_state=_verification(
                    phone.get("status_cd", phone.get("status"))
                ),
                retention_days=retention_days,
            )
        )
    return contacts


def _verification(value: object) -> str:
    if not isinstance(value, str):
        return "unverified"
    return (
        "verified"
        if value.casefold()
        in {"verified", "valid", "valid_number", "enrichment_successful"}
        else "unverified"
    )


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
        skills=_optional_string_list(value, "skills"),
        industry_codes=_industry_codes(value, organization),
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


def _optional_string_list(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    raw = value.get(key)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProviderPayloadError(f"provider field {key} must be a list")
    parsed: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ProviderPayloadError(
                f"provider field {key} contains an invalid value"
            )
        parsed.append(item.strip())
    return tuple(parsed)


def _industry_codes(
    person: Mapping[str, object],
    organization: object,
) -> tuple[str, ...]:
    supplied_codes = _optional_string_list(person, "industry_codes")
    resolved = {
        normalized
        for value in supplied_codes
        if _INDUSTRY_TAXONOMY.contains(normalized := value.casefold())
    }
    if isinstance(organization, dict):
        label = _optional_string(organization, "industry")
        if label is not None:
            code = _INDUSTRY_TAXONOMY.code_for_label(label)
            if code is not None:
                resolved.add(code)
    return tuple(sorted(resolved))


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
