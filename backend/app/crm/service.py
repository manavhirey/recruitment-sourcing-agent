import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Text, and_, cast, delete, exists, func, or_, select, text
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent
from app.audit.service import AuditService
from app.candidates.contacts import ContactCipher, ContactService
from app.candidates.models import (
    Candidate,
    CandidateExperience,
    CandidateFieldProvenance,
    ContactPoint,
)
from app.core.errors import AppError
from app.crm.models import (
    AcceptanceSnapshot,
    ActivityEvent,
    CandidateNote,
    CandidateStage,
    JobCandidate,
    JobCandidateTag,
    Tag,
)
from app.identity.models import IdentityIdempotencyKey, Membership
from app.identity.schemas import RequestContext, Role
from app.identity.service import IdentityError, MembershipService
from app.jobs.models import Job, ScorecardCriterionRecord, ScorecardVersion
from app.jobs.service import JobError, JobService
from app.sourcing.models import RunCandidate, SourcingRun
from app.sourcing.service import EnrichmentEligibility, SourcingService
from app.sourcing.state_machine import RunState

REJECTION_REASON_CODES = frozenset(
    {
        "not_qualified",
        "compensation_mismatch",
        "location_mismatch",
        "work_authorization",
        "duplicate",
        "other",
    }
)

_ALLOWED_TRANSITIONS = {
    CandidateStage.NEW: frozenset(
        {
            CandidateStage.REVIEWED,
            CandidateStage.SHORTLISTED,
            CandidateStage.REJECTED,
        }
    ),
    CandidateStage.REVIEWED: frozenset(
        {CandidateStage.SHORTLISTED, CandidateStage.REJECTED}
    ),
    CandidateStage.SHORTLISTED: frozenset(
        {CandidateStage.REVIEWED, CandidateStage.REJECTED}
    ),
    CandidateStage.REJECTED: frozenset({CandidateStage.REVIEWED}),
}
_PUBLIC_ACTIVITY_ACTIONS = frozenset(
    {
        "candidate.match_materialized",
        "candidate.match_rescored",
        "candidate.stage_changed",
        "candidate.note_added",
        "candidate.owner_changed",
        "candidate.tags_changed",
        "candidate.contact_revealed",
        "candidate.enrichment_queued",
        "candidate.shortlist_exported",
    }
)


