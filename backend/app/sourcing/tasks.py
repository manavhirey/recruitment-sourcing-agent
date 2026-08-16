import hashlib
import random
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.audit.service import AuditService
from app.candidates.contacts import ContactCipher
from app.candidates.service import CandidateService
from app.core.config import get_worker_settings
from app.core.database import session_factory as database_session_factory
from app.crm.service import materialize_run_matches
from app.identity.schemas import RequestContext, Role
from app.jobs.service import JobService
from app.matching.engine import MatchingEngine
from app.providers.apollo import ApolloGateway
from app.providers.base import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderPermissionError,
    ProviderQuery,
    ProviderRateLimited,
    ProviderTemporaryError,
    SearchPage,
)
from app.providers.health import disable_provider, is_provider_enabled
from app.providers.query_planner import QueryPlanner
from app.providers.snapshots import SnapshotStore
from app.sourcing.enrichment import (
    DeferredEnrichment,
    FailedEnrichment,
    RegionalContactPolicy,
    _fail_request,
    enqueue_top_enrichment,
    execute_queued_enrichment_request,
    fail_active_enrichment_requests,
    poll_enrichment_request,
)
from app.sourcing.models import (
    EnrichmentRequest,
    EnrichmentRetryDispatch,
    RunCandidate,
    RunCheckpoint,
    SourcingRun,
    UsageLedger,
)
from app.sourcing.service import SourcingError, SourcingService
from app.sourcing.state_machine import RunState, transition_run
from app.sourcing.webhooks import CapabilityTokenCodec
from app.worker import celery_app

_MAX_RUN_CANDIDATES = 300
_MATCH_BATCH_SIZE = 100
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_ENRICHMENT_RETRY_CLAIM_LEASE = timedelta(minutes=15)


class SearchGateway(Protocol):
    def search(self, query: ProviderQuery, page: int) -> SearchPage: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class EnrichmentRetrySchedule:
    run_id: UUID
    tenant_id: UUID
    user_id: UUID
    candidate_limit: int
    generation: int
    task_id: str
    not_before: datetime


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _provider_retry_countdown(
    error: ProviderError,
    *,
    retries: int,
    jitter: Callable[[int], int] | None = None,
) -> int:
    if isinstance(error, ProviderRateLimited) and error.retry_after is not None:
        return error.retry_after
    upper = min(300, 2 ** (retries + 1))
    return (jitter or (lambda value: random.randint(0, value)))(upper)


def _record_provider_outcome(endpoint: str, outcome: str) -> None:
    metrics = getattr(celery_app, "_platform_metrics", None)
    if metrics is not None:
        metrics.provider_requests.labels("apollo", endpoint, outcome).inc()
    telemetry = getattr(celery_app, "_telemetry", None)
    if telemetry is not None:
        telemetry.emit(
            "provider_request_completed",
            provider="apollo",
            provider_endpoint=endpoint,
            outcome=outcome,
        )


def _record_budget_exhaustion() -> None:
    metrics = getattr(celery_app, "_platform_metrics", None)
    if metrics is not None:
        metrics.budget_exhaustion.inc()


def _apply_tenant_context(session: Session, tenant_id: UUID) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )


def _query_from_payload(payload: dict[str, object]) -> ProviderQuery:
    return ProviderQuery(
        titles=_query_values(payload, "titles"),
        seniorities=_query_values(payload, "seniorities"),
        person_locations=_query_values(payload, "person_locations"),
        industry_codes=_query_values(payload, "industry_codes"),
        keywords=_query_values(payload, "keywords"),
    )


def _query_values(payload: dict[str, object], key: str) -> tuple[str, ...]:
    values = payload.get(key, [])
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        raise ValueError("stored provider query is invalid")
    return tuple(values)


def _load_run(
    session: Session, run_id: UUID, tenant_id: UUID, *, for_update: bool = False
) -> SourcingRun:
    statement = select(SourcingRun).where(
        SourcingRun.id == run_id,
        SourcingRun.tenant_id == tenant_id,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(
            populate_existing=True
        )
    run = session.scalar(statement)
    if run is None:
        raise LookupError("sourcing run not found")
    return run


def _checkpoint(
    session: Session, run: SourcingRun, key: str, stage: str
) -> RunCheckpoint:
    checkpoint = session.scalar(
        select(RunCheckpoint)
        .where(
            RunCheckpoint.tenant_id == run.tenant_id,
            RunCheckpoint.run_id == run.id,
            RunCheckpoint.idempotency_key == key,
        )
        .with_for_update()
    )
    if checkpoint is None:
        checkpoint = RunCheckpoint(
            tenant_id=run.tenant_id,
            run_id=run.id,
            idempotency_key=key,
            stage=stage,
        )
        session.add(checkpoint)
        session.flush()
    return checkpoint


def _candidate_count(session: Session, run: SourcingRun) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(RunCandidate)
            .where(
                RunCandidate.tenant_id == run.tenant_id,
                RunCandidate.run_id == run.id,
            )
        )
        or 0
    )


def _durable_seen_provider_ids(session: Session, run: SourcingRun) -> set[str]:
    seen: set[str] = set()
    payloads = session.scalars(
        select(RunCheckpoint.payload).where(
            RunCheckpoint.tenant_id == run.tenant_id,
            RunCheckpoint.run_id == run.id,
            RunCheckpoint.stage == "source",
            RunCheckpoint.status == "completed",
        )
    )
    for payload in payloads:
        if payload is None or "provider_person_ids" not in payload:
            continue
        provider_ids = payload["provider_person_ids"]
        if not isinstance(provider_ids, list) or not all(
            isinstance(provider_id, str) and provider_id for provider_id in provider_ids
        ):
            raise ValueError("stored source provider IDs are invalid")
        seen.update(provider_ids)
    if len(seen) > _MAX_RUN_CANDIDATES:
        raise ValueError("stored source provider IDs exceed the run limit")
    return seen


def _new_run_people(
    people: tuple[Any, ...], seen_provider_ids: set[str]
) -> tuple[Any, ...]:
    selected: list[Any] = []
    remaining = _MAX_RUN_CANDIDATES - len(seen_provider_ids)
    for person in people:
        provider_id = person.provider_person_id
        if provider_id in seen_provider_ids:
            continue
        seen_provider_ids.add(provider_id)
        selected.append(person)
        if len(selected) == remaining:
            break
    return tuple(selected)


