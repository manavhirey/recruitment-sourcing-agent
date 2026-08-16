import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.candidates.models import (
    Candidate,
    CandidateExperience,
    CandidateFieldProvenance,
    DuplicateSuggestion,
    SourceIdentity,
)
from app.candidates.normalization import (
    normalize_profile_url,
    normalize_text,
    observed_value_hash,
)
from app.candidates.resolver import CandidateResolver
from app.candidates.schemas import (
    CandidateExperienceProfile,
    CandidateProfile,
    ResolutionResult,
)
from app.identity.schemas import RequestContext
from app.providers.base import ProviderExperience, ProviderPerson

_DISPLAY_FIELDS = (
    ("full_name", "full_name", "normalized_name"),
    ("current_title", "current_title", "normalized_title"),
    ("current_company", "current_company", "normalized_company"),
    ("location", "location", "normalized_location"),
    ("profile_url", "profile_url", "normalized_profile_url"),
)


class CandidateService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.resolver = CandidateResolver(session)

    def ingest(
        self,
        context: RequestContext,
        provider_person: ProviderPerson,
        *,
        source_timestamp: datetime | None = None,
        confidence: float = 1.0,
    ) -> ResolutionResult:
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        timestamp = _as_utc(source_timestamp or datetime.now(UTC))
        normalized_url = normalize_profile_url(provider_person.linkedin_url)
        self._lock_identity(
            context.tenant_id,
            provider_person.provider,
            provider_person.provider_person_id,
            normalized_url,
        )
        decision = self.resolver.resolve(context, provider_person)
        created = decision.candidate_id is None
        if created:
            full_name = _display_value(provider_person.full_name) or "Unknown Candidate"
            candidate = Candidate(
                id=uuid4(),
                tenant_id=context.tenant_id,
                full_name=full_name,
                normalized_name=normalize_text(full_name) or "unknown candidate",
            )
            self.session.add(candidate)
            self.session.flush()
        else:
            existing_candidate = self.session.scalar(
                select(Candidate).where(
                    Candidate.tenant_id == context.tenant_id,
                    Candidate.id == decision.candidate_id,
                )
            )
            if existing_candidate is None:
                raise RuntimeError("resolved candidate is not visible in tenant")
            candidate = existing_candidate

        identity = self._source_identity(
            context.tenant_id,
            candidate.id,
            provider_person,
            normalized_url,
            timestamp,
            confidence,
        )
        self._observe_fields(
            candidate, identity, provider_person, timestamp, confidence, normalized_url
        )
        self._observe_experiences(
            candidate,
            identity,
            provider_person.experiences,
            timestamp,
            confidence,
        )
        duplicate_suggestion_id = self._suggest_duplicates(
            context.tenant_id,
            candidate.id,
            provider_person,
            decision.fuzzy_candidate_ids,
        )
        self.session.flush()
        return ResolutionResult(
            candidate_id=candidate.id,
            source_identity_id=identity.id,
            created=created,
            matched_by=decision.matched_by,
            duplicate_suggestion_id=duplicate_suggestion_id,
        )

    def get_profile(
        self, context: RequestContext, candidate_id: UUID
    ) -> CandidateProfile | None:
        candidate = self.session.scalar(
            select(Candidate).where(
                Candidate.tenant_id == context.tenant_id,
                Candidate.id == candidate_id,
            )
        )
        if candidate is None:
            return None
        experiences = self.session.scalars(
            select(CandidateExperience)
            .where(
                CandidateExperience.tenant_id == context.tenant_id,
                CandidateExperience.candidate_id == candidate_id,
            )
            .order_by(CandidateExperience.position, CandidateExperience.id)
        ).all()
        profile_values = CandidateProfile.model_validate(candidate).model_dump()
        profile_values.pop("experiences")
        return CandidateProfile(
            **profile_values,
            experiences=tuple(
                CandidateExperienceProfile.model_validate(item) for item in experiences
            ),
        )

    def _lock_identity(
        self,
        tenant_id: UUID,
        provider: str,
        provider_person_id: str,
        normalized_url: str | None,
    ) -> None:
        if self.session.bind is None or self.session.bind.dialect.name != "postgresql":
            return
        keys = {
            _advisory_key(f"provider\0{tenant_id}\0{provider}\0{provider_person_id}")
        }
        if normalized_url is not None:
            keys.add(_advisory_key(f"url\0{tenant_id}\0{normalized_url}"))
        for key in sorted(keys):
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": key}
            )

    def _source_identity(
        self,
        tenant_id: UUID,
        candidate_id: UUID,
        person: ProviderPerson,
        normalized_url: str | None,
        source_timestamp: datetime,
        confidence: float,
    ) -> SourceIdentity:
        identity = self.session.scalar(
            select(SourceIdentity).where(
                SourceIdentity.tenant_id == tenant_id,
                SourceIdentity.provider == person.provider,
                SourceIdentity.provider_person_id == person.provider_person_id,
            )
        )
        if identity is None and normalized_url is not None:
            identity = self.session.scalar(
                select(SourceIdentity).where(
                    SourceIdentity.tenant_id == tenant_id,
                    SourceIdentity.provider == person.provider,
                    SourceIdentity.normalized_profile_url == normalized_url,
                )
            )
        now = datetime.now(UTC)
        if identity is None:
            identity = SourceIdentity(
                id=uuid4(),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                provider=person.provider,
                provider_person_id=person.provider_person_id,
                profile_url=_display_value(person.linkedin_url),
                normalized_profile_url=normalized_url,
                source_timestamp=source_timestamp,
                confidence=confidence,
                first_seen_at=now,
                last_seen_at=now,
            )
            self.session.add(identity)
            self.session.flush()
            return identity
        identity.last_seen_at = now
        if _is_newer_not_lower(
            source_timestamp,
            confidence,
            identity.source_timestamp,
            identity.confidence,
        ):
            identity.source_timestamp = source_timestamp
            identity.confidence = confidence
            if normalized_url is not None:
                identity.profile_url = _display_value(person.linkedin_url)
                identity.normalized_profile_url = normalized_url
        return identity

    def _observe_fields(
        self,
        candidate: Candidate,
        identity: SourceIdentity,
        person: ProviderPerson,
        source_timestamp: datetime,
        confidence: float,
        normalized_url: str | None,
    ) -> None:
        values = {
            "full_name": _display_value(person.full_name),
            "current_title": _display_value(person.current_title),
            "current_company": _display_value(person.current_company),
            "location": _display_value(person.location),
            "profile_url": normalized_url,
        }
        for field_name, display_attribute, normalized_attribute in _DISPLAY_FIELDS:
            value = values[field_name]
            if value is None:
                continue
            value_hash = observed_value_hash(value)
            observation = self.session.scalar(
                select(CandidateFieldProvenance).where(
                    CandidateFieldProvenance.tenant_id == candidate.tenant_id,
                    CandidateFieldProvenance.source_identity_id == identity.id,
                    CandidateFieldProvenance.field_name == field_name,
                    CandidateFieldProvenance.observed_value_hash == value_hash,
                )
            )
            current = self.session.scalar(
                select(CandidateFieldProvenance).where(
                    CandidateFieldProvenance.tenant_id == candidate.tenant_id,
                    CandidateFieldProvenance.candidate_id == candidate.id,
                    CandidateFieldProvenance.field_name == field_name,
                    CandidateFieldProvenance.is_current.is_(True),
                )
            )
            select_value = current is None or _is_newer_not_lower(
                source_timestamp,
                confidence,
                current.source_timestamp,
                current.confidence,
            )
            if observation is None:
                observation = CandidateFieldProvenance(
                    tenant_id=candidate.tenant_id,
                    candidate_id=candidate.id,
                    source_identity_id=identity.id,
                    field_name=field_name,
                    provider=identity.provider,
                    source_timestamp=source_timestamp,
                    observed_value_hash=value_hash,
                    confidence=confidence,
                    is_current=False,
                )
                self.session.add(observation)
            elif _is_newer_not_lower(
                source_timestamp,
                confidence,
                observation.source_timestamp,
                observation.confidence,
            ):
                observation.source_timestamp = source_timestamp
                observation.confidence = confidence
            if select_value:
                if current is not None and current is not observation:
                    current.is_current = False
                    self.session.flush()
                observation.is_current = True
                setattr(candidate, display_attribute, value)
                normalized_value = (
                    value if field_name == "profile_url" else normalize_text(value)
                )
                setattr(candidate, normalized_attribute, normalized_value)

    def _observe_experiences(
        self,
        candidate: Candidate,
        identity: SourceIdentity,
        experiences: tuple[ProviderExperience, ...],
        source_timestamp: datetime,
        confidence: float,
    ) -> None:
        for position, experience in enumerate(experiences):
            payload = {
                "company_name": _display_value(experience.company_name),
                "end_date": _display_value(experience.end_date),
                "start_date": _display_value(experience.start_date),
                "title": _display_value(experience.title),
            }
            if not any(payload.values()):
                continue
            value_hash = observed_value_hash(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )
            record = self.session.scalar(
                select(CandidateExperience).where(
                    CandidateExperience.tenant_id == candidate.tenant_id,
                    CandidateExperience.source_identity_id == identity.id,
                    CandidateExperience.position == position,
                )
            )
            if record is None:
                record = CandidateExperience(
                    tenant_id=candidate.tenant_id,
                    candidate_id=candidate.id,
                    source_identity_id=identity.id,
                    position=position,
                    provider=identity.provider,
                    source_timestamp=source_timestamp,
                    observed_value_hash=value_hash,
                    confidence=confidence,
                    **payload,
                )
                self.session.add(record)
            elif record.observed_value_hash != value_hash and _is_newer_not_lower(
                source_timestamp,
                confidence,
                record.source_timestamp,
                record.confidence,
            ):
                for field_name, value in payload.items():
                    if value is not None:
                        setattr(record, field_name, value)
                record.source_timestamp = source_timestamp
                record.observed_value_hash = value_hash
                record.confidence = confidence

    def _suggest_duplicates(
        self,
        tenant_id: UUID,
        candidate_id: UUID,
        person: ProviderPerson,
        resolved_fuzzy_ids: tuple[UUID, ...],
    ) -> UUID | None:
        scores = dict(self.resolver.fuzzy_candidates(tenant_id, person))
        first_id: UUID | None = None
        for fuzzy_id in resolved_fuzzy_ids:
            if fuzzy_id == candidate_id:
                continue
            left, right = sorted((candidate_id, fuzzy_id), key=lambda value: value.int)
            suggestion = self.session.scalar(
                select(DuplicateSuggestion).where(
                    DuplicateSuggestion.tenant_id == tenant_id,
                    DuplicateSuggestion.candidate_id == left,
                    DuplicateSuggestion.suggested_candidate_id == right,
                )
            )
            if suggestion is None:
                suggestion = DuplicateSuggestion(
                    tenant_id=tenant_id,
                    candidate_id=left,
                    suggested_candidate_id=right,
                    similarity=scores.get(fuzzy_id, 0.0),
                    status="pending",
                )
                self.session.add(suggestion)
                self.session.flush()
            if first_id is None:
                first_id = suggestion.id
        return first_id


def _display_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_newer_not_lower(
    source_timestamp: datetime,
    confidence: float,
    existing_timestamp: datetime,
    existing_confidence: float,
) -> bool:
    return (
        _as_utc(source_timestamp) > _as_utc(existing_timestamp)
        and confidence >= existing_confidence
    )


def _advisory_key(value: str) -> int:
    return int.from_bytes(
        hashlib.blake2b(value.encode(), digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )
