import base64
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.candidates.contacts import ContactCipher
from app.candidates.models import Candidate, SourceIdentity
from app.clients.models import ClientCompany
from app.core.database import Base
from app.identity.models import Tenant, User
from app.identity.schemas import RequestContext, Role
from app.jobs.models import Job, ScorecardVersion
from app.providers.base import (
    EnrichmentInput,
    EnrichmentReceipt,
    EnrichmentResult,
    ProviderTemporaryError,
)
from app.providers.snapshots import SnapshotStore
from app.sourcing.enrichment import (
    RegionalContactPolicy,
    enqueue_top_enrichment,
    execute_queued_enrichment_request,
    poll_enrichment_request,
    reconcile_snapshot_references,
)
from app.sourcing.models import (
    EnrichmentRequest,
    ProviderSnapshot,
    RunCandidate,
    SourcingRun,
    UsageBudget,
    UsageLedger,
)
from app.sourcing.state_machine import RunState
from app.sourcing.webhooks import (
    CapabilityTokenCodec,
    apply_enrichment_payload,
)


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, **kwargs: object) -> None:
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = bytes(
            kwargs["Body"]
        )  # type: ignore[arg-type]

    def delete_object(self, **kwargs: object) -> None:
        self.objects.pop((str(kwargs["Bucket"]), str(kwargs["Key"])), None)

    def head_object(self, **kwargs: object) -> dict[str, object]:
        return {}

    def put_bucket_lifecycle_configuration(self, **kwargs: object) -> None:
        return None


class RecordingGateway:
    def __init__(self, factory: sessionmaker[Session], run_id: UUID) -> None:
        self.factory = factory
        self.run_id = run_id
        self.calls: list[tuple[tuple[EnrichmentInput, ...], bool, bool]] = []
        self.request_counts_at_call: list[int] = []
        self.run_states_at_call: list[RunState] = []

    def enrich_batch(
        self,
        people: tuple[EnrichmentInput, ...],
        webhook_url: str,
        *,
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
    ) -> EnrichmentReceipt:
        assert webhook_url.startswith("https://")
        assert len(people) <= 10
        with self.factory() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(UsageLedger)
                    .where(UsageLedger.run_id == self.run_id)
                )
                or 0
            ) >= 2
            self.request_counts_at_call.append(
                int(
                    session.scalar(
                        select(func.count())
                        .select_from(EnrichmentRequest)
                        .where(EnrichmentRequest.run_id == self.run_id)
                    )
                    or 0
                )
            )
            run = session.get(SourcingRun, self.run_id)
            assert run is not None
            self.run_states_at_call.append(run.state)
        self.calls.append((people, reveal_personal_emails, reveal_phone_number))
        request_id = str(100 + len(self.calls))
        return EnrichmentReceipt(
            provider="apollo",
            request_id=request_id,
            submitted_count=len(people),
            result=EnrichmentResult(
                provider="apollo",
                request_id=request_id,
                people=(),
                snapshot_payload={"request_id": int(request_id), "people": []},
            ),
            charged_units=(
                ("enrichments", len(people)),
                ("estimated_credits", len(people)),
            ),
        )


class FailingGateway(RecordingGateway):
    def enrich_batch(
        self,
        people: tuple[EnrichmentInput, ...],
        webhook_url: str,
        *,
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
    ) -> EnrichmentReceipt:
        del people, webhook_url, reveal_personal_emails, reveal_phone_number
        raise ProviderTemporaryError("sensitive provider failure")

    def poll_enrichment(self, request_id: str) -> EnrichmentResult:
        del request_id
        raise ProviderTemporaryError("sensitive provider failure")


class CancellingGateway(RecordingGateway):
    def enrich_batch(
        self,
        people: tuple[EnrichmentInput, ...],
        webhook_url: str,
        *,
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
    ) -> EnrichmentReceipt:
        receipt = super().enrich_batch(
            people,
            webhook_url,
            reveal_personal_emails=reveal_personal_emails,
            reveal_phone_number=reveal_phone_number,
        )
        with self.factory() as session:
            run = session.get(SourcingRun, self.run_id)
            assert run is not None
            run.state = RunState.CANCELLED
            run.cancellation_requested = True
            session.commit()
        return receipt


class PollingGateway(RecordingGateway):
    def __init__(self, factory: sessionmaker[Session], run_id: UUID) -> None:
        super().__init__(factory, run_id)
        self.poll_calls = 0

    def poll_enrichment(self, request_id: str) -> EnrichmentResult:
        self.poll_calls += 1
        return EnrichmentResult(
            provider="apollo",
            request_id=request_id,
            people=(),
            snapshot_payload={"credits_consumed": 8, "people": []},
            charged_credits=8,
        )