def execute_plan_run(
    session_factory: sessionmaker[Session],
    run_id: UUID,
    context: RequestContext,
    *,
    idempotency_key: str = "plan",
    planner: QueryPlanner | None = None,
) -> None:
    with session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        run = _load_run(session, run_id, context.tenant_id, for_update=True)
        checkpoint = _checkpoint(session, run, idempotency_key, "plan")
        if checkpoint.status == "completed":
            session.rollback()
            return
        if run.cancellation_requested or run.state is RunState.CANCELLED:
            if run.state is not RunState.CANCELLED:
                run.state = transition_run(run.state, RunState.CANCELLED)
            checkpoint.status = "completed"
            checkpoint.completed_at = datetime.now(UTC)
            session.commit()
            return
        if run.state is not RunState.QUEUED:
            raise ValueError("sourcing run is not queued")
        scorecard = JobService(session, b"internal-worker").get_scorecard(
            context, run.scorecard_version_id
        )
        queries = (planner or QueryPlanner()).compile(scorecard)
        run.planned_queries = [asdict(query) for query in queries]
        run.started_at = run.started_at or datetime.now(UTC)
        run.state = transition_run(run.state, RunState.SOURCING)
        run.current_stage = RunState.SOURCING.value
        checkpoint.status = "completed"
        checkpoint.completed_at = datetime.now(UTC)
        checkpoint.payload = {"query_count": len(queries)}
        AuditService(session).record(
            tenant_id=run.tenant_id,
            run_id=run.id,
            actor_user_id=context.user_id,
            event_key=f"run-planned:{run.id}:{idempotency_key}",
            action="sourcing_run.planned",
            entity_type="sourcing_run",
            entity_id=run.id,
            payload={"query_count": len(queries), "state": run.state.value},
        )
        session.commit()


def execute_match_run(
    session_factory: sessionmaker[Session],
    run_id: UUID,
    context: RequestContext,
    *,
    idempotency_key: str = "match",
    matching_engine: MatchingEngine | None = None,
) -> None:
    engine = matching_engine or MatchingEngine()
    while True:
        with session_factory() as session:
            _apply_tenant_context(session, context.tenant_id)
            run = _load_run(session, run_id, context.tenant_id, for_update=True)
            checkpoint = _checkpoint(session, run, idempotency_key, "match")
            if checkpoint.status == "completed":
                session.rollback()
                return
            if run.cancellation_requested or run.state is RunState.CANCELLED:
                if run.state is not RunState.CANCELLED:
                    run.state = transition_run(run.state, RunState.CANCELLED)
                checkpoint.status = "completed"
                checkpoint.completed_at = datetime.now(UTC)
                session.commit()
                return
            if run.state not in (RunState.MATCHING, RunState.PARTIALLY_READY):
                raise ValueError("sourcing run is not ready for matching")
            unmatched = list(
                session.scalars(
                    select(RunCandidate)
                    .where(
                        RunCandidate.tenant_id == run.tenant_id,
                        RunCandidate.run_id == run.id,
                        RunCandidate.match_score.is_(None),
                    )
                    .order_by(RunCandidate.id)
                    .limit(_MATCH_BATCH_SIZE)
                    .with_for_update()
                )
            )
            if not unmatched:
                materialize_run_matches(session, run, context)
                run.matched_count = int(
                    session.scalar(
                        select(func.count())
                        .select_from(RunCandidate)
                        .where(
                            RunCandidate.tenant_id == run.tenant_id,
                            RunCandidate.run_id == run.id,
                            RunCandidate.match_score.is_not(None),
                        )
                    )
                    or 0
                )
                run.state = transition_run(run.state, RunState.ENRICHING)
                run.current_stage = RunState.ENRICHING.value
                checkpoint.status = "completed"
                checkpoint.completed_at = datetime.now(UTC)
                checkpoint.payload = {"matched_count": run.matched_count}
                AuditService(session).record(
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    actor_user_id=context.user_id,
                    event_key=f"run-matched:{run.id}:{idempotency_key}",
                    action="sourcing_run.matched",
                    entity_type="sourcing_run",
                    entity_id=run.id,
                    payload={
                        "matched_count": run.matched_count,
                        "state": run.state.value,
                    },
                )
                session.commit()
                return
            scorecard = JobService(session, b"internal-worker").get_scorecard(
                context, run.scorecard_version_id
            )
            candidates = CandidateService(session)
            for run_candidate in unmatched:
                profile = candidates.get_profile(context, run_candidate.candidate_id)
                if profile is None:
                    continue
                result = engine.evaluate(scorecard, profile)
                run_candidate.match_score = result.total
                run_candidate.classification = result.classification
                run_candidate.evidence = result.model_dump(mode="json")
                run_candidate.scoring_version = result.scoring_version
                run_candidate.matched_at = datetime.now(UTC)
            session.commit()


def _record_source_page(
    session: Session,
    run: SourcingRun,
    context: RequestContext,
    people: tuple[Any, ...],
    checkpoint_key: str,
) -> None:
    checkpoint = _checkpoint(session, run, checkpoint_key, "source")
    if checkpoint.status == "completed":
        return
    service = CandidateService(session)
    remaining = max(0, _MAX_RUN_CANDIDATES - _candidate_count(session, run))
    persisted_provider_ids: set[str] = set()
    for person in people[:remaining]:
        resolution = service.ingest(context, person)
        if resolution.suppressed:
            continue
        if resolution.candidate_id is None:
            raise RuntimeError("candidate ingestion did not return an identity")
        persisted_provider_ids.add(person.provider_person_id)
        existing = session.scalar(
            select(RunCandidate.id).where(
                RunCandidate.tenant_id == run.tenant_id,
                RunCandidate.run_id == run.id,
                RunCandidate.candidate_id == resolution.candidate_id,
            )
        )
        if existing is None:
            session.add(
                RunCandidate(
                    tenant_id=run.tenant_id,
                    run_id=run.id,
                    candidate_id=resolution.candidate_id,
                    scorecard_version_id=run.scorecard_version_id,
                )
            )
    session.flush()
    run.candidate_count = _candidate_count(session, run)
    checkpoint.status = "completed"
    checkpoint.completed_at = datetime.now(UTC)
    checkpoint.payload = {
        "candidate_count": run.candidate_count,
        "provider_person_ids": sorted(persisted_provider_ids),
        "suppressed_count": len(people[:remaining]) - len(persisted_provider_ids),
    }


