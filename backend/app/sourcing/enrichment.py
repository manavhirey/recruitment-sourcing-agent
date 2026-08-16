from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.candidates.contacts import ContactCipher
from app.candidates.models import Candidate, SourceIdentity
from app.identity.schemas import RequestContext
from app.providers.base import (
    EnrichmentInput,
    EnrichmentPending,
    EnrichmentReceipt,
    EnrichmentResult,
    ProviderError,
)
from app.providers.snapshots import SnapshotStore
from app.sourcing.models import (
    EnrichmentRequest,
    ProviderSnapshot,
    RunCandidate,
    SourcingRun,
    UsageLedger,
)
from app.sourcing.service import SourcingError, SourcingService
from app.sourcing.state_machine import RunState, transition_run
from app.sourcing.webhooks import (
    CapabilityTokenCodec,
    apply_enrichment_payload,
)

_MAX_ENRICHMENT_LIMIT = 50
_MAX_BATCH_SIZE = 10
_STAGE_DEADLINE = timedelta(minutes=5)


class EnrichmentGateway(Protocol):
    def enrich_batch(
        self,
        people: tuple[EnrichmentInput, ...],
        webhook_url: str,
        *,
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
    ) -> EnrichmentReceipt: ...

    def poll_enrichment(
        self, request_id: str
    ) -> EnrichmentResult | EnrichmentPending: ...


@contextmanager
def _tenant_session(
    session_factory: sessionmaker[Session], tenant_id: UUID
) -> Iterator[Session]:
    with session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
        yield session


@dataclass(frozen=True)
class RegionalContactPolicy:
    provider_allows_personal_emails: bool
    provider_allows_phone_numbers: bool
    allowed_regions: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {"india", "in", "united states", "united states of america", "us", "usa"}
        )
    )

    def reveal_flags(self, location: str | None) -> tuple[bool, bool]:
        if not location:
            return False, False
        components = {
            component.strip().casefold()
            for component in location.replace("/", ",").split(",")
            if component.strip()
        }
        allowed = bool(components & self.allowed_regions)
        return (
            self.provider_allows_personal_emails and allowed,
            self.provider_allows_phone_numbers and allowed,
        )


@dataclass(frozen=True)
class SubmittedEnrichment:
    request_id: UUID
    provider_request_id: str
    candidate_ids: tuple[UUID, ...]
    capability_token: str = field(repr=False)


def stable_top_candidate_ids(
    candidates: Iterable[tuple[UUID, int | None, str | None]], *, limit: int = 50
) -> list[UUID]:
    if not 0 <= limit <= _MAX_ENRICHMENT_LIMIT:
        raise ValueError("enrichment limit must be between 0 and 50")
    eligible = [
        (candidate_id, score)
        for candidate_id, score, classification in candidates
        if classification == "main" and score is not None
    ]
    eligible.sort(key=lambda item: (-item[1], item[0]))
    return [candidate_id for candidate_id, _ in eligible[:limit]]


def batch_candidates(
    candidate_ids: Sequence[UUID], *, batch_size: int = _MAX_BATCH_SIZE
) -> list[list[UUID]]:
    if not 1 <= batch_size <= 10:
        raise ValueError("enrichment batch size must be between 1 and 10")
    return [
        list(candidate_ids[offset : offset + batch_size])
        for offset in range(0, len(candidate_ids), batch_size)
    ]


