import hashlib
import random
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.audit.service import AuditService
from app.candidates.service import CandidateService
from app.core.config import get_settings
from app.core.database import session_factory as database_session_factory
from app.identity.schemas import RequestContext, Role
from app.jobs.service import JobService
from app.matching.engine import MatchingEngine
from app.providers.apollo import ApolloGateway
from app.providers.base import (
    ProviderError,
    ProviderQuery,
    ProviderRateLimited,
    ProviderTemporaryError,
    SearchPage,
)
from app.providers.query_planner import QueryPlanner
from app.sourcing.models import RunCandidate, RunCheckpoint, SourcingRun
from app.sourcing.service import SourcingError, SourcingService
from app.sourcing.state_machine import RunState, transition_run
from app.worker import celery_app

_MAX_RUN_CANDIDATES = 300
_MATCH_BATCH_SIZE = 100
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


class SearchGateway(Protocol):
    def search(self, query: ProviderQuery, page: int) -> SearchPage: ...

    def close(self) -> None: ...


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
    for person in people[:remaining]:
        resolution = service.ingest(context, person)
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
    checkpoint.payload = {"candidate_count": run.candidate_count}


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
        successful_page = session.scalar(
            select(RunCheckpoint.id)
            .where(
                RunCheckpoint.tenant_id == tenant_id,
                RunCheckpoint.run_id == run_id,
                RunCheckpoint.stage == "source",
                RunCheckpoint.status == "completed",
            )
            .limit(1)
        )
        target = RunState.PARTIALLY_READY if successful_page else RunState.FAILED
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
    idempotency_key: str,
) -> Iterator[bool]:
    lock_key = f"{tenant_id}:{run_id}:{idempotency_key}"
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
    with _source_execution_lock(
        session_factory, context.tenant_id, run_id, idempotency_key
    ) as acquired:
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

    gateway = gateway_factory()
    provider_error = False
    budget_exhausted = False
    try:
        for query_number, query in enumerate(queries, start=1):
            page_number: int | None = 1
            while page_number is not None:
                with session_factory() as session:
                    _apply_tenant_context(session, context.tenant_id)
                    run = _load_run(session, run_id, context.tenant_id, for_update=True)
                    if run.cancellation_requested or run.state is RunState.CANCELLED:
                        session.rollback()
                        page_number = None
                        break
                    if _candidate_count(session, run) >= _MAX_RUN_CANDIDATES:
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

                reservation_key = f"source:{query.query_hash}:page:{page_number}"
                with session_factory() as session:
                    _apply_tenant_context(session, context.tenant_id)
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
                    session.commit()
                if budget_exhausted:
                    page_number = None
                    break

                try:
                    page = gateway.search(query, page_number)
                except ProviderError:
                    if propagate_provider_errors:
                        raise
                    provider_error = True
                    page_number = None
                    break

                with session_factory() as session:
                    _apply_tenant_context(session, context.tenant_id)
                    run = _load_run(session, run_id, context.tenant_id, for_update=True)
                    page_key = (
                        f"{idempotency_key}:q{query_number}:p{page.page}:"
                        f"{query.query_hash}"
                    )
                    _record_source_page(session, run, context, page.people, page_key)
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
            if provider_error or budget_exhausted:
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


def _context(tenant_id: str, user_id: str) -> RequestContext:
    return RequestContext(
        tenant_id=UUID(tenant_id),
        user_id=UUID(user_id),
        role=Role.OWNER,
    )


@celery_app.task(
    bind=True,
    name="sourcing.plan_run",
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
    settings = get_settings()
    try:
        execute_source_run(
            database_session_factory,
            UUID(run_id),
            context,
            gateway_factory=lambda: ApolloGateway(settings),
            idempotency_key=idempotency_key,
            propagate_provider_errors=True,
        )
    except (ProviderRateLimited, ProviderTemporaryError) as error:
        if self.request.retries >= self.max_retries:
            _mark_source_retry_exhausted(UUID(run_id), context, idempotency_key)
            return
        raise self.retry(
            exc=error,
            countdown=_provider_retry_countdown(
                error,
                retries=self.request.retries,
            ),
        ) from error
    except ProviderError:
        _mark_source_retry_exhausted(UUID(run_id), context, idempotency_key)
        return
    with database_session_factory() as session:
        _apply_tenant_context(session, context.tenant_id)
        run = _load_run(session, UUID(run_id), context.tenant_id)
        should_match = run.state is RunState.MATCHING
        session.rollback()
    if should_match:
        match_run.delay(run_id, tenant_id, user_id, "match")


@celery_app.task(
    bind=True,
    name="sourcing.match_run",
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
