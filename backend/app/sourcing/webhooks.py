import hashlib
import hmac
import json
import secrets
import threading
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.service import AuditService
from app.candidates.contacts import ContactCipher, ContactService
from app.candidates.models import SourceIdentity
from app.core.config import Settings
from app.core.database import get_db
from app.identity.dependencies import get_app_settings
from app.identity.schemas import RequestContext, Role
from app.providers.apollo import normalize_enrichment_payload
from app.providers.base import ProviderPayloadError
from app.providers.snapshots import SnapshotStore
from app.sourcing.models import (
    EnrichmentRequest,
    ProviderSnapshot,
    RunCandidate,
    SourcingRun,
    WebhookDelivery,
)
from app.sourcing.state_machine import RunState, transition_run

router = APIRouter(tags=["provider-webhooks"])
_REQUEST_LOCKS: defaultdict[UUID, threading.Lock] = defaultdict(threading.Lock)


class WebhookError(ValueError):
    def __init__(self, code: str, status_code: int = 400) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class CapabilityTokenCodec:
    def __init__(self, master_key: bytes) -> None:
        if not master_key:
            raise ValueError("webhook HMAC key must not be empty")
        self._master_key = bytes(master_key)

    def issue(self, request_id: UUID, tenant_id: UUID) -> str:
        return f"{tenant_id}.{request_id}.{secrets.token_urlsafe(32)}"

    def tenant_id(self, token: str) -> UUID:
        tenant_id, separator, remainder = token.partition(".")
        if not separator or not remainder:
            raise WebhookError("webhook_not_found", status.HTTP_404_NOT_FOUND)
        return self._parse_uuid(tenant_id)

    def request_id(self, token: str) -> UUID:
        _tenant_id, separator, remainder = token.partition(".")
        request_id, request_separator, secret = remainder.partition(".")
        if not separator or not request_separator or not secret:
            raise WebhookError("webhook_not_found", status.HTTP_404_NOT_FOUND)
        return self._parse_uuid(request_id)

    @staticmethod
    def _parse_uuid(value: str) -> UUID:
        try:
            parsed = UUID(value)
        except ValueError as error:
            raise WebhookError(
                "webhook_not_found", status.HTTP_404_NOT_FOUND
            ) from error
        return parsed

    def digest(self, token: str, tenant_id: UUID) -> str:
        tenant_key = hmac.digest(
            self._master_key,
            b"webhook-capability-v1\0" + tenant_id.bytes,
            hashlib.sha256,
        )
        return hmac.digest(tenant_key, token.encode(), hashlib.sha256).hex()

    def payload_digest(self, payload: dict[str, object], tenant_id: UUID) -> str:
        canonical = json.dumps(
            payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode()
        tenant_key = hmac.digest(
            self._master_key,
            b"webhook-payload-v1\0" + tenant_id.bytes,
            hashlib.sha256,
        )
        return hmac.digest(tenant_key, canonical, hashlib.sha256).hex()


class WebhookRateLimiter:
    def __init__(self, limit: int = 120, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, source: str, now: float) -> bool:
        with self._lock:
            events = self._events[source]
            cutoff = now - self.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True


def _snapshot_store(request: Request) -> SnapshotStore:
    store = getattr(request.app.state, "snapshot_store", None)
    if store is None:
        import boto3  # type: ignore[import-untyped]

        settings: Settings = request.app.state.settings
        store = SnapshotStore(
            boto3.client("s3", endpoint_url=settings.object_store_endpoint),
            settings.object_store_bucket,
            settings.contact_encryption_key.get_secret_value(),
        )
        request.app.state.snapshot_store = store
    return store


def _contact_cipher(request: Request) -> ContactCipher:
    cipher = getattr(request.app.state, "contact_cipher", None)
    if cipher is None:
        settings: Settings = request.app.state.settings
        cipher = ContactCipher(
            settings.contact_encryption_key.get_secret_value(),
            settings.suppression_hmac_key.get_secret_value().encode(),
        )
        request.app.state.contact_cipher = cipher
    return cipher


def _limiter(request: Request) -> WebhookRateLimiter:
    return request.app.state.webhook_rate_limiter


@router.post(
    "/webhooks/apollo/{capability_token}",
    status_code=status.HTTP_202_ACCEPTED,
)
def apollo_webhook(
    capability_token: str,
    payload: dict[str, object],
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, str]:
    source = request.client.host if request.client else "unknown"
    limiter = _limiter(request)
    if not limiter.allow(source, datetime.now(UTC).timestamp()):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "webhook_rate_limited"},
        )
    codec = CapabilityTokenCodec(settings.webhook_hmac_key.get_secret_value().encode())
    try:
        apply_capability_payload(
            session,
            codec,
            capability_token,
            payload,
            snapshot_store=_snapshot_store(request),
            contact_cipher=_contact_cipher(request),
            source="webhook",
        )
        session.commit()
    except WebhookError as error:
        session.rollback()
        raise HTTPException(
            status_code=error.status_code, detail={"code": error.code}
        ) from error
    return {"status": "accepted"}