def enqueue_top_enrichment(
    run_id: UUID,
    limit: int = _MAX_ENRICHMENT_LIMIT,
    *,
    session_factory: sessionmaker[Session],
    context: RequestContext,
    gateway: EnrichmentGateway,
    callback_base_url: str,
    contact_cipher: ContactCipher,
    snapshot_store: SnapshotStore,
    policy: RegionalContactPolicy,
    token_codec: CapabilityTokenCodec,
) -> list[SubmittedEnrichment]:
    if not callback_base_url.startswith("https://"):
        raise ValueError("callback base URL must use HTTPS")
    codec = token_codec
    with _tenant_session(session_factory, context.tenant_id) as session:
        run = _load_run(session, context, run_id, for_update=True)
        if run.cancellation_requested or run.state is RunState.CANCELLED:
            session.rollback()
            return []
        if run.state not in (RunState.ENRICHING, RunState.PARTIALLY_READY):
            raise ValueError("sourcing run is not ready for enrichment")
        rows = session.execute(
            select(
                RunCandidate.candidate_id,
                RunCandidate.match_score,
                RunCandidate.classification,
            ).where(
                RunCandidate.tenant_id == context.tenant_id,
                RunCandidate.run_id == run_id,
            )
        ).all()
        selected_ids = stable_top_candidate_ids(
            [(row[0], row[1], row[2]) for row in rows], limit=limit
        )
        session.rollback()
    if not selected_ids:
        _finish_empty_enrichment(session_factory, context, run_id)
        return []

    candidates = _provider_candidates(session_factory, context, selected_ids)
    batches = _policy_batches(candidates, policy)
    prepared_batches: list[
        tuple[
            list[tuple[UUID, EnrichmentInput, str | None]],
            bool,
            bool,
            UUID,
            str,
        ]
    ] = []
    for index, (records, reveal_personal, reveal_phone) in enumerate(batches):
        candidate_ids = tuple(record[0] for record in records)
        reservation_key = _reservation_key(index, candidate_ids)
        prepared = _prepare_request(
            session_factory,
            context,
            run_id,
            candidate_ids,
            reservation_key,
            reveal_personal,
            reveal_phone,
            codec,
        )
        if prepared is None:
            continue
        enrichment_id, capability_token = prepared
        prepared_batches.append(
            (
                records,
                reveal_personal,
                reveal_phone,
                enrichment_id,
                capability_token,
            )
        )

    submissions: list[SubmittedEnrichment] = []
    for (
        records,
        reveal_personal,
        reveal_phone,
        enrichment_id,
        capability_token,
    ) in prepared_batches:
        if not _request_dispatchable(session_factory, context, enrichment_id):
            continue
        candidate_ids = tuple(record[0] for record in records)
        inputs = tuple(record[1] for record in records)
        callback_url = (
            f"{callback_base_url.rstrip('/')}/webhooks/apollo/{capability_token}"
        )
        try:
            receipt = gateway.enrich_batch(
                inputs,
                callback_url,
                reveal_personal_emails=reveal_personal,
                reveal_phone_number=reveal_phone,
            )
        except ProviderError:
            _fail_request(
                session_factory,
                context,
                enrichment_id,
                error_code="provider_enrichment_failed",
            )
            continue
        _record_receipt(
            session_factory,
            context,
            enrichment_id,
            receipt,
            codec,
            snapshot_store,
            contact_cipher,
            terminal=not reveal_phone,
        )
        submissions.append(
            SubmittedEnrichment(
                request_id=enrichment_id,
                provider_request_id=receipt.request_id,
                candidate_ids=candidate_ids,
                capability_token=capability_token,
            )
        )
    _finalize_if_terminal(session_factory, context, run_id)
    return submissions


def poll_enrichment_request(
    session_factory: sessionmaker[Session],
    request_id: UUID,
    context: RequestContext,
    *,
    gateway: EnrichmentGateway,
    token_codec: CapabilityTokenCodec,
    snapshot_store: SnapshotStore,
    contact_cipher: ContactCipher,
) -> int | None:
    with _tenant_session(session_factory, context.tenant_id) as session:
        enrichment = session.scalar(
            select(EnrichmentRequest).where(
                EnrichmentRequest.id == request_id,
                EnrichmentRequest.tenant_id == context.tenant_id,
            )
        )
        if enrichment is None:
            raise LookupError("enrichment request not found")
        if enrichment.status in ("completed", "failed", "cancelled"):
            return None
        provider_request_id = enrichment.provider_request_id
    if provider_request_id is None:
        _fail_request(
            session_factory,
            context,
            request_id,
            error_code="provider_request_missing",
        )
        return None
    try:
        result = gateway.poll_enrichment(provider_request_id)
    except ProviderError:
        _fail_request(
            session_factory,
            context,
            request_id,
            error_code="provider_poll_failed",
            charge_reserved=True,
        )
        _finalize_if_terminal(session_factory, context, enrichment.run_id)
        return None
    if isinstance(result, EnrichmentPending):
        with _tenant_session(session_factory, context.tenant_id) as session:
            enrichment = session.get(EnrichmentRequest, request_id)
            if enrichment is not None and enrichment.tenant_id == context.tenant_id:
                enrichment.retry_count += 1
                enrichment.poll_after = datetime.now(UTC) + timedelta(
                    seconds=result.retry_after_seconds
                )
                session.commit()
        return result.retry_after_seconds
    with _tenant_session(session_factory, context.tenant_id) as session:
        enrichment = session.scalar(
            select(EnrichmentRequest)
            .where(
                EnrichmentRequest.id == request_id,
                EnrichmentRequest.tenant_id == context.tenant_id,
            )
            .with_for_update()
        )
        if enrichment is None:
            raise LookupError("enrichment request not found")
        apply_enrichment_payload(
            session,
            enrichment,
            result.snapshot_payload,
            codec=token_codec,
            snapshot_store=snapshot_store,
            contact_cipher=contact_cipher,
            source="poll",
        )
        session.commit()
    return None