def _finish_source(
    session: Session,
    run_id: UUID,
    tenant_id: UUID,
    outer_key: str,
    *,
    error: bool,
    budget_exhausted: bool = False,
) -> None:
    run = _load_run(session, run_id, tenant_id, for_update=True)
    count = _candidate_count(session, run)
    run.candidate_count = count
    if run.cancellation_requested:
        if run.state is not RunState.CANCELLED:
            run.state = transition_run(run.state, RunState.CANCELLED)
        run.current_stage = RunState.CANCELLED.value
        run.completed_at = datetime.now(UTC)
    elif budget_exhausted:
        if run.state is not RunState.PARTIALLY_READY:
            run.state = transition_run(run.state, RunState.PARTIALLY_READY)
        run.current_stage = RunState.PARTIALLY_READY.value
    elif error:
        target = RunState.PARTIALLY_READY if count else RunState.FAILED
        run.state = transition_run(run.state, target)
        run.current_stage = target.value
        run.error_code = "provider_search_failed"
        run.error_message = "The sourcing provider could not complete the search."
        if target is RunState.FAILED:
            run.completed_at = datetime.now(UTC)
    elif count:
        run.state = transition_run(run.state, RunState.MATCHING)
        run.current_stage = RunState.MATCHING.value
    else:
        run.state = transition_run(run.state, RunState.FAILED)
        run.current_stage = RunState.FAILED.value
        run.error_code = "no_usable_results"
        run.error_message = "The provider returned no usable candidates."
        run.completed_at = datetime.now(UTC)
    outer = _checkpoint(session, run, outer_key, "source")
    outer.status = "completed"
    outer.completed_at = datetime.now(UTC)
    outer.payload = {"candidate_count": count, "state": run.state.value}
    AuditService(session).record(
        tenant_id=run.tenant_id,
        run_id=run.id,
        actor_user_id=run.started_by_user_id,
        event_key=f"source-completed:{run.id}:{outer_key}",
        action="sourcing_run.source_completed",
        entity_type="sourcing_run",
        entity_id=run.id,
        payload={
            "candidate_count": count,
            "state": run.state.value,
            "error_code": run.error_code,
        },
    )


@contextmanager
def _source_execution_lock(
    session_factory: sessionmaker[Session],
    tenant_id: UUID,
    run_id: UUID,
) -> Iterator[bool]:
    lock_key = f"{tenant_id}:{run_id}"
    with session_factory() as lock_session:
        if lock_session.get_bind().dialect.name == "postgresql":
            lock_id = int.from_bytes(
                hashlib.sha256(lock_key.encode()).digest()[:8],
                "big",
                signed=True,
            )
            acquired = bool(
                lock_session.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": lock_id},
                )
            )
            try:
                yield acquired
            finally:
                if acquired:
                    lock_session.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": lock_id},
                    )
            return

        with _LOCAL_LOCKS_GUARD:
            local_lock = _LOCAL_LOCKS.setdefault(lock_key, threading.Lock())
        acquired = local_lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                local_lock.release()
                with _LOCAL_LOCKS_GUARD:
                    if not local_lock.locked():
                        _LOCAL_LOCKS.pop(lock_key, None)


def execute_source_run(
    session_factory: sessionmaker[Session],
    run_id: UUID,
    context: RequestContext,
    *,
    gateway_factory: Callable[[], SearchGateway],
    idempotency_key: str = "source",
    propagate_provider_errors: bool = False,
) -> None:
    with _source_execution_lock(session_factory, context.tenant_id, run_id) as acquired:
        if not acquired:
            return
        _execute_source_run(
            session_factory,
            run_id,
            context,
            gateway_factory=gateway_factory,
            idempotency_key=idempotency_key,
            propagate_provider_errors=propagate_provider_errors,
        )


