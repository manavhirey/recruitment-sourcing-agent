from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clients.service import ClientError, ClientService
from app.core.errors import AppError
from app.identity.models import IdentityIdempotencyKey
from app.identity.schemas import RequestContext, Role
from app.identity.service import IdentityError, MembershipService
from app.jobs.llm import ScorecardExtractionError, ScorecardGateway
from app.jobs.models import Job, ScorecardCriterionRecord, ScorecardVersion
from app.jobs.schemas import (
    ClientContext,
    ConfirmedScorecard,
    CriterionKind,
    EditableScorecardDraft,
    ExtractionStatus,
    ScorecardCriterion,
    ScorecardDraft,
    ScorecardDraftResponse,
    SeniorityOption,
)
from app.jobs.seniority import SENIORITY_PRESETS


class JobError(AppError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class JobService:
    def __init__(
        self,
        session: Session,
        hmac_key: bytes,
        scorecard_gateway: ScorecardGateway | None = None,
    ) -> None:
        self.session = session
        self._clients = ClientService(session, hmac_key)
        self._idempotency = MembershipService(session, hmac_key)
        self._scorecard_gateway = scorecard_gateway

    def create(
        self,
        context: RequestContext,
        *,
        client_id: UUID,
        title: str,
        job_description: str,
        idempotency_key: str,
        location: str | None = None,
        employment_model: str | None = None,
    ) -> Job:
        self._authorize_client(context, client_id)
        normalized_title = title.strip()
        normalized_description = job_description.strip()
        if not normalized_title or not normalized_description:
            raise JobError("job_intake_invalid")
        record = self._begin(
            context,
            "create_job",
            idempotency_key,
            {
                "client_id": str(client_id),
                "title": normalized_title,
                "job_description": normalized_description,
                "location": location,
                "employment_model": employment_model,
            },
        )
        if record.response_payload is not None:
            return self.get_authorized(
                context, UUID(str(record.response_payload["job_id"]))
            )
        job = Job(
            id=uuid4(),
            tenant_id=context.tenant_id,
            client_id=client_id,
            owner_user_id=context.user_id,
            title=normalized_title,
            job_description=normalized_description,
            location=location.strip() if location else None,
            employment_model=employment_model.strip() if employment_model else None,
            status="awaiting_scorecard",
            draft_revision=0,
        )
        self.session.add(job)
        self.session.flush()
        self._complete(record, {"job_id": str(job.id)})
        return job

    def get_authorized(
        self,
        context: RequestContext,
        job_id: UUID,
        *,
        for_update: bool = False,
    ) -> Job:
        statement = select(Job).where(
            Job.id == job_id,
            Job.tenant_id == context.tenant_id,
        )
        if for_update:
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        job = self.session.scalar(statement)
        if job is None:
            raise JobError("job_not_found")
        self._authorize_client(context, job.client_id)
        return job

    def list_authorized(
        self,
        context: RequestContext,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[Job], int | None]:
        statement = select(Job).where(Job.tenant_id == context.tenant_id)
        if context.role is Role.RECRUITER:
            allowed_client_ids = context.allowed_client_ids or frozenset()
            statement = statement.where(Job.client_id.in_(allowed_client_ids))
        rows = list(
            self.session.scalars(
                statement.order_by(Job.created_at.desc(), Job.id.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        )
        has_more = len(rows) > limit
        return rows[:limit], offset + limit if has_more else None

    def generate_draft(
        self,
        context: RequestContext,
        job_id: UUID,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> ScorecardDraftResponse:
        job = self.get_authorized(context, job_id)
        record = self._begin(
            context,
            f"generate_scorecard:{job_id}",
            idempotency_key,
            {"expected_revision": expected_revision},
        )
        if record.response_payload is not None:
            return ScorecardDraftResponse.model_validate(record.response_payload)
        self._check_revision(job, expected_revision)
        if self._scorecard_gateway is None:
            raise JobError("scorecard_gateway_unavailable")
        client = self._authorize_client(context, job.client_id)
        industries = tuple(self._clients.industries_for(client))
        approved_adjacencies = tuple(
            sorted({target for _, target in self._clients.adjacencies_for(client)})
        )
        draft_payload: dict[str, Any] | None
        extraction_warning: str | None
        try:
            draft = self._scorecard_gateway.extract(
                job.job_description,
                ClientContext(
                    client_id=client.id,
                    industry_codes=industries,
                    approved_adjacent_industries=approved_adjacencies,
                ),
            )
        except ScorecardExtractionError:
            draft_payload = None
            extraction_status = ExtractionStatus.MANUAL_REQUIRED.value
            extraction_warning = (
                "Automatic extraction failed twice. Enter and confirm the scorecard "
                "manually."
            )
        else:
            draft_payload = draft.model_dump(mode="json")
            extraction_status = ExtractionStatus.READY.value
            extraction_warning = None
        job = self.get_authorized(context, job_id, for_update=True)
        self._check_revision(job, expected_revision)
        job.draft_payload = draft_payload
        job.draft_extraction_status = extraction_status
        job.draft_extraction_warning = extraction_warning
        job.draft_revision += 1
        self.session.flush()
        result = self._draft_response(job)
        self._complete(record, result.model_dump(mode="json"))
        return result

    def get_draft(
        self, context: RequestContext, job_id: UUID
    ) -> ScorecardDraftResponse:
        job = self.get_authorized(context, job_id)
        if job.draft_revision == 0:
            raise JobError("scorecard_draft_not_found")
        return self._draft_response(job)

    def update_draft(
        self,
        context: RequestContext,
        job_id: UUID,
        draft: ScorecardDraft,
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> ScorecardDraftResponse:
        job = self.get_authorized(context, job_id, for_update=True)
        record = self._begin(
            context,
            f"update_scorecard_draft:{job_id}",
            idempotency_key,
            {
                "expected_revision": expected_revision,
                "draft": draft.model_dump(mode="json"),
            },
        )
        if record.response_payload is not None:
            return ScorecardDraftResponse.model_validate(record.response_payload)
        self._check_revision(job, expected_revision)
        draft = self._normalize_draft_provenance(job, draft)
        job.draft_payload = draft.model_dump(mode="json")
        job.draft_revision += 1
        if job.draft_extraction_status != ExtractionStatus.MANUAL_REQUIRED.value:
            job.draft_extraction_status = ExtractionStatus.READY.value
            job.draft_extraction_warning = None
        self.session.flush()
        result = self._draft_response(job)
        self._complete(record, result.model_dump(mode="json"))
        return result

    def confirm_scorecard(
        self,
        context: RequestContext,
        job_id: UUID,
        *,
        expected_revision: int,
        idempotency_key: str | None = None,
    ) -> ConfirmedScorecard:
        job = self.get_authorized(context, job_id, for_update=True)
        record = (
            self._begin(
                context,
                f"confirm_scorecard:{job_id}",
                idempotency_key,
                {"expected_revision": expected_revision},
            )
            if idempotency_key is not None
            else None
        )
        if record is not None and record.response_payload is not None:
            return self.get_scorecard(
                context, UUID(str(record.response_payload["scorecard_id"]))
            )
        self._check_revision(job, expected_revision)
        if job.draft_payload is None:
            raise JobError("scorecard_draft_required")
        draft = ScorecardDraft.model_validate(job.draft_payload)
        scorecard = self._append_scorecard(context, job, draft)
        job.draft_revision += 1
        self.session.flush()
        if record is not None:
            self._complete(record, {"scorecard_id": str(scorecard.id)})
        return scorecard

    def revise_scorecard(
        self,
        context: RequestContext,
        job_id: UUID,
        draft: ScorecardDraft,
        *,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> ConfirmedScorecard:
        job = self.get_authorized(context, job_id, for_update=True)
        record = (
            self._begin(
                context,
                f"rescore_job:{job_id}",
                idempotency_key,
                {
                    "expected_revision": expected_revision,
                    "draft": draft.model_dump(mode="json"),
                },
            )
            if idempotency_key is not None
            else None
        )
        if record is not None and record.response_payload is not None:
            return self.get_scorecard(
                context, UUID(str(record.response_payload["scorecard_id"]))
            )
        if expected_revision is not None:
            self._check_revision(job, expected_revision)
        draft = self._normalize_draft_provenance(job, draft)
        scorecard = self._append_scorecard(context, job, draft)
        job.draft_payload = draft.model_dump(mode="json")
        job.draft_revision += 1
        self.session.flush()
        if record is not None:
            self._complete(record, {"scorecard_id": str(scorecard.id)})
        return scorecard

    def list_versions(
        self, context: RequestContext, job_id: UUID
    ) -> list[ConfirmedScorecard]:
        self.get_authorized(context, job_id)
        records = self.session.scalars(
            select(ScorecardVersion)
            .where(ScorecardVersion.job_id == job_id)
            .order_by(ScorecardVersion.version)
        )
        return [self._to_confirmed(record) for record in records]

    def get_scorecard(
        self, context: RequestContext, scorecard_id: UUID
    ) -> ConfirmedScorecard:
        record = self.session.scalar(
            select(ScorecardVersion).where(
                ScorecardVersion.id == scorecard_id,
                ScorecardVersion.tenant_id == context.tenant_id,
            )
        )
        if record is None:
            raise JobError("scorecard_not_found")
        self.get_authorized(context, record.job_id)
        return self._to_confirmed(record)

    def _append_scorecard(
        self, context: RequestContext, job: Job, draft: ScorecardDraft
    ) -> ConfirmedScorecard:
        self._validate_industries(context, job, draft)
        if draft.unresolved_inferred_items():
            raise JobError("scorecard_inferences_unresolved")
        current_version = self.session.scalar(
            select(func.max(ScorecardVersion.version)).where(
                ScorecardVersion.job_id == job.id
            )
        )
        record = ScorecardVersion(
            id=uuid4(),
            tenant_id=context.tenant_id,
            job_id=job.id,
            version=(current_version or 0) + 1,
            target_titles=draft.target_titles,
            seniority=draft.seniority,
            minimum_years=draft.minimum_years,
            maximum_years=draft.maximum_years,
            locations=draft.locations,
            industry_code=draft.industry_code,
            suggested_adjacent_industries=draft.suggested_adjacent_industries,
            uncertainties=draft.uncertainties,
            extraction_status=job.draft_extraction_status,
            confirmed_by_user_id=context.user_id,
            confirmed_at=datetime.now(UTC),
        )
        self.session.add(record)
        self.session.flush()
        self.session.add_all(
            ScorecardCriterionRecord(
                tenant_id=context.tenant_id,
                scorecard_version_id=record.id,
                position=position,
                key=criterion.key,
                label=criterion.label,
                kind=criterion.kind.value,
                evidence_required=criterion.evidence_required,
                source_text=criterion.source_text,
                inferred=criterion.inferred,
                recruiter_entered=criterion.recruiter_entered,
                lawful_requirement_confirmed=(criterion.lawful_requirement_confirmed),
            )
            for position, criterion in enumerate(draft.criteria)
        )
        job.current_scorecard_id = record.id
        self.session.flush()
        return self._to_confirmed(record)

    def _validate_industries(
        self, context: RequestContext, job: Job, draft: ScorecardDraft
    ) -> None:
        client = self._authorize_client(context, job.client_id)
        assigned = set(self._clients.industries_for(client))
        if (
            not self._clients.taxonomy.contains(draft.industry_code)
            or draft.industry_code not in assigned
        ):
            raise JobError("scorecard_industry_invalid")
        approved = {
            target
            for source, target in self._clients.adjacencies_for(client)
            if source == draft.industry_code
        }
        suggested = set(draft.suggested_adjacent_industries)
        if not all(
            self._clients.taxonomy.contains(code) for code in suggested
        ) or not suggested.issubset(approved):
            raise JobError("scorecard_adjacency_not_approved")

    @staticmethod
    def _normalize_draft_provenance(job: Job, draft: ScorecardDraft) -> ScorecardDraft:
        previous = (
            ScorecardDraft.model_validate(job.draft_payload)
            if job.draft_payload is not None
            else None
        )
        previous_criteria = previous.criteria if previous is not None else []
        used: set[int] = set()

        def semantic_content(criterion: ScorecardCriterion) -> tuple[object, ...]:
            return (
                criterion.label.strip().casefold(),
                criterion.kind,
                criterion.evidence_required,
            )

        normalized: list[ScorecardCriterion] = []
        for criterion in draft.criteria:
            match_index = next(
                (
                    index
                    for index, old in enumerate(previous_criteria)
                    if index not in used and old.key == criterion.key
                ),
                None,
            )
            if match_index is None:
                match_index = next(
                    (
                        index
                        for index, old in enumerate(previous_criteria)
                        if index not in used
                        and semantic_content(old) == semantic_content(criterion)
                    ),
                    None,
                )
            old = previous_criteria[match_index] if match_index is not None else None
            if match_index is not None:
                used.add(match_index)
            unchanged_extraction = old is not None and semantic_content(
                old
            ) == semantic_content(criterion)
            if old is not None and (old.inferred or unchanged_extraction):
                provenance = {
                    "source_text": old.source_text,
                    "inferred": old.inferred,
                    "recruiter_entered": old.recruiter_entered,
                }
            else:
                provenance = {
                    "source_text": None,
                    "inferred": False,
                    "recruiter_entered": True,
                }
            try:
                normalized.append(
                    ScorecardCriterion.model_validate(
                        {**criterion.model_dump(), **provenance}
                    )
                )
            except ValidationError as error:
                raise JobError("scorecard_criterion_invalid") from error

        payload = draft.model_dump()
        payload["criteria"] = [criterion.model_dump() for criterion in normalized]
        payload["confirmed_inferred_items"] = []
        normalized_draft = ScorecardDraft.model_validate(payload)
        valid_confirmations = sorted(
            set(draft.confirmed_inferred_items) & normalized_draft.inferred_item_ids()
        )
        return normalized_draft.model_copy(
            update={"confirmed_inferred_items": valid_confirmations}
        )

    def _to_confirmed(self, record: ScorecardVersion) -> ConfirmedScorecard:
        criteria = self.session.scalars(
            select(ScorecardCriterionRecord)
            .where(ScorecardCriterionRecord.scorecard_version_id == record.id)
            .order_by(ScorecardCriterionRecord.position)
        )
        confirmed = ConfirmedScorecard(
            target_titles=record.target_titles,
            criteria=[
                ScorecardCriterion(
                    key=criterion.key,
                    label=criterion.label,
                    kind=CriterionKind(criterion.kind),
                    evidence_required=criterion.evidence_required,
                    source_text=criterion.source_text,
                    inferred=criterion.inferred,
                    recruiter_entered=criterion.recruiter_entered,
                    lawful_requirement_confirmed=(
                        criterion.lawful_requirement_confirmed
                    ),
                )
                for criterion in criteria
            ],
            seniority=record.seniority,
            minimum_years=record.minimum_years,
            maximum_years=record.maximum_years,
            locations=record.locations,
            industry_code=record.industry_code,
            suggested_adjacent_industries=record.suggested_adjacent_industries,
            uncertainties=record.uncertainties,
            id=record.id,
            job_id=record.job_id,
            version=record.version,
            confirmed_at=record.confirmed_at,
            extraction_status=ExtractionStatus(record.extraction_status),
        )
        return confirmed.model_copy(
            update={"confirmed_inferred_items": sorted(confirmed.inferred_item_ids())}
        )

    @staticmethod
    def _draft_response(job: Job) -> ScorecardDraftResponse:
        if job.draft_payload is None:
            draft: ScorecardDraft | EditableScorecardDraft = EditableScorecardDraft()
        else:
            try:
                draft = ScorecardDraft.model_validate(job.draft_payload)
            except ValidationError:
                draft = EditableScorecardDraft.model_validate(job.draft_payload)
        return ScorecardDraftResponse(
            job_id=job.id,
            draft_revision=job.draft_revision,
            draft=draft,
            original_job_description=job.job_description,
            extraction_status=ExtractionStatus(job.draft_extraction_status),
            extraction_warning=job.draft_extraction_warning,
            seniority_options=tuple(
                SeniorityOption(
                    value=preset.value,
                    label=preset.label,
                    minimum_years=preset.minimum_years,
                    maximum_years=preset.maximum_years,
                )
                for preset in SENIORITY_PRESETS
            ),
        )

    def _authorize_client(self, context: RequestContext, client_id: UUID):
        try:
            return self._clients.get_authorized(context, client_id)
        except ClientError as error:
            raise JobError("job_not_found") from error

    @staticmethod
    def _check_revision(job: Job, expected_revision: int) -> None:
        if job.draft_revision != expected_revision:
            raise JobError("scorecard_revision_conflict")

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
            raise JobError(error.code) from error

    def _complete(
        self, record: IdentityIdempotencyKey, response_payload: dict[str, Any]
    ) -> None:
        self._idempotency.complete_idempotent_mutation(record, response_payload)