def execute_queued_enrichment_request(
    session_factory: sessionmaker[Session],
    request_id: UUID,
    context: RequestContext,
    *,
    gateway: EnrichmentGateway,
    callback_base_url: str,
    contact_cipher: ContactCipher,
    snapshot_store: SnapshotStore,
    policy: RegionalContactPolicy,
    token_codec: CapabilityTokenCodec,
) -> SubmittedEnrichment | None:
    if not callback_base_url.startswith("https://"):
        raise ValueError("callback base URL must use HTTPS")
    with _tenant_session(session_factory, context.tenant_id) as session:
        enrichment = session.scalar(
            select(EnrichmentRequest)
            .where(
                EnrichmentRequest.id == request_id,
                EnrichmentRequest.tenant_id == context.tenant_id,
            )
            .with_for_update()
        )
        if enrichment is None:
            raise LookupError("enrichment request not found")
        if enrichment.status in ("pending", "completed", "failed", "cancelled"):
            return None
        if enrichment.status == "submitting":
            enrichment.status = "failed"
            enrichment.error_code = "ambiguous_provider_submission"
            enrichment.completed_at = datetime.now(UTC)
            session.commit()
            _finalize_if_terminal(session_factory, context, enrichment.run_id)
            return None
        run = _load_run(session, context, enrichment.run_id, for_update=True)
        if run.cancellation_requested or run.state is RunState.CANCELLED:
            enrichment.status = "cancelled"
            enrichment.completed_at = datetime.now(UTC)
            session.commit()
            return None
        candidate_ids = tuple(UUID(value) for value in enrichment.candidate_ids)
        records = _provider_candidates(session_factory, context, candidate_ids)
        if not records:
            enrichment.status = "failed"
            enrichment.error_code = "provider_identity_missing"
            enrichment.completed_at = datetime.now(UTC)
            session.commit()
            _finalize_if_terminal(session_factory, context, enrichment.run_id)
            return None
        flags = [policy.reveal_flags(record[2]) for record in records]
        reveal_personal = all(value[0] for value in flags)
        reveal_phone = all(value[1] for value in flags)
        token = token_codec.issue(enrichment.id, context.tenant_id)
        enrichment.capability_token_hmac = token_codec.digest(token, context.tenant_id)
        enrichment.reveal_personal_emails = reveal_personal
        enrichment.reveal_phone_number = reveal_phone
        enrichment.status = "submitting"
        enrichment.stage_deadline = datetime.now(UTC) + _STAGE_DEADLINE
        session.commit()
        run_id = enrichment.run_id
    inputs = tuple(record[1] for record in records)
    callback_url = f"{callback_base_url.rstrip('/')}/webhooks/apollo/{token}"
    try:
        receipt = gateway.enrich_batch(
            inputs,
            callback_url,
            reveal_personal_emails=reveal_personal,
            reveal_phone_number=reveal_phone,
        )
    except ProviderError:
        _fail_request(
            session_factory,
            context,
            request_id,
            error_code="provider_enrichment_failed",
        )
        _finalize_if_terminal(session_factory, context, run_id)
        return None
    _record_receipt(
        session_factory,
        context,
        request_id,
        receipt,
        token_codec,
        snapshot_store,
        contact_cipher,
        terminal=not reveal_phone,
    )
    _finalize_if_terminal(session_factory, context, run_id)
    return SubmittedEnrichment(
        request_id=request_id,
        provider_request_id=receipt.request_id,
        candidate_ids=tuple(record[0] for record in records),
        capability_token=token,
    )


def reconcile_snapshot_references(
    session: Session,
    store: SnapshotStore,
    *,
    now: datetime | None = None,
) -> int:
    timestamp = now or datetime.now(UTC)
    references = list(
        session.scalars(
            select(ProviderSnapshot)
            .where(ProviderSnapshot.expires_at <= timestamp)
            .order_by(ProviderSnapshot.expires_at, ProviderSnapshot.id)
        )
    )
    removed = 0
    for reference in references:
        store.delete(reference.object_reference)
        session.delete(reference)
        removed += 1
    session.flush()
    return removed