def _execute_source_run(
    session_factory: sessionmaker[Session],
    run_id: UUID,
    context: RequestContext,
    *,
    gateway_factory: Callable[[], SearchGateway],
    idempotency_key: str = "source",
    propagate_provider_errors: bool = False,
) -> None:
    with session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        run = _load_run(session, run_id, context.tenant_id, for_update=True)
        outer = _checkpoint(session, run, idempotency_key, "source")
        if outer.status == "completed":
            session.rollback()
            return
        if run.cancellation_requested or run.state is RunState.CANCELLED:
            if run.state is not RunState.CANCELLED:
                run.state = transition_run(run.state, RunState.CANCELLED)
            outer.status = "completed"
            outer.completed_at = datetime.now(UTC)
            session.commit()
            return
        if run.state is not RunState.SOURCING:
            raise ValueError("sourcing run is not in sourcing state")
        queries = tuple(_query_from_payload(item) for item in run.planned_queries)
        session.commit()

    with session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        run = _load_run(session, run_id, context.tenant_id)
        restored_provider_ids = _durable_seen_provider_ids(session, run)
        session.rollback()

    gateway = gateway_factory()
    restore_seen = getattr(gateway, "restore_seen_provider_ids", None)
    if callable(restore_seen):
        restore_seen(set(restored_provider_ids))
    provider_error = False
    budget_exhausted = False
    cancelled = False
    try:
        for query_number, query in enumerate(queries, start=1):
            page_number: int | None = 1
            while page_number is not None:
                with session_factory() as session:
                    _apply_tenant_context(session, context.tenant_id)
                    run = _load_run(session, run_id, context.tenant_id, for_update=True)
                    if run.cancellation_requested or run.state is RunState.CANCELLED:
                        session.rollback()
                        cancelled = True
                        page_number = None
                        break
                    if (
                        _candidate_count(session, run) >= _MAX_RUN_CANDIDATES
                        or len(_durable_seen_provider_ids(session, run))
                        >= _MAX_RUN_CANDIDATES
                    ):
                        session.rollback()
                        page_number = None
                        break
                    page_key = (
                        f"{idempotency_key}:q{query_number}:p{page_number}:"
                        f"{query.query_hash}"
                    )
                    page_checkpoint = _checkpoint(session, run, page_key, "source")
                    if page_checkpoint.status == "completed":
                        stored_next = (page_checkpoint.payload or {}).get("next_page")
                        if stored_next is None:
                            page_number = None
                        elif (
                            isinstance(stored_next, int)
                            and not isinstance(stored_next, bool)
                            and stored_next > 0
                        ):
                            page_number = stored_next
                        else:
                            raise ValueError("stored source checkpoint is invalid")
                        session.rollback()
                        continue
                    session.rollback()

                with session_factory() as session:
                    _apply_tenant_context(session, context.tenant_id)
                    reservation_base = f"source:{query.query_hash}:page:{page_number}"
                    prior_attempts = int(
                        session.scalar(
                            select(
                                func.count(func.distinct(UsageLedger.reservation_key))
                            ).where(
                                UsageLedger.tenant_id == context.tenant_id,
                                UsageLedger.run_id == run_id,
                                UsageLedger.reservation_key.like(
                                    f"{reservation_base}:attempt-%"
                                ),
                            )
                        )
                        or 0
                    )
                    reservation_key = f"{reservation_base}:attempt-{prior_attempts + 1}"
                    try:
                        SourcingService(session, b"internal-worker").reserve_usage(
                            context,
                            run_id,
                            provider="apollo",
                            endpoint="people_search",
                            reservation_key=reservation_key,
                            requested_units={
                                "search_pages": 1,
                                "estimated_credits": 1,
                            },
                        )
                    except SourcingError as error:
                        if error.code != "usage_budget_exhausted":
                            raise
                        budget_exhausted = True
                        _record_budget_exhaustion()
                    session.commit()
                if budget_exhausted:
                    page_number = None
                    break

                try:
                    page = gateway.search(query, page_number)
                except ProviderError:
                    with session_factory() as session:
                        _apply_tenant_context(session, context.tenant_id)
                        SourcingService(session, b"internal-worker").reconcile_usage(
                            context,
                            run_id,
                            reservation_key=reservation_key,
                            charged_units={
                                "search_pages": 1,
                                "estimated_credits": 1,
                            },
                            provider_request_id=None,
                        )
                        session.commit()
                    if propagate_provider_errors:
                        raise
                    provider_error = True
                    page_number = None
                    break

                with session_factory() as session:
                    _apply_tenant_context(session, context.tenant_id)
                    run = _load_run(session, run_id, context.tenant_id, for_update=True)
                    if run.cancellation_requested or run.state is RunState.CANCELLED:
                        SourcingService(session, b"internal-worker").reconcile_usage(
                            context,
                            run_id,
                            reservation_key=reservation_key,
                            charged_units=dict(page.charged_units),
                            provider_request_id=page.provider_request_id,
                        )
                        session.commit()
                        cancelled = True
                        page_number = None
                        break
                    seen_provider_ids = _durable_seen_provider_ids(session, run)
                    run_people = _new_run_people(page.people, seen_provider_ids)
                    page_key = (
                        f"{idempotency_key}:q{query_number}:p{page.page}:"
                        f"{query.query_hash}"
                    )
                    _record_source_page(session, run, context, run_people, page_key)
                    checkpoint = _checkpoint(session, run, page_key, "source")
                    checkpoint.payload = {
                        **(checkpoint.payload or {}),
                        "next_page": page.next_page,
                    }
                    SourcingService(session, b"internal-worker").reconcile_usage(
                        context,
                        run_id,
                        reservation_key=reservation_key,
                        charged_units=dict(page.charged_units),
                        provider_request_id=page.provider_request_id,
                    )
                    session.commit()
                page_number = page.next_page
            if provider_error or budget_exhausted or cancelled:
                break
    finally:
        close = getattr(gateway, "close", None)
        if callable(close):
            with suppress(Exception):
                close()

    with session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        _finish_source(
            session,
            run_id,
            context.tenant_id,
            idempotency_key,
            error=provider_error,
            budget_exhausted=budget_exhausted,
        )
        session.commit()


def _mark_source_retry_exhausted(
    run_id: UUID, context: RequestContext, idempotency_key: str
) -> None:
    with database_session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        _finish_source(
            session,
            run_id,
            context.tenant_id,
            idempotency_key,
            error=True,
        )
        session.commit()


def _mark_enrichment_provider_disabled(run_id: UUID, context: RequestContext) -> None:
    with database_session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        run = _load_run(session, run_id, context.tenant_id, for_update=True)
        if run.state in (RunState.ENRICHING, RunState.PARTIALLY_READY):
            if run.state is RunState.ENRICHING:
                run.state = transition_run(run.state, RunState.PARTIALLY_READY)
            run.current_stage = RunState.PARTIALLY_READY.value
            run.error_code = "provider_connector_disabled"
            run.error_message = "Contact enrichment is temporarily unavailable."
        session.commit()


def _enrichment_request_run_id(
    request_id: UUID, context: RequestContext
) -> UUID | None:
    with database_session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        run_id = session.scalar(
            select(EnrichmentRequest.run_id).where(
                EnrichmentRequest.id == request_id,
                EnrichmentRequest.tenant_id == context.tenant_id,
                EnrichmentRequest.dispatch_requested_by_user_id == context.user_id,
            )
        )
        session.rollback()
        return run_id


def _requeue_enrichment_dispatch(request_id: UUID, context: RequestContext) -> bool:
    with database_session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        request = session.scalar(
            select(EnrichmentRequest)
            .where(
                EnrichmentRequest.id == request_id,
                EnrichmentRequest.tenant_id == context.tenant_id,
            )
            .with_for_update()
        )
        if request is None:
            session.rollback()
            return False
        if request.dispatch_requested_by_user_id != context.user_id:
            session.rollback()
            return False
        if request.status not in ("queued", "submitting"):
            request.dispatch_pending = False
            request.dispatch_claimed_at = None
            request.dispatch_claim_token = None
            session.commit()
            return False
        request.dispatch_pending = True
        request.dispatch_claimed_at = None
        request.dispatch_claim_token = None
        session.commit()
        return True


