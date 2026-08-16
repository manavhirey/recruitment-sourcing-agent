from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.candidates.models import Candidate, SourceIdentity
from app.candidates.normalization import normalize_profile_url, normalize_text
from app.candidates.schemas import ResolutionDecision
from app.identity.schemas import RequestContext
from app.providers.base import ProviderPerson

_NAME_THRESHOLD = 0.88
_CORROBORATING_THRESHOLD = 0.85
_OVERALL_THRESHOLD = 0.84


class CandidateResolver:
    def __init__(self, session: Session) -> None:
        self.session = session

    def resolve(
        self, context: RequestContext, person: ProviderPerson
    ) -> ResolutionDecision:
        by_provider = self._by_provider_id(
            context.tenant_id, person.provider, person.provider_person_id
        )
        normalized_url = normalize_profile_url(person.linkedin_url)
        if by_provider is not None:
            by_url = (
                self._by_profile_url(context.tenant_id, normalized_url)
                if normalized_url is not None
                else None
            )
            conflicts = (
                (by_url,) if by_url is not None and by_url != by_provider else ()
            )
            return ResolutionDecision.reuse(
                by_provider,
                "provider_id",
                conflict_candidate_ids=conflicts,
            )
        if normalized_url is not None:
            by_url = self._by_profile_url(context.tenant_id, normalized_url)
            if by_url is not None:
                return ResolutionDecision.reuse(by_url, "profile_url")
        fuzzy = self._fuzzy_candidates(context.tenant_id, person)
        return ResolutionDecision.create_with_suggestions(
            tuple(candidate_id for candidate_id, _ in fuzzy)
        )

    def fuzzy_candidates(
        self, tenant_id: UUID, person: ProviderPerson
    ) -> tuple[tuple[UUID, float], ...]:
        return self._fuzzy_candidates(tenant_id, person)

    def _by_provider_id(
        self, tenant_id: UUID, provider: str, provider_person_id: str
    ) -> UUID | None:
        return self.session.scalar(
            select(SourceIdentity.candidate_id).where(
                SourceIdentity.tenant_id == tenant_id,
                SourceIdentity.provider == provider,
                SourceIdentity.provider_person_id == provider_person_id,
            )
        )

    def _by_profile_url(
        self, tenant_id: UUID, normalized_profile_url: str
    ) -> UUID | None:
        return self.session.scalar(
            select(SourceIdentity.candidate_id)
            .join(
                Candidate,
                (Candidate.tenant_id == SourceIdentity.tenant_id)
                & (Candidate.id == SourceIdentity.candidate_id),
            )
            .where(
                SourceIdentity.tenant_id == tenant_id,
                Candidate.tenant_id == tenant_id,
                SourceIdentity.normalized_profile_url == normalized_profile_url,
            )
            .order_by(SourceIdentity.id)
            .limit(1)
        )

    def _fuzzy_candidates(
        self, tenant_id: UUID, person: ProviderPerson
    ) -> tuple[tuple[UUID, float], ...]:
        incoming = (
            normalize_text(person.full_name),
            normalize_text(person.current_company),
            normalize_text(person.current_title),
            normalize_text(person.location),
        )
        if incoming[0] is None:
            return ()
        matches: list[tuple[UUID, float]] = []
        candidates = self.session.scalars(
            select(Candidate).where(Candidate.tenant_id == tenant_id)
        )
        for candidate in candidates:
            existing = (
                candidate.normalized_name,
                candidate.normalized_company,
                candidate.normalized_title,
                candidate.normalized_location,
            )
            name_score = _similarity(incoming[0], existing[0])
            corroborating = [
                _similarity(left, right)
                for left, right in zip(incoming[1:], existing[1:], strict=True)
                if left is not None and right is not None
            ]
            if (
                name_score < _NAME_THRESHOLD
                or not corroborating
                or max(corroborating) < _CORROBORATING_THRESHOLD
            ):
                continue
            scores = [name_score, *corroborating]
            overall = sum(scores) / len(scores)
            if overall >= _OVERALL_THRESHOLD:
                matches.append((candidate.id, overall))
        matches.sort(key=lambda item: (-item[1], item[0].int))
        return tuple(matches)


def _similarity(left: str | None, right: str | None) -> float:
    if left is None or right is None:
        return 0.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()