def _provider_candidates(
    session_factory: sessionmaker[Session],
    context: RequestContext,
    candidate_ids: Sequence[UUID],
) -> list[tuple[UUID, EnrichmentInput, str | None]]:
    with _tenant_session(session_factory, context.tenant_id) as session:
        candidates = {
            candidate.id: candidate
            for candidate in session.scalars(
                select(Candidate).where(
                    Candidate.tenant_id == context.tenant_id,
                    Candidate.id.in_(candidate_ids),
                )
            )
        }
        identities = session.scalars(
            select(SourceIdentity)
            .where(
                SourceIdentity.tenant_id == context.tenant_id,
                SourceIdentity.candidate_id.in_(candidate_ids),
                SourceIdentity.provider == "apollo",
            )
            .order_by(SourceIdentity.candidate_id, SourceIdentity.id)
        ).all()
        identity_by_candidate: dict[UUID, SourceIdentity] = {}
        for identity in identities:
            identity_by_candidate.setdefault(identity.candidate_id, identity)
        return [
            (
                candidate_id,
                EnrichmentInput(
                    identity_by_candidate[candidate_id].provider_person_id,
                    identity_by_candidate[candidate_id].profile_url,
                ),
                candidates[candidate_id].location,
            )
            for candidate_id in candidate_ids
            if candidate_id in candidates and candidate_id in identity_by_candidate
        ]


def _policy_batches(
    records: Sequence[tuple[UUID, EnrichmentInput, str | None]],
    policy: RegionalContactPolicy,
) -> list[tuple[list[tuple[UUID, EnrichmentInput, str | None]], bool, bool]]:
    batches: list[
        tuple[list[tuple[UUID, EnrichmentInput, str | None]], bool, bool]
    ] = []
    current: list[tuple[UUID, EnrichmentInput, str | None]] = []
    current_flags: tuple[bool, bool] | None = None
    for record in records:
        flags = policy.reveal_flags(record[2])
        if current and (flags != current_flags or len(current) == _MAX_BATCH_SIZE):
            assert current_flags is not None
            batches.append((current, *current_flags))
            current = []
        current.append(record)
        current_flags = flags
    if current:
        assert current_flags is not None
        batches.append((current, *current_flags))
    return batches


def _reservation_key(index: int, candidate_ids: tuple[UUID, ...]) -> str:
    import hashlib

    digest = hashlib.sha256(
        "\0".join(str(value) for value in candidate_ids).encode()
    ).hexdigest()[:16]
    return f"auto-enrichment:{index}:{digest}"


def _prepare_request(
    session_factory: sessionmaker[Session],
    context: RequestContext,
    run_id: UUID,
    candidate_ids: tuple[UUID, ...],
    reservation_key: str,
    reveal_personal: bool,
    reveal_phone: bool,
    codec: CapabilityTokenCodec,
) -> tuple[UUID, str] | None:
    with _tenant_session(session_factory, context.tenant_id) as session:
        existing = session.scalar(
            select(EnrichmentRequest).where(
                EnrichmentRequest.tenant_id == context.tenant_id,
                EnrichmentRequest.run_id == run_id,
                EnrichmentRequest.reservation_key == reservation_key,
            )
        )
        if existing is not None:
            return None
        run = _load_run(session, context, run_id, for_update=True)
        if run.cancellation_requested or run.state is RunState.CANCELLED:
            session.rollback()
            return None
        request = EnrichmentRequest(
            tenant_id=context.tenant_id,
            run_id=run_id,
            provider="apollo",
            candidate_ids=[str(value) for value in candidate_ids],
            reservation_key=reservation_key,
            status="submitting",
            reveal_personal_emails=reveal_personal,
            reveal_phone_number=reveal_phone,
            stage_deadline=datetime.now(UTC) + _STAGE_DEADLINE,
        )
        session.add(request)
        session.flush()
        token = codec.issue(request.id, context.tenant_id)
        request.capability_token_hmac = codec.digest(token, context.tenant_id)
        service = SourcingService(session, b"internal-enrichment")
        try:
            service.reserve_usage(
                context,
                run_id,
                provider="apollo",
                endpoint="people_bulk_match",
                reservation_key=reservation_key,
                requested_units={
                    "enrichments": len(candidate_ids),
                    "estimated_credits": len(candidate_ids) * 9,
                },
            )
        except SourcingError:
            request.status = "failed"
            request.error_code = "usage_budget_exhausted"
            request.completed_at = datetime.now(UTC)
            session.commit()
            return None
        session.execute(
            update(RunCandidate)
            .where(
                RunCandidate.tenant_id == context.tenant_id,
                RunCandidate.run_id == run_id,
                RunCandidate.candidate_id.in_(candidate_ids),
            )
            .values(enrichment_status="pending")
        )
        session.commit()
        return request.id, token


