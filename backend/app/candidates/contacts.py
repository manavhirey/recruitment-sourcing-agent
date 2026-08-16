import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.candidates.models import (
    Candidate,
    CandidateExperience,
    CandidateFieldProvenance,
    ContactPoint,
    DuplicateSuggestion,
    SourceIdentity,
)
from app.identity.schemas import RequestContext
from app.providers.base import ProviderContact

_SCHEMA_VERSION = 1
_CONTACT_RETENTION = timedelta(days=180)


@dataclass(frozen=True)
class ContactContext:
    tenant_id: UUID
    candidate_id: UUID
    contact_type: str

    def __post_init__(self) -> None:
        if self.contact_type not in {"email", "phone"}:
            raise ValueError("contact type must be email or phone")


@dataclass(frozen=True)
class EncryptedContact:
    ciphertext: bytes
    nonce: bytes
    encrypted_data_key: bytes
    key_nonce: bytes
    lookup_hmac: str
    schema_version: int = _SCHEMA_VERSION

    def with_ciphertext(self, value: bytes) -> "EncryptedContact":
        return replace(self, ciphertext=value)

    def with_encrypted_data_key(self, value: bytes) -> "EncryptedContact":
        return replace(self, encrypted_data_key=value)


class ContactCipher:
    """Envelope encryption for contact values; plaintext never leaves this boundary."""

    def __init__(self, key_encryption_key: str | bytes, lookup_key: bytes) -> None:
        encoded = (
            key_encryption_key.encode()
            if isinstance(key_encryption_key, str)
            else key_encryption_key
        )
        try:
            self._key_encryption_key = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("contact encryption key must be base64") from error
        if len(self._key_encryption_key) != 32:
            raise ValueError("contact encryption key must be 256-bit")
        if not lookup_key:
            raise ValueError("contact lookup key must not be empty")
        self._lookup_key = bytes(lookup_key)

    def encrypt(self, value: str, context: ContactContext) -> EncryptedContact:
        normalized = _normalize(value, context.contact_type)
        data_key = AESGCM.generate_key(bit_length=256)
        nonce = secrets.token_bytes(12)
        key_nonce = secrets.token_bytes(12)
        aad = _associated_data(context, _SCHEMA_VERSION)
        ciphertext = AESGCM(data_key).encrypt(nonce, normalized.encode(), aad)
        encrypted_data_key = AESGCM(self._key_encryption_key).encrypt(
            key_nonce, data_key, b"contact-data-key\0" + aad
        )
        return EncryptedContact(
            ciphertext=ciphertext,
            nonce=nonce,
            encrypted_data_key=encrypted_data_key,
            key_nonce=key_nonce,
            lookup_hmac=self.lookup_hmac(normalized, context),
        )

    def decrypt(self, value: EncryptedContact, context: ContactContext) -> str:
        aad = _associated_data(context, value.schema_version)
        data_key = AESGCM(self._key_encryption_key).decrypt(
            value.key_nonce,
            value.encrypted_data_key,
            b"contact-data-key\0" + aad,
        )
        plaintext = AESGCM(data_key).decrypt(value.nonce, value.ciphertext, aad)
        return plaintext.decode()

    def lookup_hmac(self, value: str, context: ContactContext) -> str:
        normalized = _normalize(value, context.contact_type)
        tenant_key = hmac.digest(
            self._lookup_key,
            b"contact-lookup-v1\0" + context.tenant_id.bytes,
            hashlib.sha256,
        )
        return hmac.digest(tenant_key, normalized.encode(), hashlib.sha256).hex()


@dataclass(frozen=True)
class ContactResolution:
    candidate_id: UUID
    contact_point: ContactPoint
    merged_candidate_id: UUID | None = None
    duplicate_suggestion_id: UUID | None = None
    accepted: bool = True


class CandidateMergeCoordinator(Protocol):
    def merge_candidate_memberships(
        self,
        tenant_id: UUID,
        source_candidate_id: UUID,
        target_candidate_id: UUID,
    ) -> None: ...


