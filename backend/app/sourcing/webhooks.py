import hashlib
import hmac
import ipaddress
import json
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.audit.service import AuditService
from app.candidates.contacts import ContactCipher, ContactService
from app.candidates.models import SourceIdentity
from app.core.config import (
    Settings,
    get_enrichment_policy_settings,
)
from app.core.database import get_db
from app.identity.dependencies import get_app_settings
from app.identity.schemas import RequestContext, Role
from app.privacy.models import SuppressionIdentifier
from app.privacy.service import SuppressionService
from app.providers.apollo import normalize_enrichment_payload
from app.providers.base import EnrichmentResult, ProviderPayloadError
from app.providers.snapshots import SnapshotStore
from app.sourcing.models import (
    EnrichmentRequest,
    ProviderSnapshot,
    RunCandidate,
    SourcingRun,
    UsageLedger,
    WebhookDelivery,
)
from app.sourcing.service import SourcingService
from app.sourcing.state_machine import RunState, transition_run

router = APIRouter(tags=["provider-webhooks"])


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


class RedisCounter(Protocol):
    def eval(self, script: str, key_count: int, *keys_and_args: Any) -> Any: ...


class WebhookRateLimiter(Protocol):
    def allow(self, source: str, now: float) -> bool: ...


class RedisWebhookRateLimiter:
    _SCRIPT = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
    return count
    """

    def __init__(
        self,
        client: RedisCounter,
        limit: int = 120,
        window_seconds: int = 60,
    ) -> None:
        self.client = client
        self.limit = limit
        self.window_seconds = window_seconds

    def allow(self, source: str, now: float) -> bool:
        bucket = int(now // self.window_seconds)
        source_digest = hashlib.sha256(source.encode()).hexdigest()
        key = f"webhook-rate:apollo:{bucket}:{source_digest}"
        count = int(
            self.client.eval(
                self._SCRIPT,
                1,
                key,
                self.window_seconds * 2,
            )
        )
        return count <= self.limit


def resolve_webhook_source(
    *,
    peer: str,
    forwarded_for: str | None,
    trusted_proxies: frozenset[str],
) -> str:
    try:
        normalized_peer = str(ipaddress.ip_address(peer))
    except ValueError:
        return "unknown"
    if normalized_peer not in trusted_proxies or not forwarded_for:
        return normalized_peer
    chain: list[str] = []
    for value in forwarded_for.split(","):
        try:
            chain.append(str(ipaddress.ip_address(value.strip())))
        except ValueError:
            return normalized_peer
    chain.append(normalized_peer)
    index = len(chain) - 1
    while index > 0 and chain[index] in trusted_proxies:
        index -= 1
    return chain[index]


def _snapshot_store(request: Request) -> SnapshotStore:
    store = getattr(request.app.state, "snapshot_store", None)
    if store is None:
        import boto3  # type: ignore[import-untyped]

        settings: Settings = request.app.state.settings
        store = SnapshotStore(
            boto3.client(
                "s3",
                endpoint_url=settings.object_store_endpoint,
                aws_access_key_id=(
                    settings.object_store_writer_access_key_id.get_secret_value()
                ),
                aws_secret_access_key=(
                    settings.object_store_writer_secret_access_key.get_secret_value()
                ),
            ),
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
async def apollo_webhook(
    capability_token: str,
    request: Request,
    session: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> dict[str, str]:
    peer = request.client.host if request.client else "unknown"
    trusted = frozenset(
        value.strip()
        for value in settings.webhook_trusted_proxy_ips.split(",")
        if value.strip()
    )
    source = resolve_webhook_source(
        peer=peer,
        forwarded_for=request.headers.get("x-forwarded-for"),
        trusted_proxies=trusted,
    )
    allowed = await run_in_threadpool(
        _limiter(request).allow,
        source,
        datetime.now(UTC).timestamp(),
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "webhook_rate_limited"},
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = settings.webhook_max_body_bytes + 1
        if declared_length > settings.webhook_max_body_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"code": "webhook_payload_too_large"},
            )
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > settings.webhook_max_body_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"code": "webhook_payload_too_large"},
            )
        chunks.append(chunk)
    try:
        payload = json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "webhook_payload_invalid"},
        ) from None
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "webhook_payload_invalid"},
        )
    await run_in_threadpool(
        _process_webhook_delivery,
        session,
        capability_token,
        payload,
        request,
        settings,
        source,
    )
    return {"status": "accepted"}


def _process_webhook_delivery(
    session: Session,
    capability_token: str,
    payload: dict[str, object],
    request: Request,
    settings: Settings,
    source: str,
) -> None:
    codec = CapabilityTokenCodec(settings.webhook_hmac_key.get_secret_value().encode())
    try:
        apply_capability_payload(
            session,
            codec,
            capability_token,
            payload,
            snapshot_store=_snapshot_store(request),
            contact_cipher=_contact_cipher(request),
            suppression_service=SuppressionService(
                session,
                settings.suppression_hmac_key.get_secret_value().encode(),
                key_version=settings.suppression_hmac_key_version,
            ),
            source="webhook",
        )
        session.commit()
    except WebhookError as error:
        session.rollback()
        raise HTTPException(
            status_code=error.status_code, detail={"code": error.code}
        ) from error


def apply_capability_payload(
    session: Session,
    codec: CapabilityTokenCodec,
    token: str,
    payload: dict[str, object],
    *,
    snapshot_store: SnapshotStore,
    contact_cipher: ContactCipher,
    suppression_service: SuppressionService | None = None,
    source: str,
) -> None:
    tenant_id = codec.tenant_id(token)
    request_id = codec.request_id(token)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(select(func.set_config("app.tenant_id", str(tenant_id), True)))
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
        suppression_service=suppression_service,
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
    suppression_service: SuppressionService | None = None,
    source: str,
    terminal: bool = True,
) -> None:
    run = session.scalar(
        select(SourcingRun)
        .where(
            SourcingRun.tenant_id == enrichment.tenant_id,
            SourcingRun.id == enrichment.run_id,
        )
        .with_for_update()
    )
    if (
        enrichment.status in ("completed", "failed", "cancelled")
        or run is None
        or run.state is RunState.CANCELLED
        or run.cancellation_requested
    ):
        return
    if enrichment.provider_request_id is None:
        raise WebhookError("webhook_request_not_ready", 409)
    try:
        policy_settings = get_enrichment_policy_settings()
        result = normalize_enrichment_payload(
            payload,
            expected_request_id=enrichment.provider_request_id,
            contact_retention_days=policy_settings.apollo_contact_retention_days,
        )
    except (ProviderPayloadError, ValueError) as error:
        raise WebhookError("webhook_payload_invalid") from error
    allowed_candidates = {UUID(value) for value in enrichment.candidate_ids}
    identities = session.scalars(
        select(SourceIdentity).where(
            SourceIdentity.tenant_id == enrichment.tenant_id,
            SourceIdentity.provider == enrichment.provider,
            SourceIdentity.candidate_id.in_(allowed_candidates),
        )
    ).all()
    identity_by_provider_id = {
        identity.provider_person_id: identity for identity in identities
    }
    payload_hmac = codec.payload_digest(payload, enrichment.tenant_id)
    existing = session.scalar(
        select(WebhookDelivery).where(
            WebhookDelivery.tenant_id == enrichment.tenant_id,
            WebhookDelivery.enrichment_request_id == enrichment.id,
            WebhookDelivery.payload_hmac == payload_hmac,
        )
    )
    if existing is not None:
        if terminal:
            _terminalize_existing_delivery(session, enrichment, result, existing.source)
        return
    policy_settings = get_enrichment_policy_settings()
    suppression = suppression_service or SuppressionService(
        session,
        policy_settings.suppression_hmac_key.get_secret_value().encode(),
        key_version=policy_settings.suppression_hmac_key_version,
    )
    suppressed: list[tuple[object, SuppressionIdentifier]] = []
    allowed_people = []
    for person in result.people:
        digests = suppression.identifiers_for_enrichment(
            enrichment.tenant_id,
            enrichment.provider,
            person,
        )
        suppressed_by = suppression.match_digests(
            enrichment.tenant_id,
            digests,
        )
        if suppressed_by is None:
            allowed_people.append(person)
        else:
            if person.provider_person_id not in identity_by_provider_id:
                provider_type = f"provider_id:{enrichment.provider.casefold()}"
                provider_suppression = suppression.match_digests(
                    enrichment.tenant_id,
                    tuple(
                        item
                        for item in digests
                        if item.identifier_type == provider_type
                    ),
                )
                if provider_suppression is None:
                    raise WebhookError("webhook_payload_invalid")
                suppressed_by = provider_suppression
            suppressed.append((person, suppressed_by))
    if any(
        person.provider_person_id not in identity_by_provider_id
        for person in allowed_people
    ):
        raise WebhookError("webhook_payload_invalid")
    delivery = WebhookDelivery(
        tenant_id=enrichment.tenant_id,
        enrichment_request_id=enrichment.id,
        source=source,
        payload_hmac=payload_hmac,
    )
    session.add(delivery)
    session.flush()
    if not allowed_people and suppressed:
        _apply_suppressed_enrichment(
            session,
            enrichment,
            result,
            delivery,
            suppressed[0][1],
            source=source,
            terminal=terminal,
        )
        return
    allowed_provider_ids = {person.provider_person_id for person in allowed_people}
    snapshot = snapshot_store.put(
        tenant_id=enrichment.tenant_id,
        run_id=enrichment.run_id,
        provider=enrichment.provider,
        request_id=enrichment.provider_request_id,
        payload=_snapshot_payload_for_request(
            result.snapshot_payload,
            enrichment,
            allowed_provider_ids=allowed_provider_ids,
        ),
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

    contacts = ContactService(
        session,
        contact_cipher,
        merge_coordinator=SourcingService(session, b"internal-enrichment"),
    )
    found_candidates: set[UUID] = set()
    context = RequestContext(
        tenant_id=enrichment.tenant_id,
        user_id=_run_actor(session, enrichment),
        role=Role.OWNER,
    )
    for person in allowed_people:
        identity = identity_by_provider_id[person.provider_person_id]
        for contact in person.contacts:
            if contact.kind == "phone" and not enrichment.reveal_phone_number:
                continue
            if (
                contact.kind == "email"
                and contact.classification == "personal"
                and not enrichment.reveal_personal_emails
            ):
                continue
            resolution = contacts.store(context, identity.candidate_id, contact)
            if resolution.accepted:
                found_candidates.add(resolution.candidate_id)

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
        _reconcile_terminal_usage(session, enrichment, context, result, source)
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
    if suppressed:
        _record_suppressed_enrichment_audit(
            session,
            enrichment,
            delivery,
            [row for _person, row in suppressed],
        )
    session.flush()


def _record_suppressed_enrichment_audit(
    session: Session,
    enrichment: EnrichmentRequest,
    delivery: WebhookDelivery,
    suppressed_by: list[SuppressionIdentifier],
) -> None:
    first = suppressed_by[0]
    AuditService(session).record(
        tenant_id=enrichment.tenant_id,
        run_id=enrichment.run_id,
        actor_user_id=None,
        event_key=f"suppressed-enrichment:{enrichment.id}:{delivery.payload_hmac}",
        action="candidate.import_suppressed",
        entity_type="suppression_identifier",
        entity_id=first.id,
        payload={
            "identifier_types": sorted({row.identifier_type for row in suppressed_by}),
            "key_versions": sorted({row.key_version for row in suppressed_by}),
            "suppressed_count": len(suppressed_by),
        },
    )


def _apply_suppressed_enrichment(
    session: Session,
    enrichment: EnrichmentRequest,
    result: EnrichmentResult,
    delivery: WebhookDelivery,
    suppressed_by: SuppressionIdentifier,
    *,
    source: str,
    terminal: bool,
) -> None:
    context = RequestContext(
        tenant_id=enrichment.tenant_id,
        user_id=_run_actor(session, enrichment),
        role=Role.OWNER,
    )
    now = datetime.now(UTC)
    session.execute(
        update(RunCandidate)
        .where(
            RunCandidate.tenant_id == enrichment.tenant_id,
            RunCandidate.run_id == enrichment.run_id,
            RunCandidate.candidate_id.in_(
                [UUID(value) for value in enrichment.candidate_ids]
            ),
        )
        .values(enrichment_status="unavailable", enriched_at=now)
    )
    delivery.applied_at = now
    enrichment.status = "completed" if terminal else "pending"
    enrichment.completed_at = now if terminal else None
    if terminal:
        _reconcile_terminal_usage(session, enrichment, context, result, source)
        _finalize_run(session, enrichment)
    _record_suppressed_enrichment_audit(session, enrichment, delivery, [suppressed_by])
    session.flush()


def _snapshot_payload_for_request(
    payload: dict[str, object],
    enrichment: EnrichmentRequest,
    *,
    allowed_provider_ids: set[str] | None = None,
) -> dict[str, object]:
    denied_keys: set[str] = set()
    if not enrichment.reveal_personal_emails:
        denied_keys.update(("personal_email", "personal_emails"))
    if not enrichment.reveal_phone_number:
        denied_keys.update(
            ("mobile_phone", "mobile_phone_number", "phone_number", "phone_numbers")
        )

    def sanitize(value: object) -> object:
        if isinstance(value, dict):
            return {
                key: sanitize(item)
                for key, item in value.items()
                if key not in denied_keys
            }
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    sanitized = sanitize(payload)
    assert isinstance(sanitized, dict)
    if allowed_provider_ids is not None:
        for key in ("people", "matches"):
            raw_people = sanitized.get(key)
            if isinstance(raw_people, list):
                sanitized[key] = [
                    person
                    for person in raw_people
                    if _snapshot_person_id(person) in allowed_provider_ids
                ]
        raw_person = sanitized.get("person")
        if (
            raw_person is not None
            and _snapshot_person_id(raw_person) not in allowed_provider_ids
        ):
            sanitized.pop("person", None)
    return sanitized


def _snapshot_person_id(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    nested = value.get("person")
    if isinstance(nested, dict):
        value = nested
    identifier = value.get("id")
    return identifier if isinstance(identifier, str) else None


def _terminalize_existing_delivery(
    session: Session,
    enrichment: EnrichmentRequest,
    result: EnrichmentResult,
    source: str,
) -> None:
    context = RequestContext(
        tenant_id=enrichment.tenant_id,
        user_id=_run_actor(session, enrichment),
        role=Role.OWNER,
    )
    now = datetime.now(UTC)
    for row in session.scalars(
        select(RunCandidate).where(
            RunCandidate.tenant_id == enrichment.tenant_id,
            RunCandidate.run_id == enrichment.run_id,
            RunCandidate.candidate_id.in_(
                [UUID(value) for value in enrichment.candidate_ids]
            ),
        )
    ):
        if row.enrichment_status != "available":
            row.enrichment_status = "unavailable"
            row.enriched_at = now
    enrichment.status = "completed"
    enrichment.completed_at = now
    _reconcile_terminal_usage(session, enrichment, context, result, source)
    _finalize_run(session, enrichment)
    session.flush()


def _reconcile_terminal_usage(
    session: Session,
    enrichment: EnrichmentRequest,
    context: RequestContext,
    result: EnrichmentResult,
    source: str,
) -> None:
    if enrichment.usage_reconciled_at is not None:
        return
    ledgers = list(
        session.scalars(
            select(UsageLedger)
            .where(
                UsageLedger.tenant_id == enrichment.tenant_id,
                UsageLedger.run_id == enrichment.run_id,
                UsageLedger.reservation_key == enrichment.reservation_key,
            )
            .with_for_update()
        )
    )
    if not ledgers:
        enrichment.usage_reconciled_at = datetime.now(UTC)
        return
    requested = {row.unit_type: row.requested_units for row in ledgers}
    reported_credits = result.charged_credits
    if source == "synchronous":
        credits = (
            reported_credits
            if reported_credits is not None
            else enrichment.synchronous_credits
        )
    else:
        remaining = max(
            0,
            requested["estimated_credits"] - enrichment.synchronous_credits,
        )
        credits = enrichment.synchronous_credits + (
            reported_credits if reported_credits is not None else remaining
        )
    SourcingService(session, b"internal-enrichment").reconcile_usage(
        context,
        enrichment.run_id,
        reservation_key=enrichment.reservation_key,
        charged_units={
            "enrichments": requested["enrichments"],
            "estimated_credits": credits,
        },
        provider_request_id=enrichment.provider_request_id,
    )
    enrichment.usage_reconciled_at = datetime.now(UTC)


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
    if run.state is RunState.READY:
        from app.crm.service import capture_acceptance_cohort

        capture_acceptance_cohort(session, run)