class CrmError(AppError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AcceptanceReport:
    denominator: int
    accepted: int
    reviewed: int
    shortlisted: int
    new: int
    rejected: int
    rate: float
    ready_at: datetime
    final_at: datetime
    final: bool


@dataclass(frozen=True)
class MandatoryGap:
    key: str
    label: str
    state: str
    summary: str


class CrmService:
    def __init__(
        self,
        session: Session,
        hmac_key: bytes,
        contact_cipher: ContactCipher | None = None,
    ) -> None:
        self.session = session
        self._hmac_key = hmac_key
        self._contact_cipher = contact_cipher
        self._jobs = JobService(session, hmac_key)
        self._sourcing = SourcingService(session, hmac_key)
        self._idempotency = MembershipService(session, hmac_key)
        self._audit = AuditService(session)

    def list_job_candidates(
        self,
        context: RequestContext,
        job_id: UUID,
        *,
        classification: str = "main",
        sort: str = "-score",
        score_min: int | None = None,
        score_max: int | None = None,
        stage: CandidateStage | None = None,
        owner_user_id: UUID | None = None,
        tags: tuple[str, ...] = (),
        location: str | None = None,
        industry: str | None = None,
        has_contact: bool | None = None,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[tuple[JobCandidate, Candidate]], str | None]:
        try:
            self._jobs.get_authorized(context, job_id)
        except JobError as error:
            raise CrmError("job_candidate_not_found") from error
        if classification not in {"main", "near_match"}:
            raise CrmError("classification_invalid")
        if sort not in {"-score", "score"}:
            raise CrmError("sort_invalid")
        statement = (
            select(JobCandidate, Candidate)
            .join(
                Candidate,
                and_(
                    Candidate.tenant_id == JobCandidate.tenant_id,
                    Candidate.id == JobCandidate.candidate_id,
                ),
            )
            .where(
                JobCandidate.tenant_id == context.tenant_id,
                JobCandidate.job_id == job_id,
                JobCandidate.classification == classification,
            )
        )
        if score_min is not None:
            statement = statement.where(JobCandidate.score >= score_min)
        if score_max is not None:
            statement = statement.where(JobCandidate.score <= score_max)
        if stage is not None:
            statement = statement.where(JobCandidate.stage == stage)
        if owner_user_id is not None:
            statement = statement.where(JobCandidate.owner_user_id == owner_user_id)
        normalized_tags = tuple(
            sorted({value.strip().casefold() for value in tags if value.strip()})
        )
        if normalized_tags:
            statement = statement.where(
                exists(
                    select(JobCandidateTag.id)
                    .join(
                        Tag,
                        and_(
                            Tag.tenant_id == JobCandidateTag.tenant_id,
                            Tag.id == JobCandidateTag.tag_id,
                        ),
                    )
                    .where(
                        JobCandidateTag.tenant_id == context.tenant_id,
                        JobCandidateTag.job_candidate_id == JobCandidate.id,
                        Tag.normalized_name.in_(normalized_tags),
                    )
                )
            )
        if location:
            statement = statement.where(
                Candidate.normalized_location.ilike(f"%{_normalize_query(location)}%")
            )
        if industry:
            statement = statement.where(
                cast(Candidate.industry_codes, Text).ilike(
                    f'%"{industry.strip().casefold()}"%'
                )
            )
        current_time = datetime.now(UTC)
        contact_exists = exists(
            select(ContactPoint.id).where(
                ContactPoint.tenant_id == context.tenant_id,
                ContactPoint.candidate_id == Candidate.id,
                ContactPoint.expires_at > current_time,
                ContactPoint.value_ciphertext.is_not(None),
                ContactPoint.verification_state != "expired",
            )
        )
        if has_contact is not None:
            statement = statement.where(
                contact_exists if has_contact else ~contact_exists
            )
        if query and query.strip():
            statement = statement.where(self._search_predicate(context, query))
        descending = sort == "-score"
        cursor_scope = self._cursor_scope(
            context,
            "job-candidates",
            {
                "job_id": str(job_id),
                "classification": classification,
                "sort": sort,
                "score_min": score_min,
                "score_max": score_max,
                "stage": stage.value if stage is not None else None,
                "owner_user_id": (
                    str(owner_user_id) if owner_user_id is not None else None
                ),
                "tags": normalized_tags,
                "location": _normalize_query(location) if location else None,
                "industry": industry.strip().casefold() if industry else None,
                "has_contact": has_contact,
                "query": _normalize_query(query) if query and query.strip() else None,
            },
        )
        if cursor is not None:
            values = self._decode_cursor(cursor, cursor_scope)
            try:
                cursor_score = int(str(values["score"]))
                cursor_id = UUID(str(values["id"]))
            except (KeyError, TypeError, ValueError) as error:
                raise CrmError("cursor_invalid") from error
            score_comparison = (
                JobCandidate.score < cursor_score
                if descending
                else JobCandidate.score > cursor_score
            )
            statement = statement.where(
                or_(
                    score_comparison,
                    and_(
                        JobCandidate.score == cursor_score,
                        JobCandidate.id > cursor_id,
                    ),
                )
            )
        statement = statement.order_by(
            JobCandidate.score.desc() if descending else JobCandidate.score.asc(),
            JobCandidate.id.asc(),
        ).limit(limit + 1)
        rows = list(self.session.execute(statement).tuples())
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1][0]
            next_cursor = self._encode_cursor(
                cursor_scope,
                {"score": last.score, "id": str(last.id)},
            )
            rows = rows[:limit]
        return rows, next_cursor

    def tags_for(self, context: RequestContext, job_candidate_id: UUID) -> list[str]:
        return list(
            self.session.scalars(
                select(Tag.name)
                .join(
                    JobCandidateTag,
                    and_(
                        JobCandidateTag.tenant_id == Tag.tenant_id,
                        JobCandidateTag.tag_id == Tag.id,
                    ),
                )
                .where(
                    Tag.tenant_id == context.tenant_id,
                    JobCandidateTag.job_candidate_id == job_candidate_id,
                )
                .order_by(func.lower(Tag.name), Tag.id)
            )
        )

    def has_contact(self, context: RequestContext, candidate_id: UUID) -> bool:
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        ContactPoint.tenant_id == context.tenant_id,
                        ContactPoint.candidate_id == candidate_id,
                        ContactPoint.expires_at > datetime.now(UTC),
                        ContactPoint.value_ciphertext.is_not(None),
                        ContactPoint.verification_state != "expired",
                    )
                )
            )
        )

    def masked_contacts(
        self, context: RequestContext, candidate_id: UUID
    ) -> list[ContactPoint]:
        return list(
            self.session.scalars(
                select(ContactPoint)
                .where(
                    ContactPoint.tenant_id == context.tenant_id,
                    ContactPoint.candidate_id == candidate_id,
                    ContactPoint.expires_at > datetime.now(UTC),
                    ContactPoint.value_ciphertext.is_not(None),
                    ContactPoint.verification_state != "expired",
                )
                .order_by(
                    ContactPoint.kind, ContactPoint.classification, ContactPoint.id
                )
            )
        )

    def candidate_experiences(
        self, context: RequestContext, candidate_id: UUID
    ) -> list[CandidateExperience]:
        return list(
            self.session.scalars(
                select(CandidateExperience)
                .where(
                    CandidateExperience.tenant_id == context.tenant_id,
                    CandidateExperience.candidate_id == candidate_id,
                )
                .order_by(CandidateExperience.position, CandidateExperience.id)
            )
        )

    def candidate_provenance(
        self, context: RequestContext, candidate_id: UUID
    ) -> list[CandidateFieldProvenance]:
        return list(
            self.session.scalars(
                select(CandidateFieldProvenance)
                .where(
                    CandidateFieldProvenance.tenant_id == context.tenant_id,
                    CandidateFieldProvenance.candidate_id == candidate_id,
                    CandidateFieldProvenance.is_current.is_(True),
                )
                .order_by(
                    CandidateFieldProvenance.field_name,
                    CandidateFieldProvenance.id,
                )
            )
        )

    def notes_for(
        self, context: RequestContext, job_candidate_id: UUID
    ) -> list[CandidateNote]:
        return list(
            self.session.scalars(
                select(CandidateNote)
                .where(
                    CandidateNote.tenant_id == context.tenant_id,
                    CandidateNote.job_candidate_id == job_candidate_id,
                )
                .order_by(CandidateNote.created_at.desc(), CandidateNote.id)
            )
        )

    def run_candidate_id(
        self, context: RequestContext, row: JobCandidate
    ) -> UUID | None:
        if row.latest_run_id is None:
            return None
        return self.session.scalar(
            select(RunCandidate.id).where(
                RunCandidate.tenant_id == context.tenant_id,
                RunCandidate.run_id == row.latest_run_id,
                RunCandidate.candidate_id == row.candidate_id,
            )
        )

    def scorecard_version_number(
        self, context: RequestContext, row: JobCandidate
    ) -> int | None:
        return self.session.scalar(
            select(ScorecardVersion.version).where(
                ScorecardVersion.tenant_id == context.tenant_id,
                ScorecardVersion.id == row.scorecard_version_id,
            )
        )

    def enrichment_eligibility(
        self,
        context: RequestContext,
        run_candidate_id: UUID | None,
    ) -> EnrichmentEligibility:
        if run_candidate_id is None:
            return EnrichmentEligibility(False, None)
        return self._sourcing.on_demand_enrichment_eligibility(
            context, run_candidate_id
        )

    def mandatory_gaps(
        self, context: RequestContext, row: JobCandidate
    ) -> list[MandatoryGap]:
        if row.classification != "near_match":
            return []
        evidence = row.score_json if isinstance(row.score_json, dict) else {}
        failed_values = evidence.get("failed_must_haves")
        unknown_values = evidence.get("unknown_keys")
        failed = {
            value for value in failed_values if isinstance(value, str)
        } if isinstance(failed_values, list) else set()
        unknown = {
            value for value in unknown_values if isinstance(value, str)
        } if isinstance(unknown_values, list) else set()
        criteria = self.session.scalars(
            select(ScorecardCriterionRecord)
            .where(
                ScorecardCriterionRecord.tenant_id == context.tenant_id,
                ScorecardCriterionRecord.scorecard_version_id
                == row.scorecard_version_id,
                ScorecardCriterionRecord.kind == "must_have",
            )
            .order_by(ScorecardCriterionRecord.position, ScorecardCriterionRecord.id)
        )
        gaps: list[MandatoryGap] = []
        for criterion in criteria:
            if criterion.key in failed:
                gaps.append(
                    MandatoryGap(
                        key=criterion.key,
                        label=criterion.label,
                        state="failed",
                        summary=f"Stored evidence does not support {criterion.label}.",
                    )
                )
            elif criterion.evidence_required and criterion.key in unknown:
                gaps.append(
                    MandatoryGap(
                        key=criterion.key,
                        label=criterion.label,
                        state="unknown",
                        summary=f"Evidence for {criterion.label} is unknown.",
                    )
                )
        return gaps

    @staticmethod
    def safe_score_json(row: JobCandidate) -> dict[str, object]:
        source = row.score_json if isinstance(row.score_json, dict) else {}
        result: dict[str, object] = {}
        total = source.get("total")
        if isinstance(total, int) and not isinstance(total, bool):
            result["total"] = min(max(total, 0), 100)
        breakdown = source.get("breakdown")
        if isinstance(breakdown, dict):
            safe_breakdown: dict[str, int] = {}
            for breakdown_key in (
                "role_and_skills",
                "scope_seniority_years",
                "industry",
                "location_and_eligibility",
                "recency_and_trajectory",
            ):
                value = breakdown.get(breakdown_key)
                if isinstance(value, int) and not isinstance(value, bool):
                    safe_breakdown[breakdown_key] = min(max(value, 0), 100)
            result["breakdown"] = safe_breakdown
        criteria = source.get("criteria")
        safe_criteria: list[dict[str, object]] = []
        if isinstance(criteria, list):
            for item in criteria[:100]:
                if not isinstance(item, dict):
                    continue
                key = item.get("key")
                label = item.get("label")
                state = item.get("state")
                summary = item.get("summary")
                if not (
                    isinstance(key, str)
                    and isinstance(label, str)
                    and state in {"supported", "failed", "unknown"}
                    and isinstance(summary, str)
                ):
                    continue
                safe_item: dict[str, object] = {
                    "key": key[:64],
                    "label": label[:160],
                    "state": state,
                    "summary": summary[:500],
                }
                for points_key in ("points", "max_points"):
                    points = item.get(points_key)
                    if isinstance(points, int) and not isinstance(points, bool):
                        safe_item[points_key] = min(max(points, 0), 100)
                safe_criteria.append(safe_item)
        result["criteria"] = safe_criteria
        for key in ("failed_must_haves", "unknown_keys"):
            values = source.get(key)
            result[key] = (
                [value[:64] for value in values[:100] if isinstance(value, str)]
                if isinstance(values, list)
                else []
            )
        return result

    def candidate(self, context: RequestContext, candidate_id: UUID) -> Candidate:
        candidate = self.session.scalar(
            select(Candidate).where(
                Candidate.tenant_id == context.tenant_id,
                Candidate.id == candidate_id,
            )
        )
        if candidate is None:
            raise CrmError("job_candidate_not_found")
        return candidate

    def add_note(
        self,
        context: RequestContext,
        job_candidate_id: UUID,
        body: str,
        *,
        idempotency_key: str,
    ) -> CandidateNote:
        row = self.get_authorized(context, job_candidate_id)
        normalized = body.strip()
        if not normalized:
            raise CrmError("note_invalid")
        record = self._begin(
            context,
            f"crm_note:{row.id}",
            idempotency_key,
            {"body": normalized},
        )
        if record.response_payload is not None:
            note = self.session.scalar(
                select(CandidateNote).where(
                    CandidateNote.tenant_id == context.tenant_id,
                    CandidateNote.id
                    == UUID(str(record.response_payload["candidate_note_id"])),
                )
            )
            if note is None:
                raise CrmError("idempotency_result_missing")
            return note
        note = CandidateNote(
            tenant_id=context.tenant_id,
            job_candidate_id=row.id,
            actor_user_id=context.user_id,
            body=normalized,
        )
        self.session.add(note)
        self.session.flush()
        self._record_mutation(
            context,
            row,
            record,
            "candidate.note_added",
            {"candidate_note_id": str(note.id)},
        )
        self._complete(record, {"candidate_note_id": str(note.id)})
        return note

    def assign(
        self,
        context: RequestContext,
        job_candidate_id: UUID,
        owner_user_id: UUID | None,
        *,
        idempotency_key: str,
    ) -> JobCandidate:
        row = self.get_authorized(context, job_candidate_id)
        if owner_user_id is not None:
            membership = self.session.scalar(
                select(Membership).where(
                    Membership.tenant_id == context.tenant_id,
                    Membership.user_id == owner_user_id,
                    Membership.active.is_(True),
                )
            )
            job = self._jobs.get_authorized(context, row.job_id)
            if membership is None or (
                membership.role is Role.RECRUITER
                and (
                    membership.allowed_client_ids is None
                    or str(job.client_id) not in membership.allowed_client_ids
                )
            ):
                raise CrmError("owner_invalid")
        record = self._begin(
            context,
            f"crm_assign:{row.id}",
            idempotency_key,
            {"owner_user_id": str(owner_user_id) if owner_user_id else None},
        )
        if record.response_payload is not None:
            return row
        previous = row.owner_user_id
        row.owner_user_id = owner_user_id
        self.session.flush()
        self._record_mutation(
            context,
            row,
            record,
            "candidate.owner_changed",
            {
                "from_owner_user_id": str(previous) if previous else None,
                "to_owner_user_id": str(owner_user_id) if owner_user_id else None,
            },
        )
        self._complete(record, {"job_candidate_id": str(row.id)})
        return row

    def set_tags(
        self,
        context: RequestContext,
        job_candidate_id: UUID,
        names: list[str],
        *,
        idempotency_key: str,
    ) -> list[str]:
        row = self.get_authorized(context, job_candidate_id)
        normalized_names: dict[str, str] = {}
        for name in names:
            display = " ".join(name.split())
            if not display or len(display) > 80:
                raise CrmError("tag_invalid")
            normalized_names.setdefault(display.casefold(), display)
        if len(normalized_names) > 20:
            raise CrmError("tag_invalid")
        requested = [normalized_names[key] for key in sorted(normalized_names)]
        record = self._begin(
            context,
            f"crm_tags:{row.id}",
            idempotency_key,
            {"tags": requested},
        )
        if record.response_payload is not None:
            return self.tags_for(context, row.id)
        tags: list[Tag] = []
        for normalized, display in sorted(normalized_names.items()):
            tag = self.session.scalar(
                select(Tag).where(
                    Tag.tenant_id == context.tenant_id,
                    Tag.normalized_name == normalized,
                )
            )
            if tag is None:
                tag = Tag(
                    tenant_id=context.tenant_id,
                    name=display,
                    normalized_name=normalized,
                )
                self.session.add(tag)
                self.session.flush()
            tags.append(tag)
        self.session.execute(
            delete(JobCandidateTag).where(
                JobCandidateTag.tenant_id == context.tenant_id,
                JobCandidateTag.job_candidate_id == row.id,
            )
        )
        self.session.add_all(
            JobCandidateTag(
                tenant_id=context.tenant_id,
                job_candidate_id=row.id,
                tag_id=tag.id,
            )
            for tag in tags
        )
        self.session.flush()
        result = sorted((tag.name for tag in tags), key=str.casefold)
        self._record_mutation(
            context,
            row,
            record,
            "candidate.tags_changed",
            {"tags": result},
        )
        self._complete(record, {"job_candidate_id": str(row.id), "tags": result})
        return result

    def activity(
        self,
        context: RequestContext,
        job_candidate_id: UUID,
        *,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[ActivityEvent], str | None]:
        self.get_authorized(context, job_candidate_id)
        statement = select(ActivityEvent).where(
            ActivityEvent.tenant_id == context.tenant_id,
            ActivityEvent.job_candidate_id == job_candidate_id,
            ActivityEvent.action.in_(_PUBLIC_ACTIVITY_ACTIONS),
        )
        cursor_scope = self._cursor_scope(
            context,
            "activity",
            {"job_candidate_id": str(job_candidate_id)},
        )
        if cursor is not None:
            values = self._decode_cursor(cursor, cursor_scope)
            try:
                updated_at = datetime.fromisoformat(str(values["updated_at"]))
                activity_id = UUID(str(values["id"]))
            except (KeyError, TypeError, ValueError) as error:
                raise CrmError("cursor_invalid") from error
            statement = statement.where(
                or_(
                    ActivityEvent.updated_at > updated_at,
                    and_(
                        ActivityEvent.updated_at == updated_at,
                        ActivityEvent.id > activity_id,
                    ),
                )
            )
        events = list(
            self.session.scalars(
                statement.order_by(ActivityEvent.updated_at, ActivityEvent.id).limit(
                    limit + 1
                )
            )
        )
        next_cursor = None
        if len(events) > limit:
            last = events[limit - 1]
            next_cursor = self._encode_cursor(
                cursor_scope,
                {"updated_at": _utc(last.updated_at).isoformat(), "id": str(last.id)},
            )
            events = events[:limit]
        return events, next_cursor

    def acceptance_report(
        self,
        context: RequestContext,
        job_id: UUID,
        *,
        as_of: datetime | None = None,
    ) -> AcceptanceReport:
        try:
            self._jobs.get_authorized(context, job_id)
        except JobError as error:
            raise CrmError("job_candidate_not_found") from error
        ready_run = self.session.scalar(
            select(SourcingRun)
            .where(
                SourcingRun.tenant_id == context.tenant_id,
                SourcingRun.job_id == job_id,
                SourcingRun.state == RunState.READY,
                SourcingRun.completed_at.is_not(None),
            )
            .order_by(SourcingRun.completed_at.desc(), SourcingRun.id.desc())
            .limit(1)
        )
        if ready_run is None or ready_run.completed_at is None:
            raise CrmError("acceptance_not_ready")
        ready_time = _utc(ready_run.completed_at)
        final_at = ready_time + timedelta(days=7)
        existing_snapshot = self._acceptance_snapshot(context, job_id, ready_run.id)
        if existing_snapshot is not None:
            return self._snapshot_report(existing_snapshot, final_at)
        cohort = list(
            self.session.scalars(
                select(JobCandidate)
                .where(
                    JobCandidate.tenant_id == context.tenant_id,
                    JobCandidate.job_id == job_id,
                )
                .order_by(JobCandidate.score.desc(), JobCandidate.id)
                .limit(20)
            )
        )
        reviewed = sum(row.stage is CandidateStage.REVIEWED for row in cohort)
        shortlisted = sum(row.stage is CandidateStage.SHORTLISTED for row in cohort)
        new = sum(row.stage is CandidateStage.NEW for row in cohort)
        rejected = sum(row.stage is CandidateStage.REJECTED for row in cohort)
        accepted = reviewed + shortlisted
        effective_time = _utc(as_of or datetime.now(UTC))
        final = effective_time >= final_at or (len(cohort) == 20 and new == 0)
        if final:
            self._jobs.get_authorized(context, job_id, for_update=True)
            existing_snapshot = self._acceptance_snapshot(context, job_id, ready_run.id)
            if existing_snapshot is not None:
                return self._snapshot_report(existing_snapshot, final_at)
            snapshot = AcceptanceSnapshot(
                tenant_id=context.tenant_id,
                job_id=job_id,
                run_id=ready_run.id,
                finalized_by_user_id=context.user_id,
                ready_at=ready_time,
                finalized_at=effective_time,
                denominator=20,
                accepted_count=accepted,
                reviewed_count=reviewed,
                shortlisted_count=shortlisted,
                new_count=new,
                rejected_count=rejected,
                cohort_candidate_ids=[str(row.candidate_id) for row in cohort],
            )
            self.session.add(snapshot)
            self.session.flush()
            event_key = f"acceptance-finalized:{ready_run.id}"
            for row in cohort:
                self._activity_once(
                    context,
                    row,
                    event_key=f"{event_key}:{row.id}",
                    action="candidate.acceptance_finalized",
                    payload={"job_id": str(job_id), "run_id": str(ready_run.id)},
                )
            self._audit.record(
                tenant_id=context.tenant_id,
                run_id=ready_run.id,
                actor_user_id=context.user_id,
                event_key=event_key,
                action="job.acceptance_finalized",
                entity_type="job",
                entity_id=job_id,
                payload={
                    "denominator": 20,
                    "accepted": accepted,
                    "reviewed": reviewed,
                    "shortlisted": shortlisted,
                    "new": new,
                    "rejected": rejected,
                },
            )
        return AcceptanceReport(
            denominator=20,
            accepted=accepted,
            reviewed=reviewed,
            shortlisted=shortlisted,
            new=new,
            rejected=rejected,
            rate=accepted / 20,
            ready_at=ready_time,
            final_at=final_at,
            final=final,
        )

    def _acceptance_snapshot(
        self,
        context: RequestContext,
        job_id: UUID,
        run_id: UUID,
    ) -> AcceptanceSnapshot | None:
        return self.session.scalar(
            select(AcceptanceSnapshot).where(
                AcceptanceSnapshot.tenant_id == context.tenant_id,
                AcceptanceSnapshot.job_id == job_id,
                AcceptanceSnapshot.run_id == run_id,
            )
        )

    @staticmethod
    def _snapshot_report(
        snapshot: AcceptanceSnapshot,
        final_at: datetime,
    ) -> AcceptanceReport:
        return AcceptanceReport(
            denominator=snapshot.denominator,
            accepted=snapshot.accepted_count,
            reviewed=snapshot.reviewed_count,
            shortlisted=snapshot.shortlisted_count,
            new=snapshot.new_count,
            rejected=snapshot.rejected_count,
            rate=snapshot.accepted_count / snapshot.denominator,
            ready_at=_utc(snapshot.ready_at),
            final_at=final_at,
            final=True,
        )

    def directory(
        self,
        context: RequestContext,
        *,
        query: str | None,
        location: str | None,
        industry: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[Candidate], str | None]:
        authorized_match = exists(
            select(JobCandidate.id)
            .join(
                Job,
                and_(
                    Job.tenant_id == JobCandidate.tenant_id,
                    Job.id == JobCandidate.job_id,
                ),
            )
            .where(
                JobCandidate.tenant_id == context.tenant_id,
                JobCandidate.candidate_id == Candidate.id,
                self._client_predicate(context, Job.client_id),
            )
        )
        statement = select(Candidate).where(
            Candidate.tenant_id == context.tenant_id,
            authorized_match,
        )
        if query and query.strip():
            statement = statement.where(self._search_predicate(context, query))
        if location:
            statement = statement.where(
                Candidate.normalized_location.ilike(f"%{_normalize_query(location)}%")
            )
        if industry:
            statement = statement.where(
                cast(Candidate.industry_codes, Text).ilike(
                    f'%"{industry.strip().casefold()}"%'
                )
            )
        cursor_scope = self._cursor_scope(
            context,
            "directory",
            {
                "query": _normalize_query(query) if query and query.strip() else None,
                "location": _normalize_query(location) if location else None,
                "industry": industry.strip().casefold() if industry else None,
            },
        )
        if cursor is not None:
            values = self._decode_cursor(cursor, cursor_scope)
            try:
                updated_at = datetime.fromisoformat(str(values["updated_at"]))
                candidate_id = UUID(str(values["id"]))
            except (KeyError, TypeError, ValueError) as error:
                raise CrmError("cursor_invalid") from error
            statement = statement.where(
                or_(
                    Candidate.updated_at > updated_at,
                    and_(
                        Candidate.updated_at == updated_at,
                        Candidate.id > candidate_id,
                    ),
                )
            )
        candidates = list(
            self.session.scalars(
                statement.order_by(Candidate.updated_at, Candidate.id).limit(limit + 1)
            )
        )
        next_cursor = None
        if len(candidates) > limit:
            last = candidates[limit - 1]
            next_cursor = self._encode_cursor(
                cursor_scope,
                {"updated_at": _utc(last.updated_at).isoformat(), "id": str(last.id)},
            )
            candidates = candidates[:limit]
        return candidates, next_cursor

    def candidate_jobs(
        self, context: RequestContext, candidate_id: UUID
    ) -> list[tuple[JobCandidate, Job]]:
        rows = list(
            self.session.execute(
                select(JobCandidate, Job)
                .join(
                    Job,
                    and_(
                        Job.tenant_id == JobCandidate.tenant_id,
                        Job.id == JobCandidate.job_id,
                    ),
                )
                .where(
                    JobCandidate.tenant_id == context.tenant_id,
                    JobCandidate.candidate_id == candidate_id,
                    self._client_predicate(context, Job.client_id),
                )
                .order_by(JobCandidate.updated_at.desc(), JobCandidate.id)
            ).tuples()
        )
        if not rows:
            raise CrmError("candidate_not_found")
        return rows

    def reveal_contact(
        self,
        context: RequestContext,
        contact_point_id: UUID,
        *,
        idempotency_key: str,
    ) -> str:
        if self._contact_cipher is None:
            raise CrmError("contact_reveal_unavailable")
        contact = self.session.scalar(
            select(ContactPoint).where(
                ContactPoint.tenant_id == context.tenant_id,
                ContactPoint.id == contact_point_id,
            )
        )
        if contact is None:
            raise CrmError("contact_point_not_found")
        try:
            rows = self.candidate_jobs(context, contact.candidate_id)
        except CrmError as error:
            raise CrmError("contact_point_not_found") from error
        row, _ = rows[0]
        record = self._begin(
            context,
            f"crm_contact_reveal:{contact_point_id}",
            idempotency_key,
            {"contact_point_id": str(contact_point_id)},
        )
        replayed = record.response_payload is not None
        try:
            value = ContactService(self.session, self._contact_cipher).reveal(
                context,
                contact_point_id,
                record_use=not replayed,
            )
        except LookupError as error:
            self._complete(
                record,
                {"contact_point_id": str(contact_point_id), "status": "expired"},
            )
            raise CrmError("contact_expired") from error
        if replayed:
            return value
        self._activity_once(
            context,
            row,
            event_key=f"contact-revealed:{record.id}",
            action="candidate.contact_revealed",
            payload={"contact_point_id": str(contact_point_id)},
        )
        self._audit.record(
            tenant_id=context.tenant_id,
            run_id=row.latest_run_id,
            actor_user_id=context.user_id,
            event_key=f"contact-revealed:{record.id}",
            action="candidate.contact_revealed",
            entity_type="contact_point",
            entity_id=contact_point_id,
            payload={"job_candidate_id": str(row.id)},
        )
        self._complete(record, {"contact_point_id": str(contact_point_id)})
        return value

    def begin_export(
        self,
        context: RequestContext,
        job_id: UUID,
        *,
        idempotency_key: str,
    ) -> tuple[IdentityIdempotencyKey, bool]:
        try:
            self._jobs.get_authorized(context, job_id)
        except JobError as error:
            raise CrmError("job_candidate_not_found") from error
        record = self._begin(
            context,
            f"crm_export:{job_id}",
            idempotency_key,
            {"job_id": str(job_id), "format": "csv", "stage": "Shortlisted"},
        )
        replayed = record.response_payload is not None
        if not replayed:
            self._audit.record(
                tenant_id=context.tenant_id,
                actor_user_id=context.user_id,
                event_key=f"shortlist-exported:{record.id}",
                action="candidate.shortlist_export_started",
                entity_type="job",
                entity_id=job_id,
                payload={"stage": CandidateStage.SHORTLISTED.value, "format": "csv"},
            )
            self._complete(record, {"job_id": str(job_id), "status": "authorized"})
        return record, replayed

    def record_exported_candidate(
        self,
        context: RequestContext,
        row: JobCandidate,
        export_record: IdentityIdempotencyKey,
    ) -> None:
        self._activity_once(
            context,
            row,
            event_key=f"shortlist-exported:{export_record.id}:{row.id}",
            action="candidate.shortlist_exported",
            payload={"job_id": str(row.job_id), "format": "csv"},
        )

    def record_exported_contact(
        self,
        context: RequestContext,
        row: JobCandidate,
        export_record: IdentityIdempotencyKey,
        contact_point_id: UUID,
    ) -> None:
        event_key = self._exported_contact_event_key(
            export_record, row, contact_point_id
        )
        self._audit.record(
            tenant_id=context.tenant_id,
            run_id=row.latest_run_id,
            actor_user_id=context.user_id,
            event_key=event_key,
            action="candidate.contact_exported",
            entity_type="contact_point",
            entity_id=contact_point_id,
            payload={"job_candidate_id": str(row.id), "job_id": str(row.job_id)},
        )

    def exported_contact_recorded(
        self,
        context: RequestContext,
        row: JobCandidate,
        export_record: IdentityIdempotencyKey,
        contact_point_id: UUID,
    ) -> bool:
        event_key = self._exported_contact_event_key(
            export_record, row, contact_point_id
        )
        if self.session.get_bind().dialect.name == "postgresql":
            lock_digest = hashlib.sha256(
                f"audit-event\0{context.tenant_id}\0{event_key}".encode()
            ).digest()
            self.session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": int.from_bytes(lock_digest[:8], "big", signed=True)},
            )
        return (
            self.session.scalar(
                select(AuditEvent.id).where(
                    AuditEvent.tenant_id == context.tenant_id,
                    AuditEvent.event_key == event_key,
                )
            )
            is not None
        )

    @staticmethod
    def _exported_contact_event_key(
        export_record: IdentityIdempotencyKey,
        row: JobCandidate,
        contact_point_id: UUID,
    ) -> str:
        return (
            f"shortlist-contact-exported:{export_record.id}:"
            f"{row.id}:{contact_point_id}"
        )

    def record_export_outcome(
        self,
        context: RequestContext,
        job_id: UUID,
        export_record: IdentityIdempotencyKey,
        outcome: str,
    ) -> None:
        if outcome not in {"completed", "aborted"}:
            raise ValueError("export outcome must be completed or aborted")
        self._audit.record(
            tenant_id=context.tenant_id,
            actor_user_id=context.user_id,
            event_key=f"shortlist-export-{outcome}:{export_record.id}",
            action=f"candidate.shortlist_export_{outcome}",
            entity_type="job",
            entity_id=job_id,
            payload={"stage": CandidateStage.SHORTLISTED.value, "format": "csv"},
        )

    def transition(
        self,
        context: RequestContext,
        job_candidate_id: UUID,
        stage: CandidateStage | str,
        *,
        reason_code: str | None,
        note: str | None,
        idempotency_key: str,
    ) -> JobCandidate:
        target = CandidateStage(stage)
        if target is CandidateStage.REJECTED:
            if reason_code is None:
                raise CrmError("rejection_reason_required")
            if reason_code not in REJECTION_REASON_CODES:
                raise CrmError("rejection_reason_invalid")
        elif reason_code is not None or note is not None:
            raise CrmError("rejection_details_invalid")
        normalized_note = note.strip() if note else None
        if normalized_note == "":
            normalized_note = None

        row = self.get_authorized(context, job_candidate_id)
        record = self._begin(
            context,
            f"crm_transition:{job_candidate_id}",
            idempotency_key,
            {
                "stage": target.value,
                "reason_code": reason_code,
                "note": normalized_note,
            },
        )
        if record.response_payload is not None:
            return row

        row = self.get_authorized(context, job_candidate_id, for_update=True)
        previous = row.stage
        if target not in _ALLOWED_TRANSITIONS[previous]:
            raise CrmError("stage_transition_invalid")
        row.stage = target
        row.rejection_reason_code = reason_code
        row.rejection_note = normalized_note
        self.session.flush()
        self._activity(
            context,
            row,
            event_key=f"crm-stage:{record.id}",
            action="candidate.stage_changed",
            payload={
                "from_stage": previous.value,
                "to_stage": target.value,
                "reason_code": reason_code,
            },
        )
        self._audit.record(
            tenant_id=context.tenant_id,
            actor_user_id=context.user_id,
            event_key=f"crm-stage:{record.id}",
            action="candidate.stage_changed",
            entity_type="job_candidate",
            entity_id=row.id,
            payload={
                "from_stage": previous.value,
                "to_stage": target.value,
                "reason_code": reason_code,
            },
        )
        self._complete(record, {"job_candidate_id": str(row.id)})
        return row

    def materialize_run_matches(
        self,
        run: SourcingRun,
        context: RequestContext,
    ) -> list[JobCandidate]:
        if (
            run.tenant_id != context.tenant_id
            or run.started_by_user_id != context.user_id
        ):
            raise CrmError("run_not_found")
        try:
            self._jobs.get_authorized(context, run.job_id)
        except JobError as error:
            raise CrmError("run_not_found") from error
        matches = list(
            self.session.scalars(
                select(RunCandidate)
                .where(
                    RunCandidate.tenant_id == context.tenant_id,
                    RunCandidate.run_id == run.id,
                    RunCandidate.match_score.is_not(None),
                    RunCandidate.classification.is_not(None),
                    RunCandidate.scoring_version.is_not(None),
                )
                .order_by(RunCandidate.candidate_id, RunCandidate.id)
            )
        )
        materialized: list[JobCandidate] = []
        for match in matches:
            match_score = match.match_score
            classification = match.classification
            scoring_version = match.scoring_version
            if match_score is None or classification is None or scoring_version is None:
                continue
            self._lock_materialization(run, match.candidate_id)
            row = self.session.scalar(
                select(JobCandidate)
                .where(
                    JobCandidate.tenant_id == context.tenant_id,
                    JobCandidate.job_id == run.job_id,
                    JobCandidate.candidate_id == match.candidate_id,
                )
                .with_for_update()
            )
            created = row is None
            if row is None:
                row = JobCandidate(
                    tenant_id=context.tenant_id,
                    job_id=run.job_id,
                    candidate_id=match.candidate_id,
                    latest_run_id=run.id,
                    classification=classification,
                    score=match_score,
                    score_json=match.evidence or {},
                    scorecard_version_id=match.scorecard_version_id,
                    scoring_version=scoring_version,
                )
                self.session.add(row)
            else:
                row.latest_run_id = run.id
                row.classification = classification
                row.score = match_score
                row.score_json = match.evidence or {}
                row.scorecard_version_id = match.scorecard_version_id
                row.scoring_version = scoring_version
            self.session.flush()
            event_key = f"match-materialized:{run.id}:{match.id}"
            self._activity_once(
                context,
                row,
                event_key=event_key,
                action=(
                    "candidate.match_materialized"
                    if created
                    else "candidate.match_rescored"
                ),
                payload={
                    "run_id": str(run.id),
                    "score": row.score,
                    "classification": row.classification,
                },
            )
            self._audit.record(
                tenant_id=context.tenant_id,
                run_id=run.id,
                actor_user_id=context.user_id,
                event_key=event_key,
                action=(
                    "candidate.match_materialized"
                    if created
                    else "candidate.match_rescored"
                ),
                entity_type="job_candidate",
                entity_id=row.id,
                payload={
                    "candidate_id": str(row.candidate_id),
                    "score": row.score,
                    "classification": row.classification,
                },
            )
            materialized.append(row)
        return materialized

    def get_authorized(
        self,
        context: RequestContext,
        job_candidate_id: UUID,
        *,
        for_update: bool = False,
    ) -> JobCandidate:
        statement = select(JobCandidate).where(
            JobCandidate.id == job_candidate_id,
            JobCandidate.tenant_id == context.tenant_id,
        )
        if for_update:
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        row = self.session.scalar(statement)
        if row is None:
            raise CrmError("job_candidate_not_found")
        try:
            self._jobs.get_authorized(context, row.job_id)
        except JobError as error:
            raise CrmError("job_candidate_not_found") from error
        return row

    def _activity(
        self,
        context: RequestContext,
        row: JobCandidate,
        *,
        event_key: str,
        action: str,
        payload: dict[str, object],
    ) -> ActivityEvent:
        event = ActivityEvent(
            tenant_id=context.tenant_id,
            job_candidate_id=row.id,
            actor_user_id=context.user_id,
            event_key=event_key,
            action=action,
            payload=payload,
            updated_at=datetime.now(UTC),
        )
        self.session.add(event)
        self.session.flush()
        return event

    def _record_mutation(
        self,
        context: RequestContext,
        row: JobCandidate,
        record: IdentityIdempotencyKey,
        action: str,
        payload: dict[str, object],
    ) -> None:
        event_key = f"{action}:{record.id}"
        self._activity_once(
            context,
            row,
            event_key=event_key,
            action=action,
            payload=payload,
        )
        self._audit.record(
            tenant_id=context.tenant_id,
            run_id=row.latest_run_id,
            actor_user_id=context.user_id,
            event_key=event_key,
            action=action,
            entity_type="job_candidate",
            entity_id=row.id,
            payload=payload,
        )

    def _activity_once(
        self,
        context: RequestContext,
        row: JobCandidate,
        *,
        event_key: str,
        action: str,
        payload: dict[str, object],
    ) -> ActivityEvent:
        existing = self.session.scalar(
            select(ActivityEvent).where(
                ActivityEvent.tenant_id == context.tenant_id,
                ActivityEvent.event_key == event_key,
            )
        )
        if existing is not None:
            return existing
        return self._activity(
            context,
            row,
            event_key=event_key,
            action=action,
            payload=payload,
        )

    def _lock_materialization(self, run: SourcingRun, candidate_id: UUID) -> None:
        if self.session.get_bind().dialect.name != "postgresql":
            return
        digest = hashlib.sha256(
            f"crm-match\0{run.tenant_id}\0{run.job_id}\0{candidate_id}".encode()
        ).digest()
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": int.from_bytes(digest[:8], "big", signed=True)},
        )

    def _search_predicate(self, context: RequestContext, query: str):
        normalized = _normalize_query(query)
        is_postgresql = self.session.get_bind().dialect.name == "postgresql"
        document = (
            func.coalesce(Candidate.normalized_name, "")
            + " "
            + func.coalesce(Candidate.normalized_title, "")
            + " "
            + func.coalesce(Candidate.normalized_company, "")
            + " "
            + cast(Candidate.normalized_skills, Text)
        )
        experience_document = (
            func.coalesce(CandidateExperience.title, "")
            + " "
            + func.coalesce(CandidateExperience.company_name, "")
        )
        if is_postgresql:
            experience_predicate = or_(
                func.to_tsvector("simple", experience_document).op("@@")(
                    func.websearch_to_tsquery("simple", normalized)
                ),
                CandidateExperience.title.op("%")(normalized),
                CandidateExperience.company_name.op("%")(normalized),
            )
        else:
            experience_predicate = or_(
                func.lower(CandidateExperience.title).contains(normalized),
                func.lower(CandidateExperience.company_name).contains(normalized),
            )
        experience_match = exists(
            select(CandidateExperience.id).where(
                CandidateExperience.tenant_id == context.tenant_id,
                CandidateExperience.candidate_id == Candidate.id,
                experience_predicate,
            )
        )
        if is_postgresql:
            full_text = func.to_tsvector("simple", document).op("@@")(
                func.websearch_to_tsquery("simple", normalized)
            )
            trigram = or_(
                Candidate.normalized_name.op("%")(normalized),
                Candidate.normalized_title.op("%")(normalized),
                Candidate.normalized_company.op("%")(normalized),
            )
            return or_(full_text, trigram, experience_match)
        return or_(
            func.lower(document).contains(normalized),
            experience_match,
        )

    def _client_predicate(self, context: RequestContext, client_column):
        if context.role is not Role.RECRUITER:
            return client_column.is_not(None)
        allowed_client_ids = context.allowed_client_ids or frozenset()
        if not allowed_client_ids:
            return client_column.in_([])
        return client_column.in_(tuple(allowed_client_ids))

    def _encode_cursor(self, scope: str, values: dict[str, object]) -> str:
        payload = json.dumps(
            {"scope": scope, **values}, sort_keys=True, separators=(",", ":")
        ).encode()
        signature = hmac.digest(self._hmac_key, b"crm-cursor-v1\0" + payload, "sha256")
        return f"{_b64(payload)}.{_b64(signature)}"

    def _cursor_scope(
        self,
        context: RequestContext,
        namespace: str,
        values: dict[str, object],
    ) -> str:
        grants = (
            sorted(str(value) for value in context.allowed_client_ids)
            if context.allowed_client_ids is not None
            else None
        )
        payload = json.dumps(
            {
                "tenant_id": str(context.tenant_id),
                "user_id": str(context.user_id),
                "role": context.role.value,
                "allowed_client_ids": grants,
                **values,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return f"{namespace}:{hashlib.sha256(payload).hexdigest()}"

    def _decode_cursor(self, cursor: str, scope: str) -> dict[str, object]:
        try:
            payload_token, signature_token = cursor.split(".", 1)
            payload = _unb64(payload_token)
            supplied_signature = _unb64(signature_token)
            expected_signature = hmac.digest(
                self._hmac_key, b"crm-cursor-v1\0" + payload, "sha256"
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise ValueError
            values = json.loads(payload)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CrmError("cursor_invalid") from error
        if not isinstance(values, dict) or values.pop("scope", None) != scope:
            raise CrmError("cursor_invalid")
        return values

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
            raise CrmError(error.code) from error

    def _complete(
        self,
        record: IdentityIdempotencyKey,
        response_payload: dict[str, Any],
    ) -> None:
        self._idempotency.complete_idempotent_mutation(record, response_payload)


def materialize_run_matches(
    session: Session,
    run: SourcingRun,
    context: RequestContext,
) -> list[JobCandidate]:
    return CrmService(session, b"internal-worker").materialize_run_matches(run, context)


def _normalize_query(value: str) -> str:
    return " ".join(value.casefold().split())


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
