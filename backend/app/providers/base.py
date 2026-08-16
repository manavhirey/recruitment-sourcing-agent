import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderQuery:
    titles: tuple[str, ...]
    seniorities: tuple[str, ...]
    person_locations: tuple[str, ...]
    industry_codes: tuple[str, ...]
    keywords: tuple[str, ...]

    @property
    def query_hash(self) -> str:
        normalized = json.dumps(
            asdict(self),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(normalized.encode()).hexdigest()


@dataclass(frozen=True)
class ProviderExperience:
    title: str | None
    company_name: str | None
    start_date: str | None
    end_date: str | None


@dataclass(frozen=True)
class ProviderPerson:
    provider: str
    provider_person_id: str
    full_name: str
    current_title: str | None
    current_company: str | None
    location: str | None
    linkedin_url: str | None
    experiences: tuple[ProviderExperience, ...]


@dataclass(frozen=True)
class SearchPage:
    people: tuple[ProviderPerson, ...]
    page: int
    next_page: int | None
    total_available: int | None


@dataclass(frozen=True)
class EnrichmentInput:
    provider_person_id: str
    linkedin_url: str | None


@dataclass(frozen=True)
class EnrichmentReceipt:
    provider: str
    request_id: str
    submitted_count: int


@dataclass(frozen=True)
class EnrichmentResult:
    provider: str
    request_id: str
    people: tuple[ProviderPerson, ...]


class ProviderError(RuntimeError):
    """Base class for normalized provider failures."""


class ProviderRateLimited(ProviderError):
    def __init__(self, retry_after: int | None) -> None:
        super().__init__("provider rate limit exceeded")
        self.retry_after = retry_after


class ProviderAuthenticationError(ProviderError):
    pass


class ProviderPermissionError(ProviderError):
    pass


class ProviderTemporaryError(ProviderError):
    pass


class ProviderPayloadError(ProviderError):
    pass


class ProviderGateway(Protocol):
    def search(self, query: ProviderQuery, page: int) -> SearchPage:
        raise NotImplementedError

    def enrich_batch(
        self, people: tuple[EnrichmentInput, ...], webhook_url: str
    ) -> EnrichmentReceipt:
        raise NotImplementedError

    def poll_enrichment(self, request_id: str) -> EnrichmentResult | None:
        raise NotImplementedError