def _mark_enrichment_request_provider_disabled(
    request_id: UUID, context: RequestContext
) -> None:
    with database_session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        request = session.scalar(
            select(EnrichmentRequest).where(
                EnrichmentRequest.id == request_id,
                EnrichmentRequest.tenant_id == context.tenant_id,
            )
        )
        charge_reserved = bool(
            request is not None and request.provider_request_id is not None
        )
        session.rollback()
    _fail_request(
        database_session_factory,
        context,
        request_id,
        error_code="provider_connector_disabled",
        charge_reserved=charge_reserved,
    )


def _run_is_match_eligible(
    session_factory: sessionmaker[Session],
    run_id: UUID,
    context: RequestContext,
) -> bool:
    with session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        run = _load_run(session, run_id, context.tenant_id)
        eligible = run.candidate_count > 0 and run.state in (
            RunState.MATCHING,
            RunState.PARTIALLY_READY,
        )
        session.rollback()
    return eligible


def _run_is_enrich_eligible(
    session_factory: sessionmaker[Session],
    run_id: UUID,
    context: RequestContext,
) -> bool:
    with session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        run = _load_run(session, run_id, context.tenant_id)
        eligible = not run.cancellation_requested and run.state in (
            RunState.ENRICHING,
            RunState.PARTIALLY_READY,
        )
        session.rollback()
    return eligible


@contextmanager
def _enrichment_execution_lock(
    session_factory: sessionmaker[Session],
    tenant_id: UUID,
    run_id: UUID,
) -> Iterator[bool]:
    lock_key = f"enrichment:{tenant_id}:{run_id}"
    with session_factory() as probe_session:
        bind = probe_session.get_bind()
    if bind.dialect.name == "postgresql":
        if not isinstance(bind, Engine):
            raise TypeError("enrichment execution lock requires an engine bind")
        lock_id = int.from_bytes(
            hashlib.sha256(lock_key.encode()).digest()[:8],
            "big",
            signed=True,
        )
        with bind.connect() as lock_connection:
            acquired = bool(
                lock_connection.scalar(
                    text("SELECT pg_try_advisory_lock(:lock_id)"),
                    {"lock_id": lock_id},
                )
            )
            lock_connection.commit()
            try:
                yield acquired
            finally:
                if acquired:
                    lock_connection.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": lock_id},
                    )
                    lock_connection.commit()
        return

    with _LOCAL_LOCKS_GUARD:
        local_lock = _LOCAL_LOCKS.setdefault(lock_key, threading.Lock())
    acquired = local_lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            local_lock.release()
            with _LOCAL_LOCKS_GUARD:
                if not local_lock.locked():
                    _LOCAL_LOCKS.pop(lock_key, None)


def _retry_state_fingerprint(
    session: Session,
    context: RequestContext,
    run_id: UUID,
) -> str:
    states = session.execute(
        select(
            EnrichmentRequest.id,
            EnrichmentRequest.status,
            EnrichmentRequest.retry_count,
            EnrichmentRequest.poll_after,
            EnrichmentRequest.stage_deadline,
        )
        .where(
            EnrichmentRequest.tenant_id == context.tenant_id,
            EnrichmentRequest.run_id == run_id,
            EnrichmentRequest.status.in_(("queued", "submitting")),
        )
        .order_by(EnrichmentRequest.id)
    ).all()
    fingerprint = "\0".join(
        ":".join(
            (
                str(request_id),
                status,
                str(retry_count),
                poll_after.isoformat() if poll_after is not None else "",
                stage_deadline.isoformat() if stage_deadline is not None else "",
            )
        )
        for request_id, status, retry_count, poll_after, stage_deadline in states
    )
    return hashlib.sha256(fingerprint.encode()).hexdigest()


def _retry_schedule(row: EnrichmentRetryDispatch) -> EnrichmentRetrySchedule:
    return EnrichmentRetrySchedule(
        run_id=row.run_id,
        tenant_id=row.tenant_id,
        user_id=row.requested_by_user_id,
        candidate_limit=row.candidate_limit,
        generation=row.generation,
        task_id=row.task_id,
        not_before=_utc(row.not_before),
    )


def _stage_enrichment_retry(
    session_factory: sessionmaker[Session],
    context: RequestContext,
    run_id: UUID,
    *,
    candidate_limit: int,
    retry_after: int,
    current_generation: int | None,
    current_claim_token: UUID | None,
) -> EnrichmentRetrySchedule | None:
    with session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        row = session.scalar(
            select(EnrichmentRetryDispatch)
            .where(
                EnrichmentRetryDispatch.tenant_id == context.tenant_id,
                EnrichmentRetryDispatch.run_id == run_id,
            )
            .with_for_update()
        )
        fingerprint = _retry_state_fingerprint(session, context, run_id)
        if current_generation is None:
            if (
                row is not None
                and row.status in ("pending", "published")
                and row.state_fingerprint == fingerprint
            ):
                schedule = _retry_schedule(row)
                session.rollback()
                return schedule
        elif (
            row is None
            or row.generation != current_generation
            or row.status != "claimed"
            or row.claim_token != current_claim_token
        ):
            session.rollback()
            return None
        generation = (row.generation if row is not None else 0) + 1
        now = datetime.now(UTC)
        if row is None:
            row = EnrichmentRetryDispatch(
                tenant_id=context.tenant_id,
                run_id=run_id,
                generation=generation,
                status="pending",
                state_fingerprint=fingerprint,
                task_id=f"enrich-run-retry:{run_id}:{generation}",
                requested_by_user_id=context.user_id,
                candidate_limit=candidate_limit,
                not_before=now + timedelta(seconds=max(1, retry_after)),
            )
            session.add(row)
        else:
            row.generation = generation
            row.status = "pending"
            row.state_fingerprint = fingerprint
            row.task_id = f"enrich-run-retry:{run_id}:{generation}"
            row.requested_by_user_id = context.user_id
            row.candidate_limit = candidate_limit
            row.not_before = now + timedelta(seconds=max(1, retry_after))
            row.claim_token = None
            row.claimed_at = None
        session.commit()
        return _retry_schedule(row)