def _record_receipt(
    session_factory: sessionmaker[Session],
    context: RequestContext,
    request_id: UUID,
    receipt: EnrichmentReceipt,
    codec: CapabilityTokenCodec,
    snapshot_store: SnapshotStore,
    contact_cipher: ContactCipher,
    *,
    terminal: bool,
) -> None:
    with _tenant_session(session_factory, context.tenant_id) as session:
        enrichment = session.scalar(
            select(EnrichmentRequest)
            .where(
                EnrichmentRequest.id == request_id,
                EnrichmentRequest.tenant_id == context.tenant_id,
            )
            .with_for_update()
        )
        if enrichment is None:
            raise LookupError("enrichment request not found")
        if enrichment.status in ("completed", "failed", "cancelled"):
            return
        run = _load_run(session, context, enrichment.run_id, for_update=True)
        if run.cancellation_requested or run.state is RunState.CANCELLED:
            enrichment.provider_request_id = receipt.request_id
            enrichment.status = "cancelled"
            enrichment.completed_at = datetime.now(UTC)
            if enrichment.usage_reconciled_at is None:
                reserved = {
                    row.unit_type: row.requested_units
                    for row in session.scalars(
                        select(UsageLedger).where(
                            UsageLedger.tenant_id == context.tenant_id,
                            UsageLedger.run_id == enrichment.run_id,
                            UsageLedger.reservation_key == enrichment.reservation_key,
                        )
                    )
                }
                _reconcile_receipt_usage(session, enrichment, context, reserved)
            session.commit()
            return
        enrichment.provider_request_id = receipt.request_id
        enrichment.status = "pending"
        enrichment.poll_after = enrichment.stage_deadline
        charged = dict(receipt.charged_units) or {
            "enrichments": receipt.submitted_count,
            "estimated_credits": receipt.submitted_count,
        }
        enrichment.synchronous_credits = charged["estimated_credits"]
        if receipt.result is not None:
            apply_enrichment_payload(
                session,
                enrichment,
                receipt.result.snapshot_payload,
                codec=codec,
                snapshot_store=snapshot_store,
                contact_cipher=contact_cipher,
                source="synchronous",
                terminal=terminal,
            )
        elif terminal:
            _reconcile_receipt_usage(session, enrichment, context, charged)
            enrichment.status = "completed"
            enrichment.completed_at = datetime.now(UTC)
            session.execute(
                update(RunCandidate)
                .where(
                    RunCandidate.tenant_id == context.tenant_id,
                    RunCandidate.run_id == enrichment.run_id,
                    RunCandidate.candidate_id.in_(
                        [UUID(value) for value in enrichment.candidate_ids]
                    ),
                )
                .values(
                    enrichment_status="unavailable",
                    enriched_at=datetime.now(UTC),
                )
            )
        session.commit()


def _request_dispatchable(
    session_factory: sessionmaker[Session],
    context: RequestContext,
    request_id: UUID,
) -> bool:
    with _tenant_session(session_factory, context.tenant_id) as session:
        enrichment = session.scalar(
            select(EnrichmentRequest)
            .where(
                EnrichmentRequest.id == request_id,
                EnrichmentRequest.tenant_id == context.tenant_id,
            )
            .with_for_update()
        )
        if enrichment is None or enrichment.status != "submitting":
            return False
        run = _load_run(session, context, enrichment.run_id, for_update=True)
        if not run.cancellation_requested and run.state is not RunState.CANCELLED:
            session.rollback()
            return True
        enrichment.status = "cancelled"
        enrichment.completed_at = datetime.now(UTC)
        if enrichment.usage_reconciled_at is None:
            _reconcile_receipt_usage(
                session,
                enrichment,
                context,
                {"enrichments": 0, "estimated_credits": 0},
            )
        session.commit()
        return False