@pytest.fixture
def enrichment_scenario() -> Generator[dict[str, Any], None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with factory() as session:
        tenant = Tenant(slug=f"enrich-{uuid4()}")
        user = User(
            oidc_subject=f"oidc|{uuid4()}",
            email="owner@example.test",
            display_name="Owner",
        )
        session.add_all((tenant, user))
        session.flush()
        client = ClientCompany(
            tenant_id=tenant.id, name="Client", normalized_name="client"
        )
        session.add(client)
        session.flush()
        job = Job(
            tenant_id=tenant.id,
            client_id=client.id,
            owner_user_id=user.id,
            title="Product Manager",
            job_description="Job",
        )
        session.add(job)
        session.flush()
        scorecard = ScorecardVersion(
            tenant_id=tenant.id,
            job_id=job.id,
            version=1,
            target_titles=[],
            seniority=[],
            locations=[],
            industry_code="technology",
            suggested_adjacent_industries=[],
            uncertainties=[],
            extraction_status="ready",
            confirmed_by_user_id=user.id,
            confirmed_at=datetime.now(UTC),
        )
        session.add(scorecard)
        session.flush()
        job.current_scorecard_id = scorecard.id
        run = SourcingRun(
            tenant_id=tenant.id,
            job_id=job.id,
            scorecard_version_id=scorecard.id,
            started_by_user_id=user.id,
            state=RunState.ENRICHING,
            current_stage=RunState.ENRICHING.value,
        )
        session.add(run)
        session.flush()
        for index in range(55):
            candidate = Candidate(
                id=UUID(int=index + 1),
                tenant_id=tenant.id,
                full_name=f"Person {index}",
                normalized_name=f"person {index}",
                location="New York, United States",
            )
            session.add(candidate)
            session.flush()
            session.add(
                SourceIdentity(
                    tenant_id=tenant.id,
                    candidate_id=candidate.id,
                    provider="apollo",
                    provider_person_id=f"person-{index}",
                    source_timestamp=datetime.now(UTC),
                    confidence=1,
                )
            )
            session.add(
                RunCandidate(
                    tenant_id=tenant.id,
                    run_id=run.id,
                    candidate_id=candidate.id,
                    scorecard_version_id=scorecard.id,
                    match_score=90,
                    classification="main",
                )
            )
        session.add(
            UsageBudget(
                tenant_id=tenant.id,
                max_enrichments=100,
                max_estimated_credits=1000,
            )
        )
        session.commit()
        values = {
            "factory": factory,
            "tenant_id": tenant.id,
            "user_id": user.id,
            "run_id": run.id,
        }
    yield values
    engine.dispose()


def test_top_enrichment_is_stable_capped_batched_and_budgeted(
    enrichment_scenario: dict[str, Any],
) -> None:
    scenario = enrichment_scenario
    gateway = RecordingGateway(scenario["factory"], scenario["run_id"])
    context = RequestContext(
        tenant_id=scenario["tenant_id"],
        user_id=scenario["user_id"],
        role=Role.OWNER,
    )
    objects = MemoryObjectStore()

    enqueue_top_enrichment(
        scenario["run_id"],
        session_factory=scenario["factory"],
        context=context,
        gateway=gateway,
        callback_base_url="https://api.example.test",
        contact_cipher=ContactCipher(
            base64.b64encode(b"c" * 32).decode(), b"contact-lookup"
        ),
        snapshot_store=SnapshotStore(
            objects, "snapshots", base64.b64encode(b"s" * 32).decode()
        ),
        policy=RegionalContactPolicy(True, False),
        token_codec=CapabilityTokenCodec(b"webhook-key"),
    )

    assert [len(call[0]) for call in gateway.calls] == [10, 10, 10, 10, 10]
    assert gateway.request_counts_at_call == [5, 5, 5, 5, 5]
    assert gateway.run_states_at_call[:4] == [RunState.ENRICHING] * 4
    assert [
        person.provider_person_id for call in gateway.calls for person in call[0]
    ] == [f"person-{index}" for index in range(50)]
    assert all(personal and not phone for _, personal, phone in gateway.calls)
    with scenario["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(EnrichmentRequest)) == 5
        assert (
            session.scalar(
                select(func.sum(UsageLedger.charged_units)).where(
                    UsageLedger.unit_type == "enrichments"
                )
            )
            == 50
        )
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None and run.state is RunState.READY


def test_cancelled_run_never_starts_new_provider_work(
    enrichment_scenario: dict[str, Any],
) -> None:
    scenario = enrichment_scenario
    with scenario["factory"]() as session:
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None
        run.state = RunState.CANCELLED
        run.cancellation_requested = True
        session.commit()
    gateway = RecordingGateway(scenario["factory"], scenario["run_id"])

    enqueue_top_enrichment(
        scenario["run_id"],
        session_factory=scenario["factory"],
        context=RequestContext(
            tenant_id=scenario["tenant_id"],
            user_id=scenario["user_id"],
            role=Role.OWNER,
        ),
        gateway=gateway,
        callback_base_url="https://api.example.test",
        contact_cipher=ContactCipher(
            base64.b64encode(b"c" * 32).decode(), b"contact-lookup"
        ),
        snapshot_store=SnapshotStore(
            MemoryObjectStore(),
            "snapshots",
            base64.b64encode(b"s" * 32).decode(),
        ),
        policy=RegionalContactPolicy(True, True),
        token_codec=CapabilityTokenCodec(b"webhook-key"),
    )

    assert gateway.calls == []


def test_duplicate_delivery_waits_for_in_flight_provider_submission(
    enrichment_scenario: dict[str, Any],
) -> None:
    scenario = enrichment_scenario
    gateway = RecordingGateway(scenario["factory"], scenario["run_id"])
    with scenario["factory"]() as session:
        request = EnrichmentRequest(
            tenant_id=scenario["tenant_id"],
            run_id=scenario["run_id"],
            provider="apollo",
            candidate_ids=[str(UUID(int=1))],
            reservation_key="crash-after-publish",
            status="submitting",
            stage_deadline=datetime.now(UTC) + timedelta(minutes=4),
        )
        session.add(request)
        session.commit()
        request_id = request.id

    result = execute_queued_enrichment_request(
        scenario["factory"],
        request_id,
        RequestContext(
            tenant_id=scenario["tenant_id"],
            user_id=scenario["user_id"],
            role=Role.OWNER,
        ),
        gateway=gateway,
        callback_base_url="https://api.example.test",
        contact_cipher=ContactCipher(
            base64.b64encode(b"c" * 32).decode(), b"contact-lookup"
        ),
        snapshot_store=SnapshotStore(
            MemoryObjectStore(),
            "snapshots",
            base64.b64encode(b"s" * 32).decode(),
        ),
        policy=RegionalContactPolicy(False, False),
        token_codec=CapabilityTokenCodec(b"webhook-key"),
    )

    assert result is not None
    assert 1 <= result.retry_after_seconds <= 240
    assert gateway.calls == []
    with scenario["factory"]() as session:
        persisted = session.get(EnrichmentRequest, request_id)
        assert persisted is not None and persisted.status == "submitting"
        assert persisted.error_code is None


def test_cancellation_race_after_provider_call_is_fenced_before_payload_write(
    enrichment_scenario: dict[str, Any],
) -> None:
    scenario = enrichment_scenario
    gateway = CancellingGateway(scenario["factory"], scenario["run_id"])
    objects = MemoryObjectStore()

    enqueue_top_enrichment(
        scenario["run_id"],
        1,
        session_factory=scenario["factory"],
        context=RequestContext(
            tenant_id=scenario["tenant_id"],
            user_id=scenario["user_id"],
            role=Role.OWNER,
        ),
        gateway=gateway,
        callback_base_url="https://api.example.test",
        contact_cipher=ContactCipher(
            base64.b64encode(b"c" * 32).decode(), b"contact-lookup"
        ),
        snapshot_store=SnapshotStore(
            objects, "snapshots", base64.b64encode(b"s" * 32).decode()
        ),
        policy=RegionalContactPolicy(False, False),
        token_codec=CapabilityTokenCodec(b"webhook-key"),
    )

    with scenario["factory"]() as session:
        request = session.scalar(select(EnrichmentRequest))
        assert request is not None and request.status == "cancelled"
        assert session.scalar(select(func.count()).select_from(ProviderSnapshot)) == 0
        assert all(
            value is not None
            for value in session.scalars(select(UsageLedger.charged_units))
        )
    assert objects.objects == {}


def test_provider_enrichment_failure_preserves_candidates_and_marks_partial(
    enrichment_scenario: dict[str, Any],
) -> None:
    scenario = enrichment_scenario
    gateway = FailingGateway(scenario["factory"], scenario["run_id"])

    enqueue_top_enrichment(
        scenario["run_id"],
        session_factory=scenario["factory"],
        context=RequestContext(
            tenant_id=scenario["tenant_id"],
            user_id=scenario["user_id"],
            role=Role.OWNER,
        ),
        gateway=gateway,
        callback_base_url="https://api.example.test",
        contact_cipher=ContactCipher(
            base64.b64encode(b"c" * 32).decode(), b"contact-lookup"
        ),
        snapshot_store=SnapshotStore(
            MemoryObjectStore(),
            "snapshots",
            base64.b64encode(b"s" * 32).decode(),
        ),
        policy=RegionalContactPolicy(False, False),
        token_codec=CapabilityTokenCodec(b"webhook-key"),
    )

    with scenario["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(Candidate)) == 55
        assert (
            session.scalar(
                select(func.count())
                .select_from(RunCandidate)
                .where(RunCandidate.enrichment_status == "failed")
            )
            == 50
        )
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None and run.state is RunState.PARTIALLY_READY


def test_budget_exhaustion_happens_before_provider_call(
    enrichment_scenario: dict[str, Any],
) -> None:
    scenario = enrichment_scenario
    with scenario["factory"]() as session:
        budget = session.scalar(select(UsageBudget))
        assert budget is not None
        budget.max_enrichments = 0
        session.commit()
    gateway = RecordingGateway(scenario["factory"], scenario["run_id"])

    enqueue_top_enrichment(
        scenario["run_id"],
        session_factory=scenario["factory"],
        context=RequestContext(
            tenant_id=scenario["tenant_id"],
            user_id=scenario["user_id"],
            role=Role.OWNER,
        ),
        gateway=gateway,
        callback_base_url="https://api.example.test",
        contact_cipher=ContactCipher(
            base64.b64encode(b"c" * 32).decode(), b"contact-lookup"
        ),
        snapshot_store=SnapshotStore(
            MemoryObjectStore(),
            "snapshots",
            base64.b64encode(b"s" * 32).decode(),
        ),
        policy=RegionalContactPolicy(False, False),
        token_codec=CapabilityTokenCodec(b"webhook-key"),
    )

    assert gateway.calls == []
    with scenario["factory"]() as session:
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None and run.state is RunState.PARTIALLY_READY
        assert set(session.scalars(select(EnrichmentRequest.status))) == {"failed"}


def test_regional_policy_is_conservative_for_unknown_and_gdpr_locations() -> None:
    policy = RegionalContactPolicy(True, True)

    assert policy.reveal_flags("Bengaluru, India") == (True, True)
    assert policy.reveal_flags("New York, United States") == (True, True)
    assert policy.reveal_flags("Berlin, Germany") == (False, False)
    assert policy.reveal_flags(None) == (False, False)


def test_expired_snapshot_reconciliation_deletes_object_and_database_reference(
    enrichment_scenario: dict[str, Any],
) -> None:
    scenario = enrichment_scenario
    gateway = RecordingGateway(scenario["factory"], scenario["run_id"])
    objects = MemoryObjectStore()
    snapshots = SnapshotStore(
        objects, "snapshots", base64.b64encode(b"s" * 32).decode()
    )
    context = RequestContext(
        tenant_id=scenario["tenant_id"],
        user_id=scenario["user_id"],
        role=Role.OWNER,
    )
    enqueue_top_enrichment(
        scenario["run_id"],
        1,
        session_factory=scenario["factory"],
        context=context,
        gateway=gateway,
        callback_base_url="https://api.example.test",
        contact_cipher=ContactCipher(
            base64.b64encode(b"c" * 32).decode(), b"contact-lookup"
        ),
        snapshot_store=snapshots,
        policy=RegionalContactPolicy(False, False),
        token_codec=CapabilityTokenCodec(b"webhook-key"),
    )
    with scenario["factory"]() as session:
        reference = session.scalar(select(ProviderSnapshot))
        assert reference is not None
        reference.expires_at = datetime(2026, 8, 15, tzinfo=UTC)
        session.commit()
        removed = reconcile_snapshot_references(
            session,
            snapshots,
            now=datetime(2026, 8, 16, tzinfo=UTC),
        )
        session.commit()

    assert removed == 1
    assert not objects.objects
    with scenario["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(ProviderSnapshot)) == 0


def test_async_phone_budget_stays_reserved_until_terminal_payload_and_replays_once(
    enrichment_scenario: dict[str, Any],
) -> None:
    scenario = enrichment_scenario
    gateway = RecordingGateway(scenario["factory"], scenario["run_id"])
    objects = MemoryObjectStore()
    snapshots = SnapshotStore(
        objects, "snapshots", base64.b64encode(b"s" * 32).decode()
    )
    cipher = ContactCipher(base64.b64encode(b"c" * 32).decode(), b"lookup")
    codec = CapabilityTokenCodec(b"webhook-key")
    context = RequestContext(
        tenant_id=scenario["tenant_id"],
        user_id=scenario["user_id"],
        role=Role.OWNER,
    )

    enqueue_top_enrichment(
        scenario["run_id"],
        1,
        session_factory=scenario["factory"],
        context=context,
        gateway=gateway,
        callback_base_url="https://api.example.test",
        contact_cipher=cipher,
        snapshot_store=snapshots,
        policy=RegionalContactPolicy(False, True),
        token_codec=codec,
    )
    with scenario["factory"]() as session:
        request = session.scalar(select(EnrichmentRequest))
        assert request is not None
        assert request.synchronous_credits == 1
        assert all(
            value is None
            for value in session.scalars(select(UsageLedger.charged_units))
        )
        payload: dict[str, object] = {
            "credits_consumed": 8,
            "people": [
                {
                    "id": "person-0",
                    "status": "success",
                    "phone_numbers": [],
                }
            ],
        }
        apply_enrichment_payload(
            session,
            request,
            payload,
            codec=codec,
            snapshot_store=snapshots,
            contact_cipher=cipher,
            source="webhook",
        )
        session.commit()
        apply_enrichment_payload(
            session,
            request,
            payload,
            codec=codec,
            snapshot_store=snapshots,
            contact_cipher=cipher,
            source="webhook",
        )
        session.commit()

    with scenario["factory"]() as session:
        charged = dict(
            session.execute(
                select(UsageLedger.unit_type, UsageLedger.charged_units)
            ).all()
        )
        assert charged == {"enrichments": 1, "estimated_credits": 9}
        assert session.scalar(select(func.count()).select_from(UsageLedger)) == 2


def test_poll_terminal_reconciles_async_credits_once_and_replay_is_noop(
    enrichment_scenario: dict[str, Any],
) -> None:
    scenario = enrichment_scenario
    gateway = PollingGateway(scenario["factory"], scenario["run_id"])
    context = RequestContext(
        tenant_id=scenario["tenant_id"],
        user_id=scenario["user_id"],
        role=Role.OWNER,
    )
    snapshots = SnapshotStore(
        MemoryObjectStore(), "snapshots", base64.b64encode(b"s" * 32).decode()
    )
    cipher = ContactCipher(base64.b64encode(b"c" * 32).decode(), b"lookup")
    codec = CapabilityTokenCodec(b"webhook-key")
    submission = enqueue_top_enrichment(
        scenario["run_id"],
        1,
        session_factory=scenario["factory"],
        context=context,
        gateway=gateway,
        callback_base_url="https://api.example.test",
        contact_cipher=cipher,
        snapshot_store=snapshots,
        policy=RegionalContactPolicy(False, True),
        token_codec=codec,
    )[0]

    for _ in range(2):
        poll_enrichment_request(
            scenario["factory"],
            submission.request_id,
            context,
            gateway=gateway,
            token_codec=codec,
            snapshot_store=snapshots,
            contact_cipher=cipher,
        )

    assert gateway.poll_calls == 1
    with scenario["factory"]() as session:
        charged = dict(
            session.execute(
                select(UsageLedger.unit_type, UsageLedger.charged_units)
            ).all()
        )
        assert charged == {"enrichments": 1, "estimated_credits": 9}


def test_terminal_poll_failure_finalizes_run_partial(
    enrichment_scenario: dict[str, Any],
) -> None:
    scenario = enrichment_scenario
    gateway = RecordingGateway(scenario["factory"], scenario["run_id"])
    context = RequestContext(
        tenant_id=scenario["tenant_id"],
        user_id=scenario["user_id"],
        role=Role.OWNER,
    )
    snapshots = SnapshotStore(
        MemoryObjectStore(), "snapshots", base64.b64encode(b"s" * 32).decode()
    )
    cipher = ContactCipher(base64.b64encode(b"c" * 32).decode(), b"lookup")
    codec = CapabilityTokenCodec(b"webhook-key")
    submissions = enqueue_top_enrichment(
        scenario["run_id"],
        1,
        session_factory=scenario["factory"],
        context=context,
        gateway=gateway,
        callback_base_url="https://api.example.test",
        contact_cipher=cipher,
        snapshot_store=snapshots,
        policy=RegionalContactPolicy(False, True),
        token_codec=codec,
    )

    poll_enrichment_request(
        scenario["factory"],
        submissions[0].request_id,
        context,
        gateway=FailingGateway(scenario["factory"], scenario["run_id"]),
        token_codec=codec,
        snapshot_store=snapshots,
        contact_cipher=cipher,
    )

    with scenario["factory"]() as session:
        run = session.get(SourcingRun, scenario["run_id"])
        assert run is not None and run.state is RunState.PARTIALLY_READY
