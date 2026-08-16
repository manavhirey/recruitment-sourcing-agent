import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
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
    contacts: tuple["ProviderContact", ...] = ()


@dataclass(frozen=True)
class ProviderContact:
    kind: str
    value: str = field(repr=False)
    classification: str = "work"
    verification_state: str = "unverified"
    confidence: float = 1.0
    observed_at: datetime | None = None


@dataclass(frozen=True)
class EnrichedContactSet:
    provider_person_id: str
    contacts: tuple[ProviderContact, ...]


@dataclass(frozen=True)
class SearchPage:
    people: tuple[ProviderPerson, ...]
    page: int
    next_page: int | None
    total_available: int | None
    provider_request_id: str | None = None
    charged_units: tuple[tuple[str, int], ...] = (
        ("estimated_credits", 1),
        ("search_pages", 1),
    )


@dataclass(frozen=True)
class EnrichmentInput:
    provider_person_id: str
    linkedin_url: str | None


@dataclass(frozen=True)
class EnrichmentReceipt:
    provider: str
    request_id: str
    submitted_count: int
    result: "EnrichmentResult | None" = None
    charged_units: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class EnrichmentResult:
    provider: str
    request_id: str
    people: tuple[EnrichedContactSet, ...]
    snapshot_payload: dict[str, object] = field(default_factory=dict, repr=False)
    charged_credits: int | None = None


@dataclass(frozen=True)
class EnrichmentPending:
    retry_after_seconds: int


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
        self,
        people: tuple[EnrichmentInput, ...],
        webhook_url: str,
        *,
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
    ) -> EnrichmentReceipt:
        raise NotImplementedError

    def poll_enrichment(self, request_id: str) -> EnrichmentResult | EnrichmentPending:
        raise NotImplementedError