def _claim_retry_publish(
    session_factory: sessionmaker[Session],
    context: RequestContext,
    schedule: EnrichmentRetrySchedule,
) -> UUID | None:
    with session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        row = session.scalar(
            select(EnrichmentRetryDispatch)
            .where(
                EnrichmentRetryDispatch.tenant_id == context.tenant_id,
                EnrichmentRetryDispatch.run_id == schedule.run_id,
            )
            .with_for_update()
        )
        if (
            row is None
            or row.generation != schedule.generation
            or row.status != "pending"
        ):
            session.rollback()
            return None
        claimed_at = _utc(row.claimed_at) if row.claimed_at is not None else None
        if claimed_at is not None and claimed_at >= (
            datetime.now(UTC) - _ENRICHMENT_RETRY_CLAIM_LEASE
        ):
            session.rollback()
            return None
        claim_token = uuid4()
        row.claim_token = claim_token
        row.claimed_at = datetime.now(UTC)
        session.commit()
        return claim_token


def _finish_retry_publish(
    session_factory: sessionmaker[Session],
    context: RequestContext,
    schedule: EnrichmentRetrySchedule,
    claim_token: UUID,
    *,
    published: bool,
) -> bool:
    with session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        row = session.scalar(
            select(EnrichmentRetryDispatch)
            .where(
                EnrichmentRetryDispatch.tenant_id == context.tenant_id,
                EnrichmentRetryDispatch.run_id == schedule.run_id,
            )
            .with_for_update()
        )
        if (
            row is None
            or row.generation != schedule.generation
            or row.status != "pending"
            or row.claim_token != claim_token
        ):
            session.rollback()
            return False
        if published:
            row.status = "published"
        row.claim_token = None
        row.claimed_at = None
        session.commit()
        return True


def _publish_enrichment_retry(
    session_factory: sessionmaker[Session],
    context: RequestContext,
    schedule: EnrichmentRetrySchedule,
) -> bool:
    claim_token = _claim_retry_publish(session_factory, context, schedule)
    if claim_token is None:
        return False
    countdown = max(
        0,
        int((schedule.not_before - datetime.now(UTC)).total_seconds() + 0.999999),
    )
    try:
        enrich_run.apply_async(
            args=(
                str(schedule.run_id),
                str(schedule.tenant_id),
                str(schedule.user_id),
                schedule.candidate_limit,
                schedule.generation,
            ),
            countdown=countdown,
            task_id=schedule.task_id,
        )
    except Exception:
        _finish_retry_publish(
            session_factory,
            context,
            schedule,
            claim_token,
            published=False,
        )
        raise
    _finish_retry_publish(
        session_factory,
        context,
        schedule,
        claim_token,
        published=True,
    )
    return True


def _claim_enrichment_retry_delivery(
    session_factory: sessionmaker[Session],
    context: RequestContext,
    run_id: UUID,
    generation: int,
    candidate_limit: int,
) -> UUID | None:
    with session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        row = session.scalar(
            select(EnrichmentRetryDispatch)
            .where(
                EnrichmentRetryDispatch.tenant_id == context.tenant_id,
                EnrichmentRetryDispatch.run_id == run_id,
            )
            .with_for_update()
        )
        if (
            row is None
            or row.generation != generation
            or row.requested_by_user_id != context.user_id
            or row.candidate_limit != candidate_limit
            or _utc(row.not_before) > datetime.now(UTC)
        ):
            session.rollback()
            return None
        claimed_at = _utc(row.claimed_at) if row.claimed_at is not None else None
        claim_expired = claimed_at is None or claimed_at < (
            datetime.now(UTC) - _ENRICHMENT_RETRY_CLAIM_LEASE
        )
        if row.status not in ("pending", "published") and not (
            row.status == "claimed" and claim_expired
        ):
            session.rollback()
            return None
        claim_token = uuid4()
        row.status = "claimed"
        row.claim_token = claim_token
        row.claimed_at = datetime.now(UTC)
        session.commit()
        return claim_token


def _has_active_enrichment_retry(
    session_factory: sessionmaker[Session],
    context: RequestContext,
    run_id: UUID,
) -> bool:
    with session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        status = session.scalar(
            select(EnrichmentRetryDispatch.status).where(
                EnrichmentRetryDispatch.tenant_id == context.tenant_id,
                EnrichmentRetryDispatch.run_id == run_id,
            )
        )
        session.rollback()
    return status in ("pending", "published", "claimed")


def _complete_enrichment_retry_delivery(
    session_factory: sessionmaker[Session],
    context: RequestContext,
    run_id: UUID,
    generation: int | None,
    claim_token: UUID | None,
) -> bool:
    if generation is None:
        return True
    with session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        row = session.scalar(
            select(EnrichmentRetryDispatch)
            .where(
                EnrichmentRetryDispatch.tenant_id == context.tenant_id,
                EnrichmentRetryDispatch.run_id == run_id,
            )
            .with_for_update()
        )
        if (
            row is None
            or row.generation != generation
            or row.status != "claimed"
            or row.claim_token != claim_token
        ):
            session.rollback()
            return False
        row.status = "completed"
        row.claim_token = None
        row.claimed_at = None
        session.commit()
        return True


def _release_enrichment_retry_delivery(
    session_factory: sessionmaker[Session],
    context: RequestContext,
    run_id: UUID,
    generation: int | None,
    claim_token: UUID | None,
) -> bool:
    if generation is None:
        return True
    with session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        row = session.scalar(
            select(EnrichmentRetryDispatch)
            .where(
                EnrichmentRetryDispatch.tenant_id == context.tenant_id,
                EnrichmentRetryDispatch.run_id == run_id,
            )
            .with_for_update()
        )
        if (
            row is None
            or row.generation != generation
            or row.status != "claimed"
            or row.claim_token != claim_token
        ):
            session.rollback()
            return False
        row.status = "published"
        row.claim_token = None
        row.claimed_at = None
        session.commit()
        return True