def _reconcile_receipt_usage(
    session: Session,
    enrichment: EnrichmentRequest,
    context: RequestContext,
    charged: dict[str, int],
) -> None:
    if enrichment.usage_reconciled_at is not None:
        return
    SourcingService(session, b"internal-enrichment").reconcile_usage(
        context,
        enrichment.run_id,
        reservation_key=enrichment.reservation_key,
        charged_units=charged,
        provider_request_id=enrichment.provider_request_id,
    )
    enrichment.usage_reconciled_at = datetime.now(UTC)


def _fail_request(
    session_factory: sessionmaker[Session],
    context: RequestContext,
    request_id: UUID,
    *,
    error_code: str,
    charge_reserved: bool = False,
) -> None:
    with _tenant_session(session_factory, context.tenant_id) as session:
        enrichment = session.scalar(
            select(EnrichmentRequest)
            .where(
                EnrichmentRequest.id == request_id,
                EnrichmentRequest.tenant_id == context.tenant_id,
            )
            .with_for_update()
        )
        if enrichment is None or enrichment.status in ("completed", "cancelled"):
            return
        enrichment.status = "failed"
        enrichment.error_code = error_code
        enrichment.completed_at = datetime.now(UTC)
        session.execute(
            update(RunCandidate)
            .where(
                RunCandidate.tenant_id == context.tenant_id,
                RunCandidate.run_id == enrichment.run_id,
                RunCandidate.candidate_id.in_(
                    [UUID(value) for value in enrichment.candidate_ids]
                ),
            )
            .values(enrichment_status="failed", enriched_at=datetime.now(UTC))
        )
        if enrichment.usage_reconciled_at is None:
            charged_units = {"enrichments": 0, "estimated_credits": 0}
            provider_request_id = None
            if charge_reserved:
                ledger = list(
                    session.scalars(
                        select(UsageLedger).where(
                            UsageLedger.tenant_id == context.tenant_id,
                            UsageLedger.run_id == enrichment.run_id,
                            UsageLedger.reservation_key == enrichment.reservation_key,
                        )
                    )
                )
                charged_units = {row.unit_type: row.requested_units for row in ledger}
                provider_request_id = enrichment.provider_request_id
            SourcingService(session, b"internal-enrichment").reconcile_usage(
                context,
                enrichment.run_id,
                reservation_key=enrichment.reservation_key,
                charged_units=charged_units,
                provider_request_id=provider_request_id,
            )
            enrichment.usage_reconciled_at = datetime.now(UTC)
        session.commit()


def _load_run(
    session: Session,
    context: RequestContext,
    run_id: UUID,
    *,
    for_update: bool,
) -> SourcingRun:
    statement = select(SourcingRun).where(
        SourcingRun.tenant_id == context.tenant_id, SourcingRun.id == run_id
    )
    if for_update:
        statement = statement.with_for_update()
    run = session.scalar(statement)
    if run is None:
        raise LookupError("sourcing run not found")
    return run


def _finish_empty_enrichment(
    session_factory: sessionmaker[Session], context: RequestContext, run_id: UUID
) -> None:
    with _tenant_session(session_factory, context.tenant_id) as session:
        run = _load_run(session, context, run_id, for_update=True)
        if run.state is RunState.ENRICHING and not run.cancellation_requested:
            run.state = transition_run(run.state, RunState.READY)
            run.current_stage = RunState.READY.value
            run.completed_at = datetime.now(UTC)
        session.commit()


def _finalize_if_terminal(
    session_factory: sessionmaker[Session], context: RequestContext, run_id: UUID
) -> None:
    with _tenant_session(session_factory, context.tenant_id) as session:
        pending = int(
            session.scalar(
                select(func.count())
                .select_from(EnrichmentRequest)
                .where(
                    EnrichmentRequest.tenant_id == context.tenant_id,
                    EnrichmentRequest.run_id == run_id,
                    EnrichmentRequest.status.in_(("queued", "submitting", "pending")),
                )
            )
            or 0
        )
        if pending:
            return
        run = _load_run(session, context, run_id, for_update=True)
        if run.state is RunState.CANCELLED or run.cancellation_requested:
            return
        failed = int(
            session.scalar(
                select(func.count())
                .select_from(EnrichmentRequest)
                .where(
                    EnrichmentRequest.tenant_id == context.tenant_id,
                    EnrichmentRequest.run_id == run_id,
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
        session.commit()