class ContactService:
    def __init__(
        self,
        session: Session,
        cipher: ContactCipher,
        *,
        merge_coordinator: CandidateMergeCoordinator | None = None,
    ) -> None:
        self.session = session
        self.cipher = cipher
        self.merge_coordinator = merge_coordinator

    def store(
        self,
        context: RequestContext,
        candidate_id: UUID,
        contact: ProviderContact,
        *,
        processed_at: datetime | None = None,
    ) -> ContactResolution:
        candidate = self._candidate(context.tenant_id, candidate_id)
        observed_at = _utc(contact.observed_at or datetime.now(UTC))
        processing_time = _utc(processed_at or datetime.now(UTC))
        contact_context = ContactContext(context.tenant_id, candidate.id, contact.kind)
        lookup_hmac = self.cipher.lookup_hmac(contact.value, contact_context)
        duplicate_suggestion_id: UUID | None = None
        merged_candidate_id: UUID | None = None
        if contact.kind == "email" and contact.verification_state == "verified":
            existing_match = self.session.scalar(
                select(ContactPoint)
                .where(
                    ContactPoint.tenant_id == context.tenant_id,
                    ContactPoint.kind == "email",
                    ContactPoint.lookup_hmac == lookup_hmac,
                    ContactPoint.verification_state == "verified",
                    ContactPoint.candidate_id != candidate.id,
                )
                .order_by(ContactPoint.candidate_id)
                .limit(1)
            )
            if existing_match is not None:
                existing_candidate = self._candidate(
                    context.tenant_id, existing_match.candidate_id
                )
                if self._merge_conflicts(candidate, existing_candidate):
                    suggestion = self._suggest_duplicate(
                        context.tenant_id, candidate.id, existing_candidate.id
                    )
                    duplicate_suggestion_id = suggestion.id
                else:
                    merged_candidate_id = candidate.id
                    candidate = self._merge_candidate(candidate, existing_candidate)
                    contact_context = ContactContext(
                        context.tenant_id, candidate.id, contact.kind
                    )

        encrypted = self.cipher.encrypt(contact.value, contact_context)
        point = self.session.scalar(
            select(ContactPoint).where(
                ContactPoint.tenant_id == context.tenant_id,
                ContactPoint.candidate_id == candidate.id,
                ContactPoint.kind == contact.kind,
                ContactPoint.lookup_hmac == encrypted.lookup_hmac,
            )
        )
        last_verified_at = (
            observed_at if contact.verification_state == "verified" else None
        )
        if point is not None and processing_time >= _utc(point.expires_at):
            _erase_contact(point, processing_time)
            self.session.flush()
            return ContactResolution(
                candidate_id=candidate.id,
                contact_point=point,
                merged_candidate_id=merged_candidate_id,
                duplicate_suggestion_id=duplicate_suggestion_id,
                accepted=False,
            )
        if point is None:
            point = ContactPoint(
                tenant_id=context.tenant_id,
                candidate_id=candidate.id,
                kind=contact.kind,
                classification=contact.classification,
                verification_state=contact.verification_state,
                confidence=contact.confidence,
                provider="apollo",
                lookup_hmac=encrypted.lookup_hmac,
                value_ciphertext=encrypted.ciphertext,
                value_nonce=encrypted.nonce,
                encrypted_data_key=encrypted.encrypted_data_key,
                key_nonce=encrypted.key_nonce,
                schema_version=encrypted.schema_version,
                observed_at=observed_at,
                last_verified_at=last_verified_at,
                expires_at=(last_verified_at or observed_at) + _CONTACT_RETENTION,
            )
            self.session.add(point)
        elif observed_at >= _utc(point.observed_at):
            point.classification = contact.classification
            point.verification_state = contact.verification_state
            point.confidence = contact.confidence
            point.provider = "apollo"
            point.value_ciphertext = encrypted.ciphertext
            point.value_nonce = encrypted.nonce
            point.encrypted_data_key = encrypted.encrypted_data_key
            point.key_nonce = encrypted.key_nonce
            point.schema_version = encrypted.schema_version
            point.observed_at = observed_at
            if last_verified_at is not None and (
                point.last_verified_at is None
                or last_verified_at > _utc(point.last_verified_at)
            ):
                point.last_verified_at = last_verified_at
                point.expires_at = (
                    max(
                        value
                        for value in (point.last_verified_at, point.last_used_at)
                        if value is not None
                    )
                    + _CONTACT_RETENTION
                )
        self.session.flush()
        return ContactResolution(
            candidate_id=candidate.id,
            contact_point=point,
            merged_candidate_id=merged_candidate_id,
            duplicate_suggestion_id=duplicate_suggestion_id,
        )

    def reveal(
        self,
        context: RequestContext,
        contact_point_id: UUID,
        *,
        used_at: datetime | None = None,
    ) -> str:
        point = self.session.scalar(
            select(ContactPoint)
            .where(
                ContactPoint.tenant_id == context.tenant_id,
                ContactPoint.id == contact_point_id,
            )
            .with_for_update()
        )
        if point is None:
            raise LookupError("contact point not found")
        use_time = _utc(used_at or datetime.now(UTC))
        if use_time >= _utc(point.expires_at):
            _erase_contact(point, use_time)
            self.session.flush()
            raise LookupError("contact point is expired")
        if any(
            value is None
            for value in (
                point.value_ciphertext,
                point.value_nonce,
                point.encrypted_data_key,
                point.key_nonce,
                point.lookup_hmac,
            )
        ):
            raise LookupError("contact point is expired")
        assert point.lookup_hmac is not None
        encrypted = EncryptedContact(
            ciphertext=point.value_ciphertext,  # type: ignore[arg-type]
            nonce=point.value_nonce,  # type: ignore[arg-type]
            encrypted_data_key=point.encrypted_data_key,  # type: ignore[arg-type]
            key_nonce=point.key_nonce,  # type: ignore[arg-type]
            lookup_hmac=point.lookup_hmac,
            schema_version=point.schema_version,
        )
        value = self.cipher.decrypt(
            encrypted,
            ContactContext(context.tenant_id, point.candidate_id, point.kind),
        )
        point.last_used_at = use_time
        point.expires_at = point.last_used_at + _CONTACT_RETENTION
        self.session.flush()
        return value

    def _candidate(self, tenant_id: UUID, candidate_id: UUID) -> Candidate:
        candidate = self.session.scalar(
            select(Candidate).where(
                Candidate.tenant_id == tenant_id, Candidate.id == candidate_id
            )
        )
        if candidate is None:
            raise LookupError("candidate not found")
        return candidate

    def _merge_conflicts(self, first: Candidate, second: Candidate) -> bool:
        if first.normalized_name != second.normalized_name:
            return True
        identities = self.session.scalars(
            select(SourceIdentity).where(
                SourceIdentity.tenant_id == first.tenant_id,
                SourceIdentity.candidate_id.in_((first.id, second.id)),
            )
        ).all()
        by_candidate: dict[UUID, dict[str, set[str]]] = {
            first.id: {},
            second.id: {},
        }
        for identity in identities:
            by_candidate[identity.candidate_id].setdefault(
                identity.provider, set()
            ).add(identity.provider_person_id)
        shared = set(by_candidate[first.id]) & set(by_candidate[second.id])
        return any(
            by_candidate[first.id][provider] != by_candidate[second.id][provider]
            for provider in shared
        )

    def _suggest_duplicate(
        self, tenant_id: UUID, candidate_id: UUID, suggested_id: UUID
    ) -> DuplicateSuggestion:
        suggestion = self.session.scalar(
            select(DuplicateSuggestion).where(
                DuplicateSuggestion.tenant_id == tenant_id,
                DuplicateSuggestion.candidate_id == candidate_id,
                DuplicateSuggestion.suggested_candidate_id == suggested_id,
            )
        )
        if suggestion is None:
            suggestion = DuplicateSuggestion(
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                suggested_candidate_id=suggested_id,
                similarity=1.0,
            )
            self.session.add(suggestion)
            self.session.flush()
        return suggestion

    def _merge_candidate(self, source: Candidate, target: Candidate) -> Candidate:
        self._move_contacts(source, target)
        if self.merge_coordinator is not None:
            self.merge_coordinator.merge_candidate_memberships(
                source.tenant_id,
                source.id,
                target.id,
            )
        identities = self.session.scalars(
            select(SourceIdentity).where(
                SourceIdentity.tenant_id == source.tenant_id,
                SourceIdentity.candidate_id == source.id,
            )
        ).all()
        for identity in identities:
            identity.candidate_id = target.id
        self.session.execute(
            update(CandidateFieldProvenance)
            .where(
                CandidateFieldProvenance.tenant_id == source.tenant_id,
                CandidateFieldProvenance.candidate_id == source.id,
            )
            .values(candidate_id=target.id)
        )
        self.session.execute(
            update(CandidateExperience)
            .where(
                CandidateExperience.tenant_id == source.tenant_id,
                CandidateExperience.candidate_id == source.id,
            )
            .values(candidate_id=target.id)
        )
        self.session.execute(
            delete(DuplicateSuggestion).where(
                DuplicateSuggestion.tenant_id == source.tenant_id,
                (
                    (DuplicateSuggestion.candidate_id == source.id)
                    | (DuplicateSuggestion.suggested_candidate_id == source.id)
                ),
            )
        )
        self.session.delete(source)
        self.session.flush()
        return target

    def _move_contacts(self, source: Candidate, target: Candidate) -> None:
        points = self.session.scalars(
            select(ContactPoint).where(
                ContactPoint.tenant_id == source.tenant_id,
                ContactPoint.candidate_id == source.id,
            )
        ).all()
        for point in points:
            collision = self.session.scalar(
                select(ContactPoint).where(
                    ContactPoint.tenant_id == source.tenant_id,
                    ContactPoint.candidate_id == target.id,
                    ContactPoint.kind == point.kind,
                    ContactPoint.lookup_hmac == point.lookup_hmac,
                    ContactPoint.lookup_hmac.is_not(None),
                )
            )
            if collision is not None:
                self.session.delete(point)
                continue
            if all(
                value is not None
                for value in (
                    point.value_ciphertext,
                    point.value_nonce,
                    point.encrypted_data_key,
                    point.key_nonce,
                    point.lookup_hmac,
                )
            ):
                assert point.lookup_hmac is not None
                plaintext = self.cipher.decrypt(
                    EncryptedContact(
                        ciphertext=point.value_ciphertext,  # type: ignore[arg-type]
                        nonce=point.value_nonce,  # type: ignore[arg-type]
                        encrypted_data_key=point.encrypted_data_key,  # type: ignore[arg-type]
                        key_nonce=point.key_nonce,  # type: ignore[arg-type]
                        lookup_hmac=point.lookup_hmac,
                        schema_version=point.schema_version,
                    ),
                    ContactContext(source.tenant_id, source.id, point.kind),
                )
                encrypted = self.cipher.encrypt(
                    plaintext,
                    ContactContext(target.tenant_id, target.id, point.kind),
                )
                point.value_ciphertext = encrypted.ciphertext
                point.value_nonce = encrypted.nonce
                point.encrypted_data_key = encrypted.encrypted_data_key
                point.key_nonce = encrypted.key_nonce
                point.lookup_hmac = encrypted.lookup_hmac
                point.schema_version = encrypted.schema_version
            point.candidate_id = target.id