def _enrichment_dependencies(settings: Any):
    import boto3  # type: ignore[import-untyped]

    cipher = ContactCipher(
        settings.contact_encryption_key.get_secret_value(),
        settings.suppression_hmac_key.get_secret_value().encode(),
    )
    snapshots = SnapshotStore(
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
    policy = RegionalContactPolicy(
        settings.apollo_reveal_personal_emails,
        settings.apollo_reveal_phone_numbers,
    )
    codec = CapabilityTokenCodec(settings.webhook_hmac_key.get_secret_value().encode())
    return cipher, snapshots, policy, codec


def _context(tenant_id: str, user_id: str) -> RequestContext:
    return RequestContext(
        tenant_id=UUID(tenant_id),
        user_id=UUID(user_id),
        role=Role.OWNER,
    )


@celery_app.task(
    bind=True,
    name="sourcing.plan_run",
    shared=False,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=5,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
)
def plan_run(
    self: Any,
    run_id: str,
    tenant_id: str,
    user_id: str,
    idempotency_key: str = "plan",
) -> None:
    context = _context(tenant_id, user_id)
    execute_plan_run(
        database_session_factory,
        UUID(run_id),
        context,
        idempotency_key=idempotency_key,
    )
    source_run.delay(run_id, tenant_id, user_id, "source")


@celery_app.task(
    bind=True,
    name="sourcing.source_run",
    shared=False,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=5,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
)
def source_run(
    self: Any,
    run_id: str,
    tenant_id: str,
    user_id: str,
    idempotency_key: str = "source",
) -> None:
    context = _context(tenant_id, user_id)
    if not is_provider_enabled(database_session_factory, "apollo"):
        _record_provider_outcome("people_search_run", "connector_disabled")
        _mark_source_retry_exhausted(UUID(run_id), context, idempotency_key)
        return
    settings = get_worker_settings()
    try:
        execute_source_run(
            database_session_factory,
            UUID(run_id),
            context,
            gateway_factory=lambda: ApolloGateway(settings),
            idempotency_key=idempotency_key,
            propagate_provider_errors=True,
        )
        _record_provider_outcome("people_search_run", "success")
    except ProviderAuthenticationError:
        disable_provider(database_session_factory, "apollo", "authentication_error")
        _record_provider_outcome("people_search_run", "authentication_error")
        _mark_source_retry_exhausted(UUID(run_id), context, idempotency_key)
    except ProviderPermissionError:
        disable_provider(database_session_factory, "apollo", "permission_error")
        _record_provider_outcome("people_search_run", "permission_error")
        _mark_source_retry_exhausted(UUID(run_id), context, idempotency_key)
    except (ProviderRateLimited, ProviderTemporaryError) as error:
        _record_provider_outcome(
            "people_search_run",
            "rate_limited"
            if isinstance(error, ProviderRateLimited)
            else "temporary_error",
        )
        if self.request.retries >= self.max_retries:
            _mark_source_retry_exhausted(UUID(run_id), context, idempotency_key)
        else:
            raise self.retry(
                exc=error,
                countdown=_provider_retry_countdown(
                    error,
                    retries=self.request.retries,
                ),
            ) from error
    except ProviderError:
        _record_provider_outcome("people_search_run", "provider_error")
        _mark_source_retry_exhausted(UUID(run_id), context, idempotency_key)
    if _run_is_match_eligible(database_session_factory, UUID(run_id), context):
        match_run.delay(run_id, tenant_id, user_id, "match")


@celery_app.task(
    bind=True,
    name="sourcing.match_run",
    shared=False,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=5,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
)
def match_run(
    self: Any,
    run_id: str,
    tenant_id: str,
    user_id: str,
    idempotency_key: str = "match",
) -> None:
    execute_match_run(
        database_session_factory,
        UUID(run_id),
        _context(tenant_id, user_id),
        idempotency_key=idempotency_key,
    )
    context = _context(tenant_id, user_id)
    if _run_is_enrich_eligible(database_session_factory, UUID(run_id), context):
        enrich_run.delay(run_id, tenant_id, user_id, 50)


@celery_app.task(
    bind=True,
    name="sourcing.enrich_run",
    shared=False,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=5,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
)
def enrich_run(
    self: Any,
    run_id: str,
    tenant_id: str,
    user_id: str,
    limit: int = 50,
    retry_generation: int | None = None,
) -> None:
    context = _context(tenant_id, user_id)
    parsed_run_id = UUID(run_id)
    claim_token: UUID | None = None
    with _enrichment_execution_lock(
        database_session_factory,
        context.tenant_id,
        parsed_run_id,
    ) as acquired:
        if not acquired:
            return
        if retry_generation is None:
            if _has_active_enrichment_retry(
                database_session_factory, context, parsed_run_id
            ):
                return
        else:
            claim_token = _claim_enrichment_retry_delivery(
                database_session_factory,
                context,
                parsed_run_id,
                retry_generation,
                limit,
            )
            if claim_token is None:
                return
        try:
            if not is_provider_enabled(database_session_factory, "apollo"):
                _record_provider_outcome("people_enrichment", "connector_disabled")
                fail_active_enrichment_requests(
                    database_session_factory,
                    context,
                    parsed_run_id,
                    error_code="provider_connector_disabled",
                )
                _mark_enrichment_provider_disabled(parsed_run_id, context)
                _complete_enrichment_retry_delivery(
                    database_session_factory,
                    context,
                    parsed_run_id,
                    retry_generation,
                    claim_token,
                )
                return
            settings = get_worker_settings()
            cipher, snapshots, policy, codec = _enrichment_dependencies(settings)
            gateway = ApolloGateway(settings)
            try:
                submissions = enqueue_top_enrichment(
                    parsed_run_id,
                    limit,
                    session_factory=database_session_factory,
                    context=context,
                    gateway=gateway,
                    callback_base_url=settings.webhook_base_url,
                    contact_cipher=cipher,
                    snapshot_store=snapshots,
                    policy=policy,
                    token_codec=codec,
                    on_budget_exhausted=_record_budget_exhaustion,
                )
            except ProviderAuthenticationError:
                disable_provider(
                    database_session_factory, "apollo", "authentication_error"
                )
                _record_provider_outcome("people_enrichment", "authentication_error")
                fail_active_enrichment_requests(
                    database_session_factory,
                    context,
                    parsed_run_id,
                    error_code="provider_authorization_failed",
                )
                _mark_enrichment_provider_disabled(parsed_run_id, context)
                _complete_enrichment_retry_delivery(
                    database_session_factory,
                    context,
                    parsed_run_id,
                    retry_generation,
                    claim_token,
                )
                return
            except ProviderPermissionError:
                disable_provider(database_session_factory, "apollo", "permission_error")
                _record_provider_outcome("people_enrichment", "permission_error")
                fail_active_enrichment_requests(
                    database_session_factory,
                    context,
                    parsed_run_id,
                    error_code="provider_authorization_failed",
                )
                _mark_enrichment_provider_disabled(parsed_run_id, context)
                _complete_enrichment_retry_delivery(
                    database_session_factory,
                    context,
                    parsed_run_id,
                    retry_generation,
                    claim_token,
                )
                return
            finally:
                gateway.close()
            retry_delays: list[int] = []
            provider_failed = False
            for submission in submissions:
                if isinstance(submission, DeferredEnrichment):
                    retry_delays.append(submission.retry_after_seconds)
                    continue
                if isinstance(submission, FailedEnrichment):
                    provider_failed = True
                    continue
                with database_session_factory() as session:
                    _apply_tenant_context(session, context.tenant_id)
                    request = session.get(EnrichmentRequest, submission.request_id)
                    should_poll = request is not None and request.status == "pending"
                if should_poll:
                    poll_enrichment_result.apply_async(
                        args=(
                            str(submission.request_id),
                            tenant_id,
                            user_id,
                        ),
                        countdown=300,
                    )
            if retry_delays:
                schedule = _stage_enrichment_retry(
                    database_session_factory,
                    context,
                    parsed_run_id,
                    candidate_limit=limit,
                    retry_after=min(retry_delays),
                    current_generation=retry_generation,
                    current_claim_token=claim_token,
                )
                if schedule is None:
                    return
                _record_provider_outcome("people_enrichment", "retry_scheduled")
                _publish_enrichment_retry(
                    database_session_factory,
                    context,
                    schedule,
                )
                return
            _complete_enrichment_retry_delivery(
                database_session_factory,
                context,
                parsed_run_id,
                retry_generation,
                claim_token,
            )
            _record_provider_outcome(
                "people_enrichment",
                "provider_error" if provider_failed else "success",
            )
        except OperationalError:
            _release_enrichment_retry_delivery(
                database_session_factory,
                context,
                parsed_run_id,
                retry_generation,
                claim_token,
            )
            raise


@celery_app.task(
    bind=True,
    name="sourcing.enrich_request",
    shared=False,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=5,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
)
def enrich_request(
    self: Any,
    request_id: str,
    tenant_id: str,
    user_id: str,
) -> None:
    context = _context(tenant_id, user_id)
    parsed_request_id = UUID(request_id)
    run_id = _enrichment_request_run_id(parsed_request_id, context)
    if run_id is None:
        return
    with _enrichment_execution_lock(
        database_session_factory,
        context.tenant_id,
        run_id,
    ) as acquired:
        if not acquired:
            if _requeue_enrichment_dispatch(parsed_request_id, context):
                _record_provider_outcome("people_enrichment", "retry_scheduled")
            return
        if not is_provider_enabled(database_session_factory, "apollo"):
            _record_provider_outcome("people_enrichment", "connector_disabled")
            _mark_enrichment_request_provider_disabled(parsed_request_id, context)
            return
        settings = get_worker_settings()
        cipher, snapshots, policy, codec = _enrichment_dependencies(settings)
        gateway = ApolloGateway(settings)
        try:
            submission = execute_queued_enrichment_request(
                database_session_factory,
                parsed_request_id,
                context,
                gateway=gateway,
                callback_base_url=settings.webhook_base_url,
                contact_cipher=cipher,
                snapshot_store=snapshots,
                policy=policy,
                token_codec=codec,
            )
        except ProviderAuthenticationError:
            disable_provider(database_session_factory, "apollo", "authentication_error")
            _record_provider_outcome("people_enrichment", "authentication_error")
            _mark_enrichment_request_provider_disabled(parsed_request_id, context)
            return
        except ProviderPermissionError:
            disable_provider(database_session_factory, "apollo", "permission_error")
            _record_provider_outcome("people_enrichment", "permission_error")
            _mark_enrichment_request_provider_disabled(parsed_request_id, context)
            return
        finally:
            gateway.close()
        if isinstance(submission, DeferredEnrichment):
            _record_provider_outcome("people_enrichment", "retry_scheduled")
            raise self.retry(countdown=submission.retry_after_seconds)
        _record_provider_outcome(
            "people_enrichment",
            "provider_error" if isinstance(submission, FailedEnrichment) else "success",
        )
        if submission is not None:
            poll_enrichment_result.apply_async(
                args=(request_id, tenant_id, user_id), countdown=300
            )


@celery_app.task(
    bind=True,
    name="sourcing.poll_enrichment_result",
    shared=False,
    acks_late=True,
    reject_on_worker_lost=True,
    max_retries=5,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
)
def poll_enrichment_result(
    self: Any,
    request_id: str,
    tenant_id: str,
    user_id: str,
) -> None:
    context = _context(tenant_id, user_id)
    if not is_provider_enabled(database_session_factory, "apollo"):
        _record_provider_outcome("enrichment_poll", "connector_disabled")
        _mark_enrichment_request_provider_disabled(UUID(request_id), context)
        return
    settings = get_worker_settings()
    cipher, snapshots, _, codec = _enrichment_dependencies(settings)
    gateway = ApolloGateway(settings)
    try:
        poll_result = poll_enrichment_request(
            database_session_factory,
            UUID(request_id),
            context,
            gateway=gateway,
            token_codec=codec,
            snapshot_store=snapshots,
            contact_cipher=cipher,
        )
    except ProviderAuthenticationError:
        disable_provider(database_session_factory, "apollo", "authentication_error")
        _record_provider_outcome("enrichment_poll", "authentication_error")
        _mark_enrichment_request_provider_disabled(UUID(request_id), context)
        return
    except ProviderPermissionError:
        disable_provider(database_session_factory, "apollo", "permission_error")
        _record_provider_outcome("enrichment_poll", "permission_error")
        _mark_enrichment_request_provider_disabled(UUID(request_id), context)
        return
    finally:
        gateway.close()
    if isinstance(poll_result, FailedEnrichment):
        _record_provider_outcome("enrichment_poll", "provider_error")
    elif isinstance(poll_result, int):
        _record_provider_outcome("enrichment_poll", "retry_scheduled")
        poll_enrichment_result.apply_async(
            args=(request_id, tenant_id, user_id), countdown=poll_result
        )
    else:
        _record_provider_outcome("enrichment_poll", "success")