def apply_capability_payload(
    session: Session,
    codec: CapabilityTokenCodec,
    token: str,
    payload: dict[str, object],
    *,
    snapshot_store: SnapshotStore,
    contact_cipher: ContactCipher,
    source: str,
) -> None:
    tenant_id = codec.tenant_id(token)
    request_id = codec.request_id(token)
    with _REQUEST_LOCKS[request_id]:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                select(func.set_config("app.tenant_id", str(tenant_id), True))
            )
        enrichment = session.scalar(
            select(EnrichmentRequest)
            .where(
                EnrichmentRequest.id == request_id,
                EnrichmentRequest.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if enrichment is None or enrichment.capability_token_hmac is None:
            raise WebhookError("webhook_not_found", status.HTTP_404_NOT_FOUND)
        supplied = codec.digest(token, enrichment.tenant_id)
        if not hmac.compare_digest(supplied, enrichment.capability_token_hmac):
            raise WebhookError("webhook_not_found", status.HTTP_404_NOT_FOUND)
        apply_enrichment_payload(
            session,
            enrichment,
            payload,
            codec=codec,
            snapshot_store=snapshot_store,
            contact_cipher=contact_cipher,
            source=source,
        )


def apply_enrichment_payload(
    session: Session,
    enrichment: EnrichmentRequest,
    payload: dict[str, object],
    *,
    codec: CapabilityTokenCodec,
    snapshot_store: SnapshotStore,
    contact_cipher: ContactCipher,
    source: str,
    terminal: bool = True,
) -> None:
    if enrichment.provider_request_id is None:
        raise WebhookError("webhook_request_not_ready", 409)
    try:
        result = normalize_enrichment_payload(
            payload, expected_request_id=enrichment.provider_request_id
        )
    except (ProviderPayloadError, ValueError) as error:
        raise WebhookError("webhook_payload_invalid") from error
    payload_hmac = codec.payload_digest(payload, enrichment.tenant_id)
    existing = session.scalar(
        select(WebhookDelivery.id).where(
            WebhookDelivery.tenant_id == enrichment.tenant_id,
            WebhookDelivery.enrichment_request_id == enrichment.id,
            WebhookDelivery.payload_hmac == payload_hmac,
        )
    )
    if existing is not None or enrichment.status == "completed":
        return
    delivery = WebhookDelivery(
        tenant_id=enrichment.tenant_id,
        enrichment_request_id=enrichment.id,
        source=source,
        payload_hmac=payload_hmac,
    )
    session.add(delivery)
    session.flush()
    snapshot = snapshot_store.put(
        tenant_id=enrichment.tenant_id,
        run_id=enrichment.run_id,
        provider=enrichment.provider,
        request_id=enrichment.provider_request_id,
        payload=result.snapshot_payload,
    )
    reference = session.scalar(
        select(ProviderSnapshot).where(
            ProviderSnapshot.tenant_id == enrichment.tenant_id,
            ProviderSnapshot.enrichment_request_id == enrichment.id,
        )
    )
    if reference is None:
        reference = ProviderSnapshot(
            tenant_id=enrichment.tenant_id,
            run_id=enrichment.run_id,
            enrichment_request_id=enrichment.id,
            provider=enrichment.provider,
            object_reference=snapshot.reference,
            checksum_sha256=snapshot.checksum_sha256,
            created_at=snapshot.created_at,
            expires_at=snapshot.expires_at,
        )
        session.add(reference)
    else:
        reference.object_reference = snapshot.reference
        reference.checksum_sha256 = snapshot.checksum_sha256
        reference.created_at = snapshot.created_at
        reference.expires_at = snapshot.expires_at

    allowed_candidates = {UUID(value) for value in enrichment.candidate_ids}
    contacts = ContactService(session, contact_cipher)
    found_candidates: set[UUID] = set()
    context = RequestContext(
        tenant_id=enrichment.tenant_id,
        user_id=_run_actor(session, enrichment),
        role=Role.OWNER,
    )
    for person in result.people:
        identity = session.scalar(
            select(SourceIdentity).where(
                SourceIdentity.tenant_id == enrichment.tenant_id,
                SourceIdentity.provider == enrichment.provider,
                SourceIdentity.provider_person_id == person.provider_person_id,
            )
        )
        if identity is None or identity.candidate_id not in allowed_candidates:
            continue
        if person.contacts:
            found_candidates.add(identity.candidate_id)
        for contact in person.contacts:
            contacts.store(context, identity.candidate_id, contact)

    now = datetime.now(UTC)
    for row in session.scalars(
        select(RunCandidate).where(
            RunCandidate.tenant_id == enrichment.tenant_id,
            RunCandidate.run_id == enrichment.run_id,
            RunCandidate.candidate_id.in_(allowed_candidates),
        )
    ):
        if row.candidate_id in found_candidates:
            row.enrichment_status = "available"
            row.enriched_at = now
        elif terminal:
            row.enrichment_status = "unavailable"
            row.enriched_at = now
    delivery.applied_at = now
    enrichment.status = "completed" if terminal else "pending"
    enrichment.completed_at = now if terminal else None
    if terminal:
        _finalize_run(session, enrichment)
    AuditService(session).record(
        tenant_id=enrichment.tenant_id,
        run_id=enrichment.run_id,
        actor_user_id=None,
        event_key=f"enrichment-applied:{enrichment.id}:{payload_hmac}",
        action="sourcing_run.enrichment_applied",
        entity_type="enrichment_request",
        entity_id=enrichment.id,
        payload={"source": source, "candidate_count": len(found_candidates)},
    )
    session.flush()


def _run_actor(session: Session, enrichment: EnrichmentRequest) -> UUID:
    actor = session.scalar(
        select(SourcingRun.started_by_user_id).where(
            SourcingRun.tenant_id == enrichment.tenant_id,
            SourcingRun.id == enrichment.run_id,
        )
    )
    if actor is None:
        raise WebhookError("webhook_not_found", 404)
    return actor


def _finalize_run(session: Session, enrichment: EnrichmentRequest) -> None:
    remaining = int(
        session.scalar(
            select(func.count())
            .select_from(EnrichmentRequest)
            .where(
                EnrichmentRequest.tenant_id == enrichment.tenant_id,
                EnrichmentRequest.run_id == enrichment.run_id,
                EnrichmentRequest.status.in_(("queued", "submitting", "pending")),
            )
        )
        or 0
    )
    if remaining:
        return
    run = session.scalar(
        select(SourcingRun)
        .where(
            SourcingRun.tenant_id == enrichment.tenant_id,
            SourcingRun.id == enrichment.run_id,
        )
        .with_for_update()
    )
    if run is None or run.state is RunState.CANCELLED or run.cancellation_requested:
        return
    failed = int(
        session.scalar(
            select(func.count())
            .select_from(EnrichmentRequest)
            .where(
                EnrichmentRequest.tenant_id == enrichment.tenant_id,
                EnrichmentRequest.run_id == enrichment.run_id,
                EnrichmentRequest.status == "failed",
            )
        )
        or 0
    )
    target = RunState.PARTIALLY_READY if failed else RunState.READY
    if (
        run.state is RunState.ENRICHING
        or run.state is RunState.PARTIALLY_READY
        and target is RunState.READY
    ):
        run.state = transition_run(run.state, target)
    run.current_stage = run.state.value
    run.completed_at = datetime.now(UTC)
