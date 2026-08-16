from __future__ import annotations

import builtins
import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit.service import AuditService
from app.candidates.contacts import ContactCipher, ContactContext, EncryptedContact
from app.candidates.models import (
    Candidate,
    CandidateExperience,
    CandidateFieldProvenance,
    ContactPoint,
    DuplicateSuggestion,
    SourceIdentity,
)
from app.candidates.normalization import normalize_profile_url
from app.core.errors import AppError
from app.crm.models import CandidateNote, JobCandidate, JobCandidateTag
from app.identity.models import IdentityIdempotencyKey
from app.identity.schemas import RequestContext, Role
from app.identity.service import IdentityError, MembershipService
from app.jobs.models import Job
from app.privacy.models import (
    PrivacyDeletionSnapshotTarget,
    PrivacyRequest,
    PrivacyRequestCheckpoint,
    SuppressionIdentifier,
)
from app.privacy.schemas import PrivacyRequestState, PrivacyRequestType
from app.providers.base import EnrichedContactSet, ProviderPerson
from app.sourcing.models import (
    EnrichmentRequest,
    ProviderSnapshot,
    RunCandidate,
)

_DEFAULT_KEY_VERSION = "v1"
_DELETED_NAME = "[deleted]"


class PrivacyError(AppError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class IdentifierDigest:
    identifier_type: str
    digest: bytes


class SuppressionService:
    """Tenant- and purpose-scoped irreversible identifier suppression."""

    def __init__(
        self,
        session: Session | None,
        hmac_key: bytes,
        *,
        key_version: str = _DEFAULT_KEY_VERSION,
    ) -> None:
        if not hmac_key:
            raise ValueError("suppression HMAC key must not be empty")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", key_version):
            raise ValueError("suppression key version is invalid")
        self.session = session
        self._hmac_key = bytes(hmac_key)
        self.key_version = key_version

    def digest(self, tenant_id: UUID, identifier_type: str, value: str) -> str:
        return self.digest_bytes(tenant_id, identifier_type, value).hex()

    def digest_bytes(self, tenant_id: UUID, identifier_type: str, value: str) -> bytes:
        normalized_type = _normalize_identifier_type(identifier_type)
        normalized = _normalize_identifier(normalized_type, value)
        tenant_key = hmac.digest(
            self._hmac_key,
            b"candidate-suppression\0"
            + self.key_version.encode("ascii")
            + b"\0"
            + tenant_id.bytes,
            hashlib.sha256,
        )
        return hmac.digest(
            tenant_key,
            normalized_type.encode("utf-8") + b"\0" + normalized.encode("utf-8"),
            hashlib.sha256,
        )

    def identifiers_for_person(
        self, tenant_id: UUID, person: ProviderPerson
    ) -> tuple[IdentifierDigest, ...]:
        values: list[tuple[str, str]] = [
            (
                f"provider_id:{_normalize_provider(person.provider)}",
                person.provider_person_id,
            )
        ]
        if person.linkedin_url:
            values.append(("profile_url", person.linkedin_url))
        values.extend((contact.kind, contact.value) for contact in person.contacts)
        return self._digests(tenant_id, values)

    def identifiers_for_candidate(
        self,
        tenant_id: UUID,
        candidate: Candidate,
        identities: list[SourceIdentity],
        contacts: list[tuple[str, str]],
    ) -> tuple[IdentifierDigest, ...]:
        values: list[tuple[str, str]] = []
        if candidate.profile_url:
            values.append(("profile_url", candidate.profile_url))
        for identity in identities:
            values.append(
                (
                    f"provider_id:{_normalize_provider(identity.provider)}",
                    identity.provider_person_id,
                )
            )
            if identity.profile_url:
                values.append(("profile_url", identity.profile_url))
        values.extend(contacts)
        return self._digests(tenant_id, values)

    def identifiers_for_enrichment(
        self,
        tenant_id: UUID,
        provider: str,
        person: EnrichedContactSet,
    ) -> tuple[IdentifierDigest, ...]:
        values = [
            (
                f"provider_id:{_normalize_provider(provider)}",
                person.provider_person_id,
            )
        ]
        values.extend((contact.kind, contact.value) for contact in person.contacts)
        return self._digests(tenant_id, values)

    def match_person(
        self, tenant_id: UUID, person: ProviderPerson
    ) -> SuppressionIdentifier | None:
        return self.match_digests(
            tenant_id, self.identifiers_for_person(tenant_id, person)
        )

    def match_digests(
        self,
        tenant_id: UUID,
        digests: tuple[IdentifierDigest, ...],
    ) -> SuppressionIdentifier | None:
        session = self._require_session()
        self._lock(tenant_id, digests, exclusive_tenant=False)
        for item in digests:
            row = session.scalar(
                select(SuppressionIdentifier).where(
                    SuppressionIdentifier.tenant_id == tenant_id,
                    SuppressionIdentifier.identifier_type == item.identifier_type,
                    SuppressionIdentifier.key_version == self.key_version,
                    SuppressionIdentifier.digest == item.digest,
                )
            )
            if row is not None:
                return row
        return None

    def persist(
        self,
        tenant_id: UUID,
        privacy_request_id: UUID,
        digests: tuple[IdentifierDigest, ...],
    ) -> list[SuppressionIdentifier]:
        session = self._require_session()
        self._lock(tenant_id, digests, exclusive_tenant=True)
        rows: list[SuppressionIdentifier] = []
        for item in digests:
            row = session.scalar(
                select(SuppressionIdentifier).where(
                    SuppressionIdentifier.tenant_id == tenant_id,
                    SuppressionIdentifier.identifier_type == item.identifier_type,
                    SuppressionIdentifier.key_version == self.key_version,
                    SuppressionIdentifier.digest == item.digest,
                )
            )
            if row is None:
                row = SuppressionIdentifier(
                    tenant_id=tenant_id,
                    privacy_request_id=privacy_request_id,
                    identifier_type=item.identifier_type,
                    key_version=self.key_version,
                    digest=item.digest,
                )
                session.add(row)
                session.flush()
            rows.append(row)
        return rows

    def _digests(
        self, tenant_id: UUID, values: list[tuple[str, str]]
    ) -> tuple[IdentifierDigest, ...]:
        unique: dict[tuple[str, bytes], IdentifierDigest] = {}
        for identifier_type, value in values:
            try:
                identifier_type = _normalize_identifier_type(identifier_type)
                digest = self.digest_bytes(tenant_id, identifier_type, value)
            except ValueError:
                continue
            item = IdentifierDigest(identifier_type, digest)
            unique[(identifier_type, digest)] = item
        return tuple(
            unique[key] for key in sorted(unique, key=lambda item: (item[0], item[1]))
        )

    def _lock(
        self,
        tenant_id: UUID,
        digests: tuple[IdentifierDigest, ...],
        *,
        exclusive_tenant: bool,
    ) -> None:
        session = self._require_session()
        if session.get_bind().dialect.name != "postgresql":
            return
        tenant_lock_id = int.from_bytes(
            hashlib.sha256(b"suppression-tenant-gate-v1\0" + tenant_id.bytes).digest()[
                :8
            ],
            "big",
            signed=True,
        )
        lock_function = (
            "pg_advisory_xact_lock"
            if exclusive_tenant
            else "pg_advisory_xact_lock_shared"
        )
        session.execute(
            text(f"SELECT {lock_function}(:lock_id)"),
            {"lock_id": tenant_lock_id},
        )
        lock_ids = {
            int.from_bytes(
                hashlib.sha256(
                    b"suppression-barrier\0"
                    + tenant_id.bytes
                    + b"\0"
                    + self.key_version.encode("ascii")
                    + b"\0"
                    + item.identifier_type.encode("utf-8")
                    + b"\0"
                    + item.digest
                ).digest()[:8],
                "big",
                signed=True,
            )
            for item in digests
        }
        for lock_id in sorted(lock_ids):
            session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": lock_id},
            )

    def _require_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("suppression persistence requires a database session")
        return self.session


