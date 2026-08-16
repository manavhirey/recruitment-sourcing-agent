from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.audit.service import AuditService
from app.core.errors import AppError
from app.identity.models import IdentityIdempotencyKey
from app.identity.schemas import RequestContext, Role
from app.identity.service import IdentityError, MembershipService
from app.jobs.models import ScorecardVersion
from app.jobs.service import JobError, JobService
from app.sourcing.models import (
    EnrichmentRequest,
    RunCandidate,
    SourcingRun,
    TenantNotification,
    UsageBudget,
    UsageLedger,
)
from app.sourcing.state_machine import RunState, transition_run

_ACTIVE_STATES = (
    RunState.QUEUED,
    RunState.SOURCING,
    RunState.MATCHING,
    RunState.ENRICHING,
    RunState.PARTIALLY_READY,
)
_TERMINAL_STATES = (RunState.READY, RunState.CANCELLED, RunState.FAILED)
_UNIT_CAP_COLUMNS = {
    "search_pages": UsageBudget.max_search_pages,
    "enrichments": UsageBudget.max_enrichments,
    "estimated_credits": UsageBudget.max_estimated_credits,
}


class SourcingError(AppError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SourcingService:
    def __init__(self, session: Session, hmac_key: bytes) -> None:
        self.session = session
        self._jobs = JobService(session, hmac_key)
        self._idempotency = MembershipService(session, hmac_key)
        self._audit = AuditService(session)

    def start(
        self,
        context: RequestContext,
        job_id: UUID,
        *,
        idempotency_key: str,
    ) -> SourcingRun:
        job = self._job(context, job_id, for_update=True)
        record = self._begin(
            context,
            f"start_sourcing_run:{job_id}",
            idempotency_key,
            {"job_id": str(job_id)},
        )
        if record.response_payload is not None:
            return self.get_authorized(
                context, UUID(str(record.response_payload["run_id"]))
            )
        if job.current_scorecard_id is None:
            raise SourcingError("scorecard_required")
        scorecard = self.session.scalar(
            select(ScorecardVersion).where(
                ScorecardVersion.id == job.current_scorecard_id,
                ScorecardVersion.tenant_id == context.tenant_id,
                ScorecardVersion.job_id == job.id,
            )
        )
        if scorecard is None:
            raise SourcingError("scorecard_required")
        active = self.session.scalar(
            select(SourcingRun.id).where(
                SourcingRun.tenant_id == context.tenant_id,
                SourcingRun.job_id == job.id,
                SourcingRun.scorecard_version_id == scorecard.id,
                SourcingRun.state.in_(_ACTIVE_STATES),
            )
        )
        if active is not None:
            raise SourcingError("active_run_exists")
        run = SourcingRun(
            id=uuid4(),
            tenant_id=context.tenant_id,
            job_id=job.id,
            scorecard_version_id=scorecard.id,
            started_by_user_id=context.user_id,
            state=RunState.QUEUED,
            current_stage=RunState.QUEUED.value,
        )
        self.session.add(run)
        self.session.flush()
        self._audit.record(
            tenant_id=context.tenant_id,
            run_id=run.id,
            actor_user_id=context.user_id,
            event_key=f"sourcing-run-started:{record.id}",
            action="sourcing_run.started",
            entity_type="sourcing_run",
            entity_id=run.id,
            payload={
                "job_id": str(job.id),
                "scorecard_version_id": str(scorecard.id),
                "state": run.state.value,
            },
        )
        self._complete(record, {"run_id": str(run.id)})
        return run

    def cancel(
        self,
        context: RequestContext,
        run_id: UUID,
        *,
        idempotency_key: str,
    ) -> SourcingRun:
        run = self.get_authorized(context, run_id, for_update=True)
        record = self._begin(
            context,
            f"cancel_sourcing_run:{run_id}",
            idempotency_key,
            {"run_id": str(run_id)},
        )
        if record.response_payload is not None:
            return self.get_authorized(context, run_id)
        if run.state not in _TERMINAL_STATES:
            now = datetime.now(UTC)
            run.cancellation_requested = True
            run.cancellation_requested_at = now
            run.cancellation_requested_by_user_id = context.user_id
            run.state = transition_run(run.state, RunState.CANCELLED)
            run.current_stage = RunState.CANCELLED.value
            run.completed_at = now
            self._audit.record(
                tenant_id=context.tenant_id,
                run_id=run.id,
                actor_user_id=context.user_id,
                event_key=f"sourcing-run-cancelled:{record.id}",
                action="sourcing_run.cancelled",
                entity_type="sourcing_run",
                entity_id=run.id,
                payload={"state": RunState.CANCELLED.value},
            )
        self._complete(record, {"run_id": str(run.id)})
        self.session.flush()
        return run

    def get_authorized(
        self,
        context: RequestContext,
        run_id: UUID,
        *,
        for_update: bool = False,
    ) -> SourcingRun:
        statement = select(SourcingRun).where(
            SourcingRun.id == run_id,
            SourcingRun.tenant_id == context.tenant_id,
        )
        if for_update:
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        run = self.session.scalar(statement)
        if run is None:
            raise SourcingError("run_not_found")
        self._job(context, run.job_id)
        return run

    def activity(self, context: RequestContext, run_id: UUID) -> list[AuditEvent]:
        self.get_authorized(context, run_id)
        return self._audit.for_run(context.tenant_id, run_id)

    def usage_totals(self, context: RequestContext, run_id: UUID) -> dict[str, int]:
        run = self.get_authorized(context, run_id)
        rows = self.session.execute(
            select(
                UsageLedger.unit_type,
                func.coalesce(
                    func.sum(
                        case(
                            (
                                UsageLedger.charged_units.is_(None),
                                UsageLedger.requested_units,
                            ),
                            else_=UsageLedger.charged_units,
                        )
                    ),
                    0,
                ),
            )
            .where(
                UsageLedger.tenant_id == context.tenant_id,
                UsageLedger.run_id == run.id,
            )
            .group_by(UsageLedger.unit_type)
        ).all()
        totals = {unit_type: 0 for unit_type in _UNIT_CAP_COLUMNS}
        totals.update({str(unit_type): int(total) for unit_type, total in rows})
        return totals

    def queue_on_demand_enrichment(
        self,
        context: RequestContext,
        run_candidate_id: UUID,
        *,
        idempotency_key: str,
    ) -> tuple[EnrichmentRequest, bool]:
        run_candidate = self.session.scalar(
            select(RunCandidate)
            .where(
                RunCandidate.id == run_candidate_id,
                RunCandidate.tenant_id == context.tenant_id,
            )
            .with_for_update()
        )
        if run_candidate is None:
            raise SourcingError("run_candidate_not_found")
        run = self.get_authorized(context, run_candidate.run_id, for_update=True)
        record = self._begin(
            context,
            f"enrich_run_candidate:{run_candidate_id}",
            idempotency_key,
            {"run_candidate_id": str(run_candidate_id)},
        )
        if record.response_payload is not None:
            request = self.session.scalar(
                select(EnrichmentRequest).where(
                    EnrichmentRequest.id
                    == UUID(str(record.response_payload["enrichment_request_id"])),
                    EnrichmentRequest.tenant_id == context.tenant_id,
                )
            )
            if request is None:
                raise SourcingError("enrichment_request_not_found")
            return request, False
        reservation_key = f"on-demand:{record.id}"
        self.reserve_usage(
            context,
            run.id,
            provider="apollo",
            endpoint="people_bulk_match",
            reservation_key=reservation_key,
            requested_units={"enrichments": 1, "estimated_credits": 9},
        )
        request = EnrichmentRequest(
            tenant_id=context.tenant_id,
            run_id=run.id,
            provider="apollo",
            candidate_ids=[str(run_candidate.candidate_id)],
            reservation_key=reservation_key,
            status="queued",
        )
        self.session.add(request)
        run_candidate.enrichment_status = "pending"
        self.session.flush()
        self._audit.record(
            tenant_id=context.tenant_id,
            run_id=run.id,
            actor_user_id=context.user_id,
            event_key=f"on-demand-enrichment-queued:{record.id}",
            action="candidate.enrichment_queued",
            entity_type="enrichment_request",
            entity_id=request.id,
            payload={"run_candidate_id": str(run_candidate.id)},
        )
        self._complete(record, {"enrichment_request_id": str(request.id)})
        return request, True

    def list_notifications(self, context: RequestContext) -> list[TenantNotification]:
        return list(
            self.session.scalars(
                select(TenantNotification)
                .where(
                    TenantNotification.tenant_id == context.tenant_id,
                    TenantNotification.audience_role == context.role.value,
                )
                .order_by(
                    TenantNotification.acknowledged_at.asc().nulls_first(),
                    TenantNotification.created_at.desc(),
                    TenantNotification.id,
                )
            )
        )

    def acknowledge_notification(
        self,
        context: RequestContext,
        notification_id: UUID,
        *,
        idempotency_key: str,
    ) -> TenantNotification:
        notification = self.session.scalar(
            select(TenantNotification)
            .where(
                TenantNotification.id == notification_id,
                TenantNotification.tenant_id == context.tenant_id,
                TenantNotification.audience_role == context.role.value,
            )
            .with_for_update()
        )
        if notification is None:
            raise SourcingError("notification_not_found")
        record = self._begin(
            context,
            f"acknowledge_notification:{notification_id}",
            idempotency_key,
            {"notification_id": str(notification_id)},
        )
        if record.response_payload is not None:
            return notification
        if notification.acknowledged_at is None:
            notification.acknowledged_at = datetime.now(UTC)
            notification.acknowledged_by_user_id = context.user_id
            self._audit.record(
                tenant_id=context.tenant_id,
                run_id=notification.run_id,
                actor_user_id=context.user_id,
                event_key=f"notification-acknowledged:{record.id}",
                action="tenant_notification.acknowledged",
                entity_type="tenant_notification",
                entity_id=notification.id,
                payload={"code": notification.code},
            )
        self._complete(record, {"notification_id": str(notification.id)})
        self.session.flush()
        return notification

    def reserve_usage(
        self,
        context: RequestContext,
        run_id: UUID,
        *,
        provider: str,
        endpoint: str,
        reservation_key: str,
        requested_units: dict[str, int],
    ) -> list[UsageLedger]:
        run = self.get_authorized(context, run_id, for_update=True)
        requested = self._validated_units(requested_units)
        existing = list(
            self.session.scalars(
                select(UsageLedger)
                .where(
                    UsageLedger.tenant_id == context.tenant_id,
                    UsageLedger.run_id == run.id,
                    UsageLedger.reservation_key == reservation_key,
                )
                .order_by(UsageLedger.unit_type)
                .with_for_update()
            )
        )
        if existing:
            actual = {row.unit_type: row.requested_units for row in existing}
            if actual != requested or any(
                row.provider != provider or row.endpoint != endpoint for row in existing
            ):
                raise SourcingError("usage_reservation_conflict")
            return existing

        budgets = list(
            self.session.scalars(
                select(UsageBudget)
                .where(
                    UsageBudget.tenant_id == context.tenant_id,
                    or_(UsageBudget.job_id.is_(None), UsageBudget.job_id == run.job_id),
                )
                .order_by(UsageBudget.job_id.asc().nulls_first())
                .with_for_update()
            )
        )
        for unit_type, requested_count in requested.items():
            for budget in budgets:
                cap = getattr(budget, _UNIT_CAP_COLUMNS[unit_type].key)
                if cap is None:
                    continue
                filters = [
                    UsageLedger.tenant_id == context.tenant_id,
                    UsageLedger.unit_type == unit_type,
                ]
                if budget.job_id is not None:
                    filters.append(UsageLedger.job_id == budget.job_id)
                used = int(
                    self.session.scalar(
                        select(
                            func.coalesce(
                                func.sum(
                                    case(
                                        (
                                            UsageLedger.charged_units.is_(None),
                                            UsageLedger.requested_units,
                                        ),
                                        else_=UsageLedger.charged_units,
                                    )
                                ),
                                0,
                            )
                        ).where(*filters)
                    )
                    or 0
                )
                if used + requested_count > cap:
                    self._mark_budget_exhausted(context, run, unit_type)
                    raise SourcingError("usage_budget_exhausted")

        rows = [
            UsageLedger(
                tenant_id=context.tenant_id,
                run_id=run.id,
                job_id=run.job_id,
                provider=provider,
                endpoint=endpoint,
                unit_type=unit_type,
                reservation_key=reservation_key,
                requested_units=count,
            )
            for unit_type, count in sorted(requested.items())
        ]
        self.session.add_all(rows)
        self.session.flush()
        return rows

    def reconcile_usage(
        self,
        context: RequestContext,
        run_id: UUID,
        *,
        reservation_key: str,
        charged_units: dict[str, int],
        provider_request_id: str | None,
    ) -> list[UsageLedger]:
        run = self.get_authorized(context, run_id, for_update=True)
        charged = self._validated_units(charged_units, allow_zero=True)
        rows = list(
            self.session.scalars(
                select(UsageLedger)
                .where(
                    UsageLedger.tenant_id == context.tenant_id,
                    UsageLedger.run_id == run.id,
                    UsageLedger.reservation_key == reservation_key,
                )
                .order_by(UsageLedger.unit_type)
                .with_for_update()
            )
        )
        if not rows or {row.unit_type for row in rows} != set(charged):
            raise SourcingError("usage_reservation_not_found")
        now = datetime.now(UTC)
        for row in rows:
            value = charged[row.unit_type]
            if row.charged_units is not None:
                if (
                    row.charged_units != value
                    or row.provider_request_id != provider_request_id
                ):
                    raise SourcingError("usage_reconciliation_conflict")
                continue
            row.charged_units = value
            row.provider_request_id = provider_request_id
            row.reconciled_at = now
        self.session.flush()
        return rows

    @staticmethod
    def _validated_units(
        units: dict[str, int], *, allow_zero: bool = False
    ) -> dict[str, int]:
        if not units or not set(units).issubset(_UNIT_CAP_COLUMNS):
            raise SourcingError("usage_units_invalid")
        minimum = 0 if allow_zero else 1
        if any(isinstance(value, bool) or value < minimum for value in units.values()):
            raise SourcingError("usage_units_invalid")
        return dict(units)

    def _mark_budget_exhausted(
        self, context: RequestContext, run: SourcingRun, unit_type: str
    ) -> None:
        if run.state in (RunState.SOURCING, RunState.MATCHING, RunState.ENRICHING):
            run.state = transition_run(run.state, RunState.PARTIALLY_READY)
            run.current_stage = RunState.PARTIALLY_READY.value
        run.error_code = "usage_budget_exhausted"
        run.error_message = "The configured sourcing usage budget was exhausted."
        for role in (Role.OWNER, Role.ADMIN):
            existing = self.session.scalar(
                select(TenantNotification.id).where(
                    TenantNotification.tenant_id == context.tenant_id,
                    TenantNotification.run_id == run.id,
                    TenantNotification.audience_role == role.value,
                    TenantNotification.code == "usage_budget_exhausted",
                )
            )
            if existing is None:
                self.session.add(
                    TenantNotification(
                        tenant_id=context.tenant_id,
                        run_id=run.id,
                        audience_role=role.value,
                        code="usage_budget_exhausted",
                        title="Sourcing usage budget exhausted",
                        message=(
                            "A sourcing run stopped before exceeding the configured "
                            "usage budget."
                        ),
                    )
                )
        self._audit.record(
            tenant_id=context.tenant_id,
            run_id=run.id,
            actor_user_id=context.user_id,
            event_key=f"usage-budget-exhausted:{run.id}:{unit_type}",
            action="sourcing_run.usage_budget_exhausted",
            entity_type="sourcing_run",
            entity_id=run.id,
            payload={"unit_type": unit_type},
        )
        self.session.flush()

    def _job(self, context: RequestContext, job_id: UUID, *, for_update: bool = False):
        try:
            return self._jobs.get_authorized(context, job_id, for_update=for_update)
        except JobError as error:
            raise SourcingError("run_not_found") from error

    def _begin(
        self,
        context: RequestContext,
        operation: str,
        idempotency_key: str,
        request_payload: dict[str, Any],
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
            raise SourcingError(error.code) from error

    def _complete(
        self, record: IdentityIdempotencyKey, response_payload: dict[str, Any]
    ) -> None:
        self._idempotency.complete_idempotent_mutation(record, response_payload)
