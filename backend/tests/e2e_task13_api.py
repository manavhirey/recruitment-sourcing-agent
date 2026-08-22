"""Production FastAPI app with deterministic provider/LLM fakes for browser E2E.

This module is test support only; production startup never imports it.
"""

import os
import tempfile
from collections.abc import Generator
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

os.environ.update(
    {
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "MIGRATION_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "MAINTENANCE_DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "REDIS_URL": "redis://localhost:6379/15",
        "OBJECT_STORE_ENDPOINT": "http://localhost:9000",
        "OBJECT_STORE_WRITER_ACCESS_KEY_ID": "test-writer-key",
        "OBJECT_STORE_WRITER_SECRET_ACCESS_KEY": "test-writer-value",
        "OBJECT_STORE_DELETE_ACCESS_KEY_ID": "test-delete-key",
        "OBJECT_STORE_DELETE_SECRET_ACCESS_KEY": "test-delete-value",
        "OBJECT_STORE_LIFECYCLE_ADMIN_ACCESS_KEY_ID": "test-lifecycle-key",
        "OBJECT_STORE_LIFECYCLE_ADMIN_SECRET_ACCESS_KEY": "test-lifecycle-value",
        "OIDC_ISSUER": "https://issuer.test/",
        "OIDC_AUDIENCE": "sourcing-api",
        "OPENAI_API_KEY": "test-openai-key",
        "APOLLO_API_KEY": "test-apollo-key",
        "CONTACT_ENCRYPTION_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "SUPPRESSION_HMAC_KEY": "test-suppression-key",
        "TELEMETRY_HMAC_KEY": "test-telemetry-key",
        "WEBHOOK_HMAC_KEY": "test-webhook-key",
    }
)

import app.identity.dependencies as identity_dependencies
from app.candidates.models import Candidate
from app.clients.models import ClientCompany, ClientIndustry
from app.core.config import Settings
from app.core.database import Base, get_db
from app.crm.service import materialize_run_matches
from app.identity.models import Membership, Tenant, User
from app.identity.schemas import IdentityClaims, RequestContext, Role
from app.jobs.schemas import (
    ClientContext,
    CriterionKind,
    ScorecardCriterion,
    ScorecardDraft,
)
from app.main import create_app
from app.sourcing.models import RunCandidate, SourcingRun
from app.sourcing.state_machine import RunState

TENANT_ID = UUID("00000000-0000-4000-8000-000000000001")
CLIENT_ID = UUID("00000000-0000-4000-8000-000000000201")
CANDIDATE_ID = UUID("00000000-0000-4000-8000-000000000601")
USER_ID = UUID("00000000-0000-4000-8000-000000000801")

database_path = Path(tempfile.gettempdir()) / "sourcing-task13-real-e2e.sqlite3"
database_path.unlink(missing_ok=True)
engine = create_engine(
    f"sqlite+pysqlite:///{database_path}",
    connect_args={"check_same_thread": False},
)
Base.metadata.create_all(engine)


class StaticVerifier:
    def verify(self, token: str) -> IdentityClaims:
        if token != "e2e-access-token":
            raise HTTPException(status_code=401, detail={"code": "invalid_token"})
        return IdentityClaims(
            subject="oidc|e2e-owner",
            email="owner@example.test",
            name="E2E Owner",
            email_verified=True,
        )


class DeterministicScorecardGateway:
    def extract(
        self,
        job_description: str,
        client_context: ClientContext,
    ) -> ScorecardDraft:
        del job_description, client_context
        return ScorecardDraft(
            target_titles=["Senior Product Manager"],
            criteria=[
                ScorecardCriterion(
                    key="payments",
                    label="Payments platform experience",
                    kind=CriterionKind.MUST_HAVE,
                    evidence_required=True,
                    source_text="payments platform",
                )
            ],
            seniority=["senior"],
            minimum_years=5,
            locations=["New York, NY"],
            industry_code="technology.fintech",
            suggested_adjacent_industries=[],
            uncertainties=[],
        )