class PrivacyService:
    def __init__(
        self,
        session: Session,
        hmac_key: bytes,
        contact_cipher: ContactCipher,
        *,
        key_version: str = _DEFAULT_KEY_VERSION,
        idempotency_hmac_key: bytes | None = None,
    ) -> None:
        self.session = session
        self._hmac_key = bytes(hmac_key)
        self._idempotency = MembershipService(session, idempotency_hmac_key or hmac_key)
        self._audit = AuditService(session)
        self._suppression = SuppressionService(
            session, hmac_key, key_version=key_version
        )
        self._contact_cipher = contact_cipher

    def submit(
        self,
        context: RequestContext,
        *,
        candidate_id: UUID,
        request_type: PrivacyRequestType,
        idempotency_key: str,
    ) -> PrivacyRequest:
        self._candidate_authorized(context, candidate_id)
        record = self._begin(
            context,
            "privacy_submit",
            idempotency_key,
            {"candidate_id": str(candidate_id), "request_type": request_type.value},
        )
        replay = self._request_from_record(context, record)
        if replay is not None:
            return replay
        active = self._active_request(context, candidate_id, request_type)
        if active is not None:
            self._complete(record, active)
            return active
        request = PrivacyRequest(
            tenant_id=context.tenant_id,
            candidate_id=candidate_id,
            request_type=request_type,
            state=PrivacyRequestState.IDENTITY_VERIFICATION_REQUIRED,
            submitted_by_user_id=context.user_id,
        )
        try:
            with self.session.begin_nested():
                self.session.add(request)
                self.session.flush()
        except IntegrityError:
            active = self._active_request(context, candidate_id, request_type)
            if active is None:
                raise
            self._complete(record, active)
            return active
        self._audit.record(
            tenant_id=context.tenant_id,
            actor_user_id=context.user_id,
            event_key=f"privacy-submitted:{request.id}",
            action="privacy.request_submitted",
            entity_type="privacy_request",
            entity_id=request.id,
            payload={"request_type": request.request_type.value},
        )
        self._complete(record, request)
        return request

    def _active_request(
        self,
        context: RequestContext,
        candidate_id: UUID,
        request_type: PrivacyRequestType,
    ) -> PrivacyRequest | None:
        return self.session.scalar(
            select(PrivacyRequest)
            .where(
                PrivacyRequest.tenant_id == context.tenant_id,
                PrivacyRequest.candidate_id == candidate_id,
                PrivacyRequest.request_type == request_type,
                PrivacyRequest.state.not_in(
                    (
                        PrivacyRequestState.COMPLETED,
                        PrivacyRequestState.REJECTED,
                    )
                ),
            )
            .order_by(PrivacyRequest.created_at, PrivacyRequest.id)
            .limit(1)
        )

    def list(self, context: RequestContext) -> list[PrivacyRequest]:
        statement = select(PrivacyRequest).where(
            PrivacyRequest.tenant_id == context.tenant_id
        )
        if context.role is Role.RECRUITER:
            statement = statement.where(
                PrivacyRequest.submitted_by_user_id == context.user_id
            )
        requests = list(
            self.session.scalars(
                statement.order_by(PrivacyRequest.created_at.desc(), PrivacyRequest.id)
            )
        )
        if context.role is not Role.RECRUITER:
            return requests
        visible: list[PrivacyRequest] = []
        for request in requests:
            try:
                self._candidate_authorized(context, request.candidate_id)
            except PrivacyError:
                continue
            visible.append(request)
        return visible

    def get(self, context: RequestContext, request_id: UUID) -> PrivacyRequest:
        request = self.session.scalar(
            select(PrivacyRequest).where(
                PrivacyRequest.tenant_id == context.tenant_id,
                PrivacyRequest.id == request_id,
            )
        )
        if request is None:
            raise PrivacyError("privacy_request_not_found")
        if context.role is Role.RECRUITER:
            if request.submitted_by_user_id != context.user_id:
                raise PrivacyError("privacy_request_not_found")
            try:
                self._candidate_authorized(context, request.candidate_id)
            except PrivacyError:
                raise PrivacyError("privacy_request_not_found") from None
        return request

    def verify(
        self,
        context: RequestContext,
        request_id: UUID,
        *,
        idempotency_key: str,
    ) -> PrivacyRequest:
        self._require_manager(context)
        request = self._get_for_update(context, request_id)
        record = self._begin(
            context,
            f"privacy_verify:{request.id}",
            idempotency_key,
            {},
        )
        if record.response_payload is not None:
            return request
        if request.state is not PrivacyRequestState.IDENTITY_VERIFICATION_REQUIRED:
            raise PrivacyError("privacy_request_state_invalid")
        now = datetime.now(UTC)
        request.identity_verified_by_user_id = context.user_id
        request.identity_verified_at = now
        request.state = PrivacyRequestState.RECEIVED
        self._record_action(context, request, "identity_verified")
        self._complete(record, request)
        return request

    def approve(
        self,
        context: RequestContext,
        request_id: UUID,
        *,
        idempotency_key: str,
    ) -> PrivacyRequest:
        self._require_manager(context)
        request = self._get_for_update(context, request_id)
        record = self._begin(
            context,
            f"privacy_approve:{request.id}",
            idempotency_key,
            {},
        )
        if record.response_payload is not None:
            return request
        if (
            request.state is not PrivacyRequestState.RECEIVED
            or request.identity_verified_at is None
        ):
            raise PrivacyError("privacy_request_state_invalid")
        now = datetime.now(UTC)
        request.approved_by_user_id = context.user_id
        request.approved_at = now
        request.state = PrivacyRequestState.APPROVED
        self._record_action(context, request, "approved")
        self._complete(record, request)
        return request

    def reject(
        self,
        context: RequestContext,
        request_id: UUID,
        reason_code: str,
        *,
        idempotency_key: str,
    ) -> PrivacyRequest:
        self._require_manager(context)
        request = self._get_for_update(context, request_id)
        record = self._begin(
            context,
            f"privacy_reject:{request.id}",
            idempotency_key,
            {"reason_code": reason_code},
        )
        if record.response_payload is not None:
            return request
        if request.state in (
            PrivacyRequestState.EXECUTING,
            PrivacyRequestState.COMPLETED,
            PrivacyRequestState.REJECTED,
        ):
            raise PrivacyError("privacy_request_state_invalid")
        request.state = PrivacyRequestState.REJECTED
        request.rejected_by_user_id = context.user_id
        request.rejected_at = datetime.now(UTC)
        request.rejection_reason_code = reason_code
        self._record_action(context, request, "rejected")
        self._complete(record, request)
        return request

    def execute(
        self,
        context: RequestContext,
        request_id: UUID,
        *,
        idempotency_key: str,
    ) -> PrivacyRequest:
        request = self.get(context, request_id)
        if request.request_type in (
            PrivacyRequestType.DELETION,
            PrivacyRequestType.OPT_OUT,
        ):
            return self.execute_delete(
                context, request_id, idempotency_key=idempotency_key
            )
        self._require_manager(context)
        request = self._get_for_update(context, request_id)
        record = self._begin(
            context,
            f"privacy_execute:{request.id}",
            idempotency_key,
            {},
        )
        if record.response_payload is not None:
            return request
        if request.state is not PrivacyRequestState.APPROVED:
            raise PrivacyError("privacy_request_state_invalid")
        request.state = PrivacyRequestState.MANUAL_FULFILLMENT_REQUIRED
        self._checkpoint(request, "manual_fulfillment_required")
        self._record_action(context, request, "manual_fulfillment_required")
        self._complete(record, request)
        return request

    def execute_delete(
        self,
        context: RequestContext,
        request_id: UUID,
        *,
        idempotency_key: str,
    ) -> PrivacyRequest:
        self._require_manager(context)
        request = self._get_for_update(context, request_id)
        record = self._begin(
            context,
            f"privacy_execute_delete:{request.id}",
            idempotency_key,
            {},
        )
        if request.request_type not in (
            PrivacyRequestType.DELETION,
            PrivacyRequestType.OPT_OUT,
        ):
            raise PrivacyError("privacy_request_type_invalid")
        if request.state is PrivacyRequestState.COMPLETED:
            if record.response_payload is None:
                self._complete(record, request)
            return request
        if request.state not in (
            PrivacyRequestState.APPROVED,
            PrivacyRequestState.EXECUTING,
        ):
            raise PrivacyError("privacy_request_state_invalid")
        request.state = PrivacyRequestState.EXECUTING
        self._prepare_deletion(request, context)
        # The suppression barrier and snapshot work list must be durable before
        # a maintenance worker can observe or act on either one.
        self.session.commit()
        self._apply_tenant_context(context.tenant_id)

        request = self._get_for_update(context, request_id)
        pending = int(
            self.session.scalar(
                select(func.count())
                .select_from(PrivacyDeletionSnapshotTarget)
                .where(
                    PrivacyDeletionSnapshotTarget.tenant_id == context.tenant_id,
                    PrivacyDeletionSnapshotTarget.privacy_request_id == request.id,
                    PrivacyDeletionSnapshotTarget.status != "deleted",
                )
            )
            or 0
        )
        if pending == 0:
            self._finalize_deletion(request, context)
        refreshed_record = self.session.get(IdentityIdempotencyKey, record.id)
        if refreshed_record is not None:
            self._complete(refreshed_record, request)
        self.session.commit()
        return request

    def _apply_tenant_context(self, tenant_id: UUID) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )

    def _prepare_deletion(
        self, request: PrivacyRequest, context: RequestContext
    ) -> None:
        candidate = self.session.scalar(
            select(Candidate).where(
                Candidate.tenant_id == request.tenant_id,
                Candidate.id == request.candidate_id,
            )
        )
        if candidate is None:
            raise PrivacyError("privacy_request_not_found")
        digests = self._deletion_identifiers(request, candidate)
        self._checkpoint(request, "identifiers_collected")
        # The exclusive tenant gate follows the same lock order as ingestion's
        # shared gate, so deletion never holds a candidate row while waiting on
        # an ingestion suppression lock.
        self._suppression.persist(request.tenant_id, request.id, digests)
        candidate = self.session.scalar(
            select(Candidate)
            .where(
                Candidate.tenant_id == request.tenant_id,
                Candidate.id == request.candidate_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if candidate is None:
            raise PrivacyError("privacy_request_not_found")
        digests = self._deletion_identifiers(request, candidate)
        self._suppression.persist(request.tenant_id, request.id, digests)
        self._checkpoint(request, "suppression_barrier")
        self._collect_snapshot_targets(request)
        self._checkpoint(request, "snapshot_targets_collected")
        self._redact_personal_data(request)
        self._audit.record(
            tenant_id=request.tenant_id,
            actor_user_id=context.user_id,
            event_key=f"privacy-deletion-started:{request.id}",
            action="privacy.deletion_started",
            entity_type="privacy_request",
            entity_id=request.id,
            payload={"identifier_count": len(digests)},
        )
        self.session.flush()

    def _deletion_identifiers(
        self, request: PrivacyRequest, candidate: Candidate
    ) -> tuple[IdentifierDigest, ...]:
        identities = list(
            self.session.scalars(
                select(SourceIdentity).where(
                    SourceIdentity.tenant_id == request.tenant_id,
                    SourceIdentity.candidate_id == request.candidate_id,
                )
            )
        )
        contact_values = self._contact_values(request.tenant_id, request.candidate_id)
        return self._suppression.identifiers_for_candidate(
            request.tenant_id,
            candidate,
            identities,
            contact_values,
        )

    def _contact_values(
        self, tenant_id: UUID, candidate_id: UUID
    ) -> builtins.list[tuple[str, str]]:
        values: list[tuple[str, str]] = []
        points = self.session.scalars(
            select(ContactPoint).where(
                ContactPoint.tenant_id == tenant_id,
                ContactPoint.candidate_id == candidate_id,
            )
        ).all()
        for point in points:
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
                continue
            assert point.lookup_hmac is not None
            plaintext = self._contact_cipher.decrypt(
                EncryptedContact(
                    ciphertext=point.value_ciphertext,  # type: ignore[arg-type]
                    nonce=point.value_nonce,  # type: ignore[arg-type]
                    encrypted_data_key=point.encrypted_data_key,  # type: ignore[arg-type]
                    key_nonce=point.key_nonce,  # type: ignore[arg-type]
                    lookup_hmac=point.lookup_hmac,
                    schema_version=point.schema_version,
                ),
                ContactContext(tenant_id, candidate_id, point.kind),
            )
            values.append((point.kind, plaintext))
        return values

    def _collect_snapshot_targets(self, request: PrivacyRequest) -> None:
        enrichment_requests = self.session.scalars(
            select(EnrichmentRequest).where(
                EnrichmentRequest.tenant_id == request.tenant_id
            )
        ).all()
        request_ids = {
            item.id
            for item in enrichment_requests
            if str(request.candidate_id) in item.candidate_ids
        }
        if not request_ids:
            return
        snapshots = self.session.scalars(
            select(ProviderSnapshot).where(
                ProviderSnapshot.tenant_id == request.tenant_id,
                ProviderSnapshot.enrichment_request_id.in_(request_ids),
            )
        ).all()
        for snapshot in snapshots:
            existing = self.session.scalar(
                select(PrivacyDeletionSnapshotTarget).where(
                    PrivacyDeletionSnapshotTarget.tenant_id == request.tenant_id,
                    PrivacyDeletionSnapshotTarget.privacy_request_id == request.id,
                    PrivacyDeletionSnapshotTarget.snapshot_id == snapshot.id,
                )
            )
            if existing is None:
                self.session.add(
                    PrivacyDeletionSnapshotTarget(
                        tenant_id=request.tenant_id,
                        privacy_request_id=request.id,
                        snapshot_id=snapshot.id,
                    )
                )
        self.session.flush()

    def _finalize_deletion(
        self, request: PrivacyRequest, context: RequestContext
    ) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            self.session.scalar(
                text("SELECT privacy_finalize_deletion(:request_id, :tenant_id)"),
                {"request_id": request.id, "tenant_id": request.tenant_id},
            )
            self.session.expire_all()
            refreshed = self.get(context, request.id)
            request.state = refreshed.state
            request.completed_at = refreshed.completed_at
            return
        self._finalize_deletion_portable(request, context)

    def _redact_personal_data(self, request: PrivacyRequest) -> None:
        candidate_id = request.candidate_id
        tenant_id = request.tenant_id
        job_candidate_ids = select(JobCandidate.id).where(
            JobCandidate.tenant_id == tenant_id,
            JobCandidate.candidate_id == candidate_id,
        )
        self.session.execute(
            delete(CandidateNote).where(
                CandidateNote.tenant_id == tenant_id,
                CandidateNote.job_candidate_id.in_(job_candidate_ids),
            )
        )
        self.session.execute(
            delete(JobCandidateTag).where(
                JobCandidateTag.tenant_id == tenant_id,
                JobCandidateTag.job_candidate_id.in_(job_candidate_ids),
            )
        )
        self.session.execute(
            update(JobCandidate)
            .where(
                JobCandidate.tenant_id == tenant_id,
                JobCandidate.candidate_id == candidate_id,
            )
            .values(score=0, score_json={}, rejection_note=None)
        )
        self.session.execute(
            update(RunCandidate)
            .where(
                RunCandidate.tenant_id == tenant_id,
                RunCandidate.candidate_id == candidate_id,
            )
            .values(
                match_score=None,
                classification=None,
                evidence=None,
                scoring_version=None,
            )
        )
        for model in (
            ContactPoint,
            CandidateExperience,
            CandidateFieldProvenance,
            SourceIdentity,
        ):
            self.session.execute(
                delete(model).where(
                    model.tenant_id == tenant_id,
                    model.candidate_id == candidate_id,
                )
            )
        self.session.execute(
            delete(DuplicateSuggestion).where(
                DuplicateSuggestion.tenant_id == tenant_id,
                (
                    (DuplicateSuggestion.candidate_id == candidate_id)
                    | (DuplicateSuggestion.suggested_candidate_id == candidate_id)
                ),
            )
        )
        candidate = self.session.scalar(
            select(Candidate).where(
                Candidate.tenant_id == tenant_id,
                Candidate.id == candidate_id,
            )
        )
        if candidate is not None:
            candidate.full_name = _DELETED_NAME
            candidate.normalized_name = f"deleted-{candidate.id}"
            candidate.current_title = None
            candidate.normalized_title = None
            candidate.current_company = None
            candidate.normalized_company = None
            candidate.location = None
            candidate.normalized_location = None
            candidate.normalized_skills = []
            candidate.industry_codes = []
            candidate.profile_url = None
            candidate.normalized_profile_url = None
        self._checkpoint(request, "personal_data_redacted")
        self.session.flush()

    def _finalize_deletion_portable(
        self, request: PrivacyRequest, context: RequestContext
    ) -> None:
        if request.state is PrivacyRequestState.COMPLETED:
            return
        self._redact_personal_data(request)
        request.state = PrivacyRequestState.COMPLETED
        request.completed_at = datetime.now(UTC)
        self._checkpoint(request, "completed")
        self._audit.record(
            tenant_id=request.tenant_id,
            actor_user_id=context.user_id,
            event_key=f"privacy-deletion-completed:{request.id}",
            action="privacy.deletion_completed",
            entity_type="privacy_request",
            entity_id=request.id,
            payload={},
        )
        self.session.flush()

    def _checkpoint(self, request: PrivacyRequest, name: str) -> None:
        checkpoint = self.session.scalar(
            select(PrivacyRequestCheckpoint).where(
                PrivacyRequestCheckpoint.tenant_id == request.tenant_id,
                PrivacyRequestCheckpoint.privacy_request_id == request.id,
                PrivacyRequestCheckpoint.name == name,
            )
        )
        if checkpoint is None:
            checkpoint = PrivacyRequestCheckpoint(
                tenant_id=request.tenant_id,
                privacy_request_id=request.id,
                name=name,
                attempt_count=0,
            )
            self.session.add(checkpoint)
        if checkpoint.status != "completed":
            checkpoint.status = "completed"
            checkpoint.attempt_count += 1
            checkpoint.last_error_code = None
            checkpoint.completed_at = datetime.now(UTC)
        self.session.flush()

    def _candidate_authorized(
        self, context: RequestContext, candidate_id: UUID
    ) -> Candidate:
        candidate = self.session.scalar(
            select(Candidate).where(
                Candidate.tenant_id == context.tenant_id,
                Candidate.id == candidate_id,
            )
        )
        if candidate is None:
            raise PrivacyError("candidate_not_found")
        if context.role is not Role.RECRUITER:
            return candidate
        if not context.allowed_client_ids:
            raise PrivacyError("candidate_not_found")
        visible = self.session.scalar(
            select(JobCandidate.id)
            .join(
                Job,
                (Job.tenant_id == JobCandidate.tenant_id)
                & (Job.id == JobCandidate.job_id),
            )
            .where(
                JobCandidate.tenant_id == context.tenant_id,
                JobCandidate.candidate_id == candidate_id,
                Job.client_id.in_(context.allowed_client_ids),
            )
            .limit(1)
        )
        if visible is None:
            raise PrivacyError("candidate_not_found")
        return candidate

    def _get_for_update(
        self, context: RequestContext, request_id: UUID
    ) -> PrivacyRequest:
        request = self.session.scalar(
            select(PrivacyRequest)
            .where(
                PrivacyRequest.tenant_id == context.tenant_id,
                PrivacyRequest.id == request_id,
            )
            .with_for_update()
        )
        if request is None:
            raise PrivacyError("privacy_request_not_found")
        return request

    def _require_manager(self, context: RequestContext) -> None:
        if context.role not in (Role.OWNER, Role.ADMIN):
            raise PrivacyError("forbidden")

    def _record_action(
        self, context: RequestContext, request: PrivacyRequest, action: str
    ) -> None:
        self._audit.record(
            tenant_id=request.tenant_id,
            actor_user_id=context.user_id,
            event_key=f"privacy-{action}:{request.id}",
            action=f"privacy.request_{action}",
            entity_type="privacy_request",
            entity_id=request.id,
            payload={"state": request.state.value},
        )
        self.session.flush()

    def _begin(
        self,
        context: RequestContext,
        operation: str,
        idempotency_key: str,
        request_payload: dict[str, object],
    ) -> IdentityIdempotencyKey:
        try:
            return self._idempotency.begin_idempotent_mutation(
                tenant_id=context.tenant_id,
                actor_key=str(context.user_id),
                operation=operation,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
        except IdentityError as error:
            raise PrivacyError(error.code) from error

    def _complete(
        self, record: IdentityIdempotencyKey, request: PrivacyRequest
    ) -> None:
        self._idempotency.complete_idempotent_mutation(
            record,
            {"privacy_request_id": str(request.id)},
        )

    def _request_from_record(
        self, context: RequestContext, record: IdentityIdempotencyKey
    ) -> PrivacyRequest | None:
        if record.response_payload is None:
            return None
        value = record.response_payload.get("privacy_request_id")
        if not isinstance(value, str):
            raise PrivacyError("idempotency_result_missing")
        return self.get(context, UUID(value))


def _normalize_provider(value: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    if not normalized:
        raise ValueError("provider is invalid")
    return normalized


def _normalize_identifier(identifier_type: str, value: str) -> str:
    if identifier_type == "email":
        normalized = unicodedata.normalize("NFKC", value).strip().casefold()
        if not normalized or "@" not in normalized:
            raise ValueError("email identifier is invalid")
        return normalized
    if identifier_type == "phone":
        normalized = re.sub(r"[^0-9+]", "", unicodedata.normalize("NFKC", value))
        if not normalized or normalized.count("+") > 1 or "+" in normalized[1:]:
            raise ValueError("phone identifier is invalid")
        return normalized
    if identifier_type == "profile_url":
        normalized_url = normalize_profile_url(value)
        if normalized_url is None:
            raise ValueError("profile URL identifier is invalid")
        return normalized_url
    if identifier_type.startswith("provider_id:"):
        normalized = unicodedata.normalize("NFKC", value).strip()
        if not normalized:
            raise ValueError("provider identifier is invalid")
        return normalized
    raise ValueError("suppression identifier type is invalid")


def _normalize_identifier_type(identifier_type: str) -> str:
    normalized = unicodedata.normalize("NFKC", identifier_type).strip().casefold()
    if normalized in {"email", "phone", "profile_url"}:
        return normalized
    if normalized.startswith("provider_id:"):
        provider = _normalize_provider(normalized.removeprefix("provider_id:"))
        return f"provider_id:{provider}"
    raise ValueError("suppression identifier type is invalid")