def expire_due_contacts(
    session: Session,
    *,
    now: datetime | None = None,
) -> int:
    timestamp = _utc(now or datetime.now(UTC))
    points = session.scalars(
        select(ContactPoint)
        .where(
            ContactPoint.expires_at <= timestamp,
            ContactPoint.expired_at.is_(None),
        )
        .order_by(ContactPoint.expires_at, ContactPoint.id)
        .with_for_update()
    ).all()
    for point in points:
        _erase_contact(point, timestamp)
    session.flush()
    return len(points)


def _erase_contact(point: ContactPoint, timestamp: datetime) -> None:
    point.value_ciphertext = None
    point.value_nonce = None
    point.encrypted_data_key = None
    point.key_nonce = None
    point.lookup_hmac = None
    point.verification_state = "expired"
    point.expired_at = timestamp


def _associated_data(context: ContactContext, schema_version: int) -> bytes:
    return json.dumps(
        {
            "candidate_id": str(context.candidate_id),
            "contact_type": context.contact_type,
            "schema_version": schema_version,
            "tenant_id": str(context.tenant_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _normalize(value: str, contact_type: str) -> str:
    normalized = value.strip()
    if contact_type == "email":
        normalized = normalized.casefold()
        if not normalized or "@" not in normalized:
            raise ValueError("email contact is invalid")
        return normalized
    normalized = re.sub(r"[^0-9+]", "", normalized)
    if not normalized or normalized.count("+") > 1 or "+" in normalized[1:]:
        raise ValueError("phone contact is invalid")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def reveal_candidate_contact(
    session: Session,
    cipher: ContactCipher,
    context: RequestContext,
    run_candidate_id: UUID,
    contact_point_id: UUID,
    *,
    authorization_hmac_key: bytes,
) -> str:
    from app.audit.service import AuditService
    from app.jobs.service import JobError, JobService
    from app.sourcing.models import RunCandidate, SourcingRun

    row = session.scalar(
        select(RunCandidate).where(
            RunCandidate.id == run_candidate_id,
            RunCandidate.tenant_id == context.tenant_id,
        )
    )
    if row is None:
        raise LookupError("run candidate not found")
    run = session.scalar(
        select(SourcingRun).where(
            SourcingRun.id == row.run_id,
            SourcingRun.tenant_id == context.tenant_id,
        )
    )
    if run is None:
        raise LookupError("run candidate not found")
    try:
        JobService(session, authorization_hmac_key).get_authorized(context, run.job_id)
    except JobError as error:
        raise LookupError("run candidate not found") from error
    contact = session.scalar(
        select(ContactPoint.id).where(
            ContactPoint.id == contact_point_id,
            ContactPoint.tenant_id == context.tenant_id,
            ContactPoint.candidate_id == row.candidate_id,
        )
    )
    if contact is None:
        raise LookupError("contact point not found")
    value = ContactService(session, cipher).reveal(context, contact_point_id)
    AuditService(session).record(
        tenant_id=context.tenant_id,
        run_id=run.id,
        actor_user_id=context.user_id,
        event_key=f"contact-revealed:{contact_point_id}:{context.user_id}",
        action="candidate.contact_revealed",
        entity_type="contact_point",
        entity_id=contact_point_id,
        payload={"run_candidate_id": str(run_candidate_id)},
    )
    session.flush()
    return value