def database_session() -> Generator[Session, None, None]:
    with Session(engine, expire_on_commit=False) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def dispatch_sourcing_run(
    run_id: UUID,
    tenant_id: UUID,
    user_id: UUID,
    dispatch_key: str,
) -> None:
    del dispatch_key
    context = RequestContext(
        tenant_id=tenant_id,
        user_id=user_id,
        role=Role.OWNER,
    )
    with Session(engine, expire_on_commit=False) as session:
        run = session.get(SourcingRun, run_id)
        candidate = session.get(Candidate, CANDIDATE_ID)
        if run is None or candidate is None:
            raise RuntimeError("deterministic sourcing seed is missing")
        existing = session.scalar(
            select(RunCandidate).where(
                RunCandidate.tenant_id == tenant_id,
                RunCandidate.run_id == run.id,
                RunCandidate.candidate_id == candidate.id,
            )
        )
        if existing is None:
            session.add(
                RunCandidate(
                    tenant_id=tenant_id,
                    run_id=run.id,
                    candidate_id=candidate.id,
                    scorecard_version_id=run.scorecard_version_id,
                    match_score=92,
                    classification="main",
                    evidence={
                        "total": 92,
                        "breakdown": {
                            "role_and_skills": 35,
                            "scope_seniority_years": 22,
                            "industry": 20,
                            "location_and_eligibility": 8,
                            "recency_and_trajectory": 7,
                        },
                        "criteria": [
                            {
                                "key": "payments",
                                "label": "Payments platform experience",
                                "state": "supported",
                                "summary": "Stored experience supports this requirement.",
                                "points": 35,
                                "max_points": 35,
                                "evidence": ["Led a payments platform program."],
                                "source_refs": [],
                            }
                        ],
                        "failed_must_haves": [],
                        "unknown_keys": [],
                    },
                    scoring_version="matching-v1",
                    enrichment_status="available",
                )
            )
            session.flush()
        run.state = RunState.PARTIALLY_READY
        run.current_stage = "enrichment"
        run.candidate_count = 1
        run.matched_count = 1
        materialize_run_matches(session, run, context)
        session.commit()


settings = Settings.for_test()
app = create_app(
    settings,
    scorecard_gateway=DeterministicScorecardGateway(),
    sourcing_dispatcher=dispatch_sourcing_run,
)
app.state.token_verifier = StaticVerifier()
app.dependency_overrides[get_db] = database_session
identity_dependencies.apply_tenant_context = lambda session, tenant_id: None

_observed: list[dict[str, str]] = []


@app.middleware("http")
async def observe_authorized_api_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
    if request.url.path.startswith("/api/v1/"):
        _observed.append(
            {
                "method": request.method,
                "path": request.url.path,
                "tenant": request.headers.get("X-Tenant-ID", ""),
                "authorization": (
                    "present" if request.headers.get("Authorization") else "missing"
                ),
            }
        )
    return await call_next(request)


@app.get("/__e2e__/observed")
def observed() -> list[dict[str, str]]:
    return list(_observed)


with Session(engine) as session:
    session.add_all(
        (
            Tenant(id=TENANT_ID, slug="task13-real-e2e"),
            User(
                id=USER_ID,
                oidc_subject="oidc|e2e-owner",
                email="owner@example.test",
                display_name="E2E Owner",
            ),
            Candidate(
                id=CANDIDATE_ID,
                tenant_id=TENANT_ID,
                full_name="Priya Sharma",
                normalized_name="priya sharma",
                current_title="Senior Product Manager",
                normalized_title="senior product manager",
                current_company="Northstar Pay",
                normalized_company="northstar pay",
                location="New York, United States",
                normalized_location="new york united states",
                normalized_skills=["payments", "product management"],
                industry_codes=["technology.fintech"],
            ),
        )
    )
    session.flush()
    membership = Membership(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        role=Role.OWNER,
        active=True,
    )
    client = ClientCompany(
        id=CLIENT_ID,
        tenant_id=TENANT_ID,
        name="Northstar Payments",
        normalized_name="northstar payments",
    )
    session.add_all((membership, client))
    session.flush()
    session.add(
        ClientIndustry(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            industry_code="technology.fintech",
            taxonomy_version="v1",
        )
    )
    session.commit()
