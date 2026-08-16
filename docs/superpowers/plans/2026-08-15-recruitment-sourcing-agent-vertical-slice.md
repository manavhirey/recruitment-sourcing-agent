# Recruitment Sourcing Agent Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a production-ready, multi-tenant sourcing workflow that turns a confirmed job scorecard into 100–300 Apollo-sourced candidates, explains and ranks the matches, enriches the top 50, and supports agency recruiter review.

**Architecture:** Build a Next.js web application and a FastAPI modular monolith backed by PostgreSQL. Celery workers use Redis for transport while PostgreSQL owns durable run state and idempotency; Apollo and the language model remain behind typed gateways.

**Tech Stack:** Python 3.12+, uv, FastAPI, Pydantic 2, SQLAlchemy 2, Alembic, PostgreSQL 16, Celery 5, Redis 7, HTTPX, OpenAI Python SDK, PyJWT, cryptography, boto3, OpenTelemetry, Prometheus, pytest, Hypothesis, Next.js, TypeScript, Auth.js, TanStack Query, React Hook Form, Zod, Mock Service Worker, Vitest, Testing Library, Playwright, Docker Compose.

## Global Constraints

- Support recruitment agencies operating in India and the United States.
- Capacity target: 25 agencies, 250 active recruiter users, and 25 concurrent sourcing runs.
- Use only licensed provider APIs; do not implement scraping, browser automation, CAPTCHA handling, or access-control bypass.
- Apollo is the only live sourcing and enrichment connector; the SaaS operator owns its API credentials.
- One job sources 100–300 candidates and automatically enriches only the top 50.
- A recruiter must confirm an immutable scorecard version before sourcing starts.
- Missing a confirmed must-have puts a candidate in Near Matches; unknown evidence remains eligible unless the scorecard marks evidence as mandatory.
- Use the approved 100-point weights: role/skills 35, scope/seniority/years 25, industry 20, location/work eligibility 10, recency/trajectory 10.
- Award points only for supported evidence; do not renormalize scores around unknown data.
- Candidate identity is canonical only within an agency; never link or expose candidates across tenants.
- CRM stages are New, Reviewed, Shortlisted, and Rejected. Near Match is a classification, not a stage.
- Store all provider-returned contact fields only when permitted; encrypt them and retain them for 180 days after verification or recorded legitimate use.
- Expire provider snapshots after 30 days.
- Do not send outreach or implement interviews, offers, placements, billing, custom stages, or automated model retraining.
- Common API routes use `/api/v1`; provider webhooks use `/webhooks`.
- Persist UTC timestamps and UUID primary keys. All mutating endpoints accept or derive an idempotency key.
- Before Task 14 can pass its matching-quality gate, a recruiter panel must supply at least 30 de-identified India/US jobs with at least 20 relevance judgments per job; synthetic fixtures may test mechanics but cannot satisfy the launch gate.
- The approved design is `docs/superpowers/specs/2026-08-15-recruitment-sourcing-agent-vertical-slice-design.md`.

---

## Planned File Structure

```text
.
├── .github/workflows/ci.yml
├── .gitignore
├── compose.yaml
├── docs/runbooks/
│   ├── backup-restore.md
│   ├── provider-outage.md
│   └── rollback.md
├── backend/
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/versions/
│   ├── app/
│   │   ├── main.py
│   │   ├── worker.py
│   │   ├── core/{config,database,errors,security,telemetry}.py
│   │   ├── identity/{models,schemas,service,router,dependencies,cli}.py
│   │   ├── clients/{models,schemas,service,router,taxonomy}.py
│   │   ├── jobs/{models,schemas,service,router,llm}.py
│   │   ├── providers/{base,apollo,query_planner}.py
│   │   ├── candidates/{models,schemas,normalization,resolver,service,router}.py
│   │   ├── matching/{schemas,engine,explanations}.py
│   │   ├── sourcing/{models,schemas,state_machine,tasks,router}.py
│   │   ├── crm/{models,schemas,service,router,exports}.py
│   │   ├── privacy/{models,schemas,service,tasks,router}.py
│   │   └── audit/{models,service}.py
│   └── tests/
│       ├── conftest.py
│       ├── contract/
│       ├── integration/
│       └── unit/
├── web/
│   ├── Dockerfile
│   ├── package.json
│   ├── app/
│   │   ├── api/auth/[...nextauth]/route.ts
│   │   └── (app)/{layout,jobs,clients,candidates,settings}/
│   ├── components/{layout,jobs,candidates,scorecards}/
│   ├── lib/{api,auth,schemas}.ts
│   └── tests/{fixtures.ts,jobs,clients,scorecards,candidates,settings}/
├── evaluation/
│   ├── fixtures/jobs.jsonl
│   ├── fixtures/judgments.jsonl
│   └── evaluate_matching.py
└── loadtests/k6-sourcing.js
```

Each backend feature package owns its models, schemas, service, and routes. A feature must call another feature through its service interface rather than importing and mutating its tables. Provider payload types stay inside `providers`; the rest of the application consumes normalized DTOs.

## Shared Test Fixture Contracts

`backend/tests/conftest.py` is created in Task 1 and extended by each backend task. Keep these fixture names and meanings stable:

- `session: Session`: API-role PostgreSQL transaction rolled back after the test.
- `api: TestClient`: FastAPI client with dependency-overridden database session and provider/LLM fakes.
- `tenant_factory(slug) -> Tenant`, `client_factory(name) -> ClientCompany`, `job_factory() -> Job`, and `candidate_factory(**facts) -> CandidateProfile`: persist tenant-scoped records with safe defaults.
- `tenant_token(role, tenant_id, allowed_client_ids) -> dict[str, str]`: returns both `Authorization` and `X-Tenant-ID` headers; aliases are `owner_token`, `recruiter_token`, `owner_context`, and `recruiter_context`.
- `scorecard_factory(**overrides) -> ConfirmedScorecard` and `provider_person_factory(**overrides) -> ProviderPerson`: create immutable DTOs without persistence.
- `scorecard`, `provider_query`, `draft_job`, `ready_job_with_decisions`, `shortlist`, and `directory_fixture`: named scenario fixtures built from the factories above with IDs exposed as typed attributes.
- `job_service`, `candidate_service`, `crm_service`, `privacy_service`, and `engine`: real services using the test transaction and fake gateways only where an external network would otherwise be called.
- `run_factory(state) -> SourcingRun`, `execute_source_task(run_id, idempotency_key)`, `run_candidate_count(run_id)`, and `completed_checkpoint_count(run_id, key)`: execute Celery tasks synchronously through the same checkpoint code used by workers.
- `apollo_gateway`: real HTTPX Apollo adapter pointed at `respx_mock`; it never receives a live key.
- `cipher`: `ContactCipher` using a fixed test key; `pending_enrichment` and `apollo_phone_payload` use matching request and candidate IDs.
- `captured_logs`: in-memory structured-log sink after the production redaction processor.
- `reveal_contact(value)`, `candidate_personal_fields(candidate_id)`, and `ingest_provider_person(tenant_id, person)`: test helpers that call production services and return typed results rather than querying encrypted columns directly.

`web/tests/fixtures.ts` exports `scorecardDraftFixture`, `priyaFixture`, `marcusFixture`, `nearMatchFixture`, and `productManagerJobDescription`. Use Mock Service Worker handlers that implement the committed generated API types; do not hand-write a second response schema.

### Task 1: Bootable Backend and Local Infrastructure

**Files:**
- Create: `.gitignore`
- Create: `.env.example`
- Create: `compose.yaml`
- Create: `backend/docker/init-test-db.sql`
- Create: `backend/pyproject.toml`
- Create: `backend/alembic.ini`
- Create: `backend/alembic/env.py`
- Create: `backend/app/__init__.py`
- Create: `backend/app/main.py`
- Create: `backend/app/core/config.py`
- Create: `backend/app/core/database.py`
- Create: `backend/app/core/errors.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/unit/test_health.py`

**Interfaces:**
- Consumes: no application interfaces.
- Produces: `Settings`, `get_settings()`, SQLAlchemy `Base`, `session_factory`, `get_db()`, and `create_app() -> FastAPI`.

- [ ] **Step 1: Write the failing health and configuration tests**

```python
# backend/tests/unit/test_health.py
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_test_settings_supply_all_required_secrets() -> None:
    settings = Settings.for_test()
    assert settings.environment == "test"
    assert settings.database_url.startswith("postgresql+psycopg://")


def test_health_reports_ready() -> None:
    client = TestClient(create_app(Settings.for_test()))
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run: `cd backend && uv run pytest tests/unit/test_health.py -v`

Expected: FAIL during collection because `app.core.config` and `app.main` do not exist.

- [ ] **Step 3: Add the backend package and testable settings**

Create `backend/pyproject.toml` with the following direct dependency groups, then run `uv lock` and commit `backend/uv.lock`:

```toml
[project]
name = "recruitment-sourcing-api"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "alembic>=1.13",
  "boto3>=1.34",
  "celery[redis]>=5.4",
  "cryptography>=42",
  "email-validator>=2.2",
  "fastapi>=0.115",
  "httpx>=0.27",
  "openai>=1.50",
  "opentelemetry-api>=1.27",
  "prometheus-client>=0.21",
  "psycopg[binary,pool]>=3.2",
  "pydantic-settings>=2.5",
  "pyjwt[crypto]>=2.9",
  "sqlalchemy>=2.0",
  "structlog>=24.4",
  "uvicorn[standard]>=0.30",
]

[dependency-groups]
dev = [
  "freezegun>=1.5",
  "hypothesis>=6.112",
  "mypy>=1.11",
  "pytest>=8.3",
  "pytest-cov>=5.0",
  "respx>=0.21",
  "ruff>=0.6",
]
```

`.gitignore` must include `.env`, `.DS_Store`, `.superpowers/`, Python caches, coverage output, `node_modules/`, `.next/`, Playwright output, and local object-store volumes. `.env.example` contains variable names and non-secret development values only.

```python
# backend/app/core/config.py
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    database_url: str
    redis_url: str
    object_store_endpoint: str
    object_store_bucket: str = "provider-snapshots"
    oidc_issuer: str
    oidc_audience: str
    apollo_api_key: SecretStr
    contact_encryption_key: SecretStr
    suppression_hmac_key: SecretStr
    webhook_hmac_key: SecretStr

    @classmethod
    def for_test(cls) -> "Settings":
        return cls(
            environment="test",
            database_url="postgresql+psycopg://postgres:postgres@localhost:5432/sourcing_test",
            redis_url="redis://localhost:6379/15",
            object_store_endpoint="http://localhost:9000",
            oidc_issuer="https://issuer.test/",
            oidc_audience="sourcing-api",
            apollo_api_key="test-apollo-key",
            contact_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            suppression_hmac_key="test-suppression-key",
            webhook_hmac_key="test-webhook-key",
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

```python
# backend/app/main.py
from fastapi import FastAPI

from app.core.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Recruitment Sourcing API", version="1.0.0")
    app.state.settings = settings or get_settings()

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        return {"status": "ready"}

    return app


app = create_app()
```

- [ ] **Step 4: Add SQLAlchemy, Alembic, and local services**

Create `backend/app/core/database.py` with a declarative `Base`, psycopg engine, `session_factory`, and a `get_db()` dependency that commits on success and rolls back on error. Configure Alembic to import `Base.metadata`.

Create `compose.yaml` with pinned PostgreSQL 16, Redis 7, and MinIO services, health checks, named volumes, and a separate `sourcing_test` database initialization script. Do not place credentials in the file; read them from `.env` with documented development defaults in `.env.example`.

```python
# backend/app/core/database.py
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_engine(get_settings().database_url, pool_pre_ping=True)
session_factory = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with session_factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
```

- [ ] **Step 5: Run quality checks**

Run: `cd backend && uv run ruff check . && uv run mypy app && uv run pytest tests/unit/test_health.py -v`

Expected: all commands exit 0; both tests pass.

- [ ] **Step 6: Commit the bootable backend**

```bash
git add .gitignore .env.example compose.yaml backend
git commit -m "build: add backend and local service foundation"
```

### Task 2: Tenant Identity, OIDC Verification, and Row Isolation

**Files:**
- Create: `backend/app/core/security.py`
- Create: `backend/app/identity/models.py`
- Create: `backend/app/identity/schemas.py`
- Create: `backend/app/identity/service.py`
- Create: `backend/app/identity/dependencies.py`
- Create: `backend/app/identity/router.py`
- Create: `backend/app/identity/cli.py`
- Create: `backend/alembic/versions/0001_identity_and_rls.py`
- Create: `backend/tests/unit/identity/test_token_verifier.py`
- Create: `backend/tests/integration/identity/test_tenant_isolation.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Consumes: `Settings`, `Base`, `get_db()`.
- Produces: `Role`, `IdentityClaims`, `RequestContext`, `TokenVerifier.verify(token)`, `get_request_context()`, `TenantService.provision(slug, owner_claims)`, `MembershipService.invite()`, `MembershipService.claim_invite()`, and `apply_tenant_context(session, tenant_id)`.

- [ ] **Step 1: Write failing token and tenant-isolation tests**

```python
# backend/tests/unit/identity/test_token_verifier.py
from uuid import UUID

from app.identity.schemas import IdentityClaims


def test_identity_claims_requires_subject_and_email() -> None:
    claims = IdentityClaims.model_validate(
        {"sub": "oidc|owner-1", "email": "owner@agency.test", "name": "Owner"}
    )
    assert claims.subject == "oidc|owner-1"
    assert claims.email == "owner@agency.test"
```

```python
# backend/tests/integration/identity/test_tenant_isolation.py
from sqlalchemy import text


def test_tenant_row_policy_hides_other_tenant(session, tenant_factory) -> None:
    first = tenant_factory(slug="first")
    second = tenant_factory(slug="second")
    session.commit()

    session.execute(text("SELECT set_config('app.tenant_id', :value, true)"), {"value": str(first.id)})
    visible = session.execute(text("SELECT id FROM tenants ORDER BY slug")).scalars().all()
    assert visible == [first.id]
    assert second.id not in visible
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `cd backend && uv run pytest tests/unit/identity tests/integration/identity -v`

Expected: FAIL because identity schemas, tables, and row policies are absent.

- [ ] **Step 3: Define identity contracts and JWT verification**

```python
# backend/app/identity/schemas.py
from enum import StrEnum
from uuid import UUID

from pydantic import AliasChoices, BaseModel, EmailStr, Field


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    RECRUITER = "recruiter"


class IdentityClaims(BaseModel):
    subject: str = Field(validation_alias=AliasChoices("subject", "sub"))
    email: EmailStr
    name: str
    email_verified: bool = False


class RequestContext(BaseModel):
    tenant_id: UUID
    user_id: UUID
    role: Role
    allowed_client_ids: frozenset[UUID] | None = None
```

Implement `TokenVerifier` with `PyJWKClient`, fixed `RS256`, configured issuer and audience, a five-minute JWKS cache, and no logging of the raw token. Map token failures to a stable `401 invalid_token` response.

- [ ] **Step 4: Add identity tables, provisioning, and RLS**

Create SQLAlchemy models for `Tenant`, `User`, `Membership`, and `MembershipInvitation`. Enforce unique tenant slug, unique OIDC subject, and unique `(tenant_id, user_id)` membership. `TenantService.provision()` creates the tenant, user, and Owner membership in one transaction and records the normalized email. Invitation rows store a one-time token HMAC, intended verified email, role, creator, and expiry; never store the bearer token.

In migration `0001_identity_and_rls.py`, enable and force RLS. The `tenants` policy compares `id`; child-table policies compare `tenant_id`; both compare against `current_setting('app.tenant_id', true)::uuid`. The platform provisioning command uses a separate database role that owns migrations but is never used by the web API.

```python
# backend/app/identity/dependencies.py
from sqlalchemy import text
from sqlalchemy.orm import Session


def apply_tenant_context(session: Session, tenant_id: UUID) -> None:
    session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant_id)},
    )
```

- [ ] **Step 5: Add `/api/v1/me` and authorization dependencies**

`get_request_context()` verifies the bearer token, requires an `X-Tenant-ID` header, loads the user's membership for that exact tenant, applies the tenant context before any business query, and returns `RequestContext`. A missing tenant header is `400 tenant_required`; a non-member receives `404 tenant_not_found`. Add `require_role(*roles)` and a `/api/v1/me` response containing tenant ID, user ID, role, and allowed-client scope but no OIDC claims beyond display name and email.

Owners and Admins may list members, create a seven-day one-time invitation link, change Recruiter/Admin roles, and deactivate non-owner memberships. `POST /api/v1/membership-invitations/{token}/claim` requires an OIDC claim with `email_verified=true` and an email matching the invitation. Claiming consumes the token atomically. The last active Owner cannot be demoted or deactivated.

- [ ] **Step 6: Run migration and isolation checks**

Run: `cd backend && uv run alembic upgrade head && uv run pytest tests/unit/identity tests/integration/identity -v`

Expected: token tests pass; the RLS test sees only the active tenant.

- [ ] **Step 7: Commit identity and tenant isolation**

```bash
git add backend/app/core/security.py backend/app/identity backend/alembic/versions/0001_identity_and_rls.py backend/tests
git commit -m "feat: add tenant identity and row isolation"
```

### Task 3: Clients, Recruiter Grants, and Industry Taxonomy

**Files:**
- Create: `backend/app/clients/models.py`
- Create: `backend/app/clients/schemas.py`
- Create: `backend/app/clients/taxonomy.py`
- Create: `backend/app/clients/industry_taxonomy.v1.json`
- Create: `backend/app/clients/service.py`
- Create: `backend/app/clients/router.py`
- Create: `backend/alembic/versions/0002_clients.py`
- Create: `backend/tests/unit/clients/test_taxonomy.py`
- Create: `backend/tests/integration/clients/test_client_access.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Consumes: `RequestContext`, `Role`, `Base`, tenant-scoped session.
- Produces: `IndustryCode`, `IndustryTaxonomy.get(code)`, `IndustryTaxonomy.is_adjacent(source, target)`, `ClientService.create()`, `ClientService.grant_access()`, and REST routes under `/api/v1/clients`.

- [ ] **Step 1: Write failing taxonomy and access tests**

```python
# backend/tests/unit/clients/test_taxonomy.py
from app.clients.taxonomy import IndustryTaxonomy


def test_fintech_can_be_approved_as_adjacent_to_banking() -> None:
    taxonomy = IndustryTaxonomy.load_version("v1")
    assert taxonomy.contains("financial_services.banking")
    assert taxonomy.contains("technology.fintech")
    assert taxonomy.default_adjacency("financial_services.banking") == {
        "technology.fintech"
    }
```

```python
# backend/tests/integration/clients/test_client_access.py
def test_recruiter_cannot_read_ungranted_client(api, recruiter_token, client_factory) -> None:
    hidden = client_factory(name="Hidden Client")
    response = api.get(f"/api/v1/clients/{hidden.id}", headers=recruiter_token)
    assert response.status_code == 404
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd backend && uv run pytest tests/unit/clients tests/integration/clients -v`

Expected: FAIL because the taxonomy and client routes do not exist.

- [ ] **Step 3: Add the versioned taxonomy and client models**

Seed a compact v1 taxonomy covering the launch markets, including technology, fintech, financial services, healthcare, pharmaceuticals, consumer, retail, manufacturing, professional services, logistics, education, telecommunications, energy, and government. Each node contains a stable code, display label, parent code, and default adjacency codes.

Create `ClientCompany`, `ClientIndustry`, `ClientAdjacentIndustry`, and `ClientGrant` models. Use unique constraints on `(tenant_id, normalized_name)` and `(client_id, industry_code)`. RLS applies to every table.

- [ ] **Step 4: Implement explicit access and adjacency approval**

```python
# backend/app/clients/service.py
class ClientService:
    def get_authorized(self, context: RequestContext, client_id: UUID) -> ClientCompany:
        statement = select(ClientCompany).where(ClientCompany.id == client_id)
        if context.role == Role.RECRUITER and context.allowed_client_ids is not None:
            statement = statement.where(ClientCompany.id.in_(context.allowed_client_ids))
        client = self.session.scalar(statement)
        if client is None:
            raise NotFoundError("client_not_found")
        return client
```

Only Owners and Admins may grant client access or approve adjacent industries. Recruiters may create jobs only for clients returned by `get_authorized()`.

- [ ] **Step 5: Add REST routes and verify behavior**

Add list, create, read, update-industry, update-adjacency, and grant routes. Return `404` instead of `403` for resources outside the caller's tenant or client grants.

Run: `cd backend && uv run alembic upgrade head && uv run pytest tests/unit/clients tests/integration/clients -v`

Expected: all client and taxonomy tests pass.

- [ ] **Step 6: Commit clients and taxonomy**

```bash
git add backend/app/clients backend/alembic/versions/0002_clients.py backend/tests backend/app/main.py
git commit -m "feat: add clients and industry taxonomy"
```

### Task 4: Job Intake, Structured Scorecards, and Confirmation

**Files:**
- Create: `backend/app/jobs/models.py`
- Create: `backend/app/jobs/schemas.py`
- Create: `backend/app/jobs/llm.py`
- Create: `backend/app/jobs/service.py`
- Create: `backend/app/jobs/router.py`
- Create: `backend/alembic/versions/0003_jobs_scorecards.py`
- Create: `backend/tests/unit/jobs/test_scorecard_schema.py`
- Create: `backend/tests/integration/jobs/test_scorecard_versioning.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/core/config.py`

**Interfaces:**
- Consumes: `ClientService.get_authorized()`, `IndustryTaxonomy`, `RequestContext`.
- Produces: `CriterionKind`, `ScorecardCriterion`, `ScorecardDraft`, `ConfirmedScorecard`, `ClientContext`, `ScorecardGateway.extract(job_description, client_context)`, `JobService.create()`, `JobService.confirm_scorecard()`, and `/api/v1/jobs` routes.

- [ ] **Step 1: Write failing schema and versioning tests**

```python
# backend/tests/unit/jobs/test_scorecard_schema.py
import pytest
from pydantic import ValidationError

from app.jobs.schemas import CriterionKind, ScorecardCriterion


def test_protected_class_exclusion_is_rejected() -> None:
    with pytest.raises(ValidationError, match="protected characteristic"):
        ScorecardCriterion(
            key="gender",
            label="Male candidates",
            kind=CriterionKind.EXCLUSION,
            evidence_required=True,
        )
```

```python
# backend/tests/integration/jobs/test_scorecard_versioning.py
def test_confirmed_scorecard_is_immutable(job_service, draft_job, owner_context) -> None:
    first = job_service.confirm_scorecard(owner_context, draft_job.id, expected_revision=1)
    second = job_service.revise_scorecard(owner_context, draft_job.id, first.to_draft())
    assert first.version == 1
    assert second.version == 2
    assert first.id != second.id
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd backend && uv run pytest tests/unit/jobs tests/integration/jobs -v`

Expected: FAIL because scorecard schemas and persistence do not exist.

- [ ] **Step 3: Define strict scorecard schemas**

```python
# backend/app/jobs/schemas.py
class CriterionKind(StrEnum):
    MUST_HAVE = "must_have"
    PREFERENCE = "preference"
    EXCLUSION = "exclusion"


class ScorecardCriterion(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    label: str = Field(min_length=3, max_length=160)
    kind: CriterionKind
    evidence_required: bool = False
    source_text: str | None = Field(default=None, max_length=500)
    inferred: bool = False


class ScorecardDraft(BaseModel):
    target_titles: list[str] = Field(min_length=1, max_length=12)
    criteria: list[ScorecardCriterion] = Field(min_length=1, max_length=40)
    seniority: list[str] = Field(max_length=8)
    minimum_years: int | None = Field(default=None, ge=0, le=50)
    maximum_years: int | None = Field(default=None, ge=0, le=50)
    locations: list[str] = Field(max_length=20)
    industry_code: str
    suggested_adjacent_industries: list[str] = Field(max_length=12)
    uncertainties: list[str] = Field(max_length=20)
```

Add a validator that always rejects criteria referring to protected characteristics. Never infer work authorization. An exclusion absent from source text may be added only when a recruiter explicitly enters and confirms a job-related lawful requirement.

- [ ] **Step 4: Implement the model gateway with schema validation and one retry**

Define a `ScorecardGateway` protocol and an `OpenAIResponsesScorecardGateway` adapter. Supply the Pydantic JSON schema, job description, and client industry context; parse directly into `ScorecardDraft`. On schema failure, retry once with the validation errors. On a second failure raise `ScorecardExtractionError`, which the API maps to an editable blank draft containing the original job description and a visible extraction warning.

```python
# backend/app/jobs/llm.py
class ScorecardGateway(Protocol):
    def extract(self, job_description: str, client_context: ClientContext) -> ScorecardDraft:
        raise NotImplementedError


def extraction_instructions() -> str:
    return (
        "Extract only job-relevant criteria. Mark every inference. "
        "Never infer protected characteristics or work authorization. "
        "Return data that validates against the supplied scorecard schema."
    )
```

- [ ] **Step 5: Persist jobs and immutable scorecard versions**

Create `Job`, `ScorecardVersion`, and `ScorecardCriterionRecord`. A job begins in `awaiting_scorecard`. Confirmation checks an optimistic `expected_revision`, validates industry adjacency against the client's approved set, writes a new immutable version, and sets `current_scorecard_id`. Database triggers reject updates to confirmed scorecard rows.

- [ ] **Step 6: Add routes and verify the manual fallback**

Add create job, get job, generate draft, update draft, confirm, list versions, and explicit rescore routes. Test one valid extraction, one invalid-then-valid extraction, and one double failure that returns a manual draft with `extraction_status="manual_required"`.

Run: `cd backend && uv run alembic upgrade head && uv run pytest tests/unit/jobs tests/integration/jobs -v`

Expected: all scorecard tests pass; confirmed rows reject mutation.

- [ ] **Step 7: Commit job intake and scorecards**

```bash
git add backend/app/jobs backend/app/core/config.py backend/alembic/versions/0003_jobs_scorecards.py backend/tests backend/app/main.py
git commit -m "feat: add job intake and confirmed scorecards"
```

### Task 5: Provider Gateway, Query Planning, and Apollo Search

**Files:**
- Create: `backend/app/providers/base.py`
- Create: `backend/app/providers/query_planner.py`
- Create: `backend/app/providers/apollo.py`
- Create: `backend/tests/unit/providers/test_query_planner.py`
- Create: `backend/tests/contract/providers/test_apollo_search.py`
- Modify: `backend/app/core/config.py`

**Interfaces:**
- Consumes: `ConfirmedScorecard`, `IndustryTaxonomy`, `Settings.apollo_api_key`.
- Produces: `ProviderQuery`, `ProviderPerson`, `SearchPage`, `EnrichmentInput`, `EnrichmentReceipt`, provider error classes, `QueryPlanner.compile(scorecard)`, and `ProviderGateway.search(query, page)`.

- [ ] **Step 1: Write failing query and Apollo contract tests**

```python
# backend/tests/unit/providers/test_query_planner.py
def test_query_planner_separates_exact_and_adjacent_industries(scorecard_factory) -> None:
    scorecard = scorecard_factory(
        target_titles=["Product Manager", "Senior Product Manager"],
        industry_code="financial_services.banking",
        adjacent_industries=["technology.fintech"],
    )
    queries = QueryPlanner(max_queries=8).compile(scorecard)
    assert 2 <= len(queries) <= 8
    assert any(query.industry_codes == ("financial_services.banking",) for query in queries)
    assert any(query.industry_codes == ("technology.fintech",) for query in queries)
```

```python
# backend/tests/contract/providers/test_apollo_search.py
def test_apollo_search_normalizes_people(respx_mock, apollo_gateway, provider_query) -> None:
    respx_mock.post("https://api.apollo.io/api/v1/mixed_people/api_search").mock(
        return_value=httpx.Response(
            200,
            json={"people": [{"id": "p1", "name": "Priya Sharma", "title": "Senior Product Manager", "organization": {"name": "PayFlow"}}]},
        )
    )
    page = apollo_gateway.search(provider_query, page=1)
    assert page.people[0].provider_person_id == "p1"
    assert page.people[0].current_title == "Senior Product Manager"
```

- [ ] **Step 2: Run provider tests and verify they fail**

Run: `cd backend && uv run pytest tests/unit/providers tests/contract/providers -v`

Expected: FAIL because provider contracts and the Apollo adapter do not exist.

- [ ] **Step 3: Define provider-neutral DTOs and failures**

```python
# backend/app/providers/base.py
@dataclass(frozen=True)
class ProviderQuery:
    titles: tuple[str, ...]
    seniorities: tuple[str, ...]
    person_locations: tuple[str, ...]
    industry_codes: tuple[str, ...]
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class ProviderPerson:
    provider: str
    provider_person_id: str
    full_name: str
    current_title: str | None
    current_company: str | None
    location: str | None
    linkedin_url: str | None
    experiences: tuple[ProviderExperience, ...]


class ProviderGateway(Protocol):
    def search(self, query: ProviderQuery, page: int) -> SearchPage:
        raise NotImplementedError

    def enrich_batch(
        self, people: tuple[EnrichmentInput, ...], webhook_url: str
    ) -> EnrichmentReceipt:
        raise NotImplementedError

    def poll_enrichment(self, request_id: str) -> EnrichmentResult | None:
        raise NotImplementedError
```

Define `ProviderRateLimited(retry_after)`, `ProviderAuthenticationError`, `ProviderPermissionError`, `ProviderTemporaryError`, and `ProviderPayloadError`. No Apollo response type may cross this boundary.

- [ ] **Step 4: Implement bounded query planning**

The planner creates separate exact-industry and adjacent-industry query groups, chunks alternate titles into groups of at most three, maps internal seniority to Apollo-supported values, and retains only job-relevant keywords. It emits at most eight stable, deduplicated queries and hashes their normalized JSON for idempotency.

- [ ] **Step 5: Implement Apollo search and error normalization**

Call `POST /api/v1/mixed_people/api_search` with `x-api-key`, a 20-second timeout, and a maximum of 100 people per page. Map internal titles, seniority, locations, and keywords to Apollo parameters. Stop paging after 300 unique provider IDs across the whole run.

Map `401` to authentication failure, `403` to permission failure, `429` to rate-limited using the reset header or usage endpoint, `5xx` to temporary failure, and an invalid `200` body to payload failure. Store the raw response only through the encrypted snapshot service added in Task 9.

- [ ] **Step 6: Test search pagination and rate limits**

Add contract fixtures for empty results, two pages, duplicate provider IDs, `401`, `403`, `429`, `500`, and malformed JSON.

Run: `cd backend && uv run pytest tests/unit/providers tests/contract/providers -v`

Expected: all provider tests pass without a live Apollo key.

- [ ] **Step 7: Commit the provider gateway**

```bash
git add backend/app/providers backend/app/core/config.py backend/tests
git commit -m "feat: add Apollo search provider gateway"
```

### Task 6: Agency-Scoped Candidate Identity and Provenance

**Files:**
- Create: `backend/app/candidates/models.py`
- Create: `backend/app/candidates/schemas.py`
- Create: `backend/app/candidates/normalization.py`
- Create: `backend/app/candidates/resolver.py`
- Create: `backend/app/candidates/service.py`
- Create: `backend/alembic/versions/0004_candidates.py`
- Create: `backend/tests/unit/candidates/test_normalization.py`
- Create: `backend/tests/integration/candidates/test_resolver.py`

**Interfaces:**
- Consumes: `ProviderPerson`, tenant-scoped SQLAlchemy session.
- Produces: `CandidateProfile`, `CandidateService.ingest(context, provider_person) -> ResolutionResult`, `CandidateResolver.resolve()`, and `DuplicateSuggestion`.

- [ ] **Step 1: Write failing normalization and resolution tests**

```python
# backend/tests/unit/candidates/test_normalization.py
def test_profile_url_normalization_removes_tracking() -> None:
    assert normalize_profile_url("https://www.linkedin.com/in/priya/?trk=search") == (
        "https://www.linkedin.com/in/priya"
    )
```

```python
# backend/tests/integration/candidates/test_resolver.py
def test_same_provider_id_reuses_candidate(candidate_service, context, provider_person_factory) -> None:
    person = provider_person_factory(provider_person_id="apollo-1")
    first = candidate_service.ingest(context, person)
    second = candidate_service.ingest(context, person)
    assert first.candidate_id == second.candidate_id
    assert second.created is False


def test_fuzzy_match_creates_suggestion_without_merge(candidate_service, context, provider_person_factory) -> None:
    first = provider_person_factory(provider_person_id="a", full_name="Priya Sharma")
    second = provider_person_factory(provider_person_id="b", full_name="Priya S Sharma")
    candidate_service.ingest(context, first)
    result = candidate_service.ingest(context, second)
    assert result.created is True
    assert result.duplicate_suggestion_id is not None
```

- [ ] **Step 2: Run candidate tests and verify they fail**

Run: `cd backend && uv run pytest tests/unit/candidates tests/integration/candidates -v`

Expected: FAIL because normalization and candidate tables are absent.

- [ ] **Step 3: Add candidate, source identity, experience, and suggestion models**

Create `Candidate`, `SourceIdentity`, `CandidateExperience`, and `DuplicateSuggestion`. Unique constraints are tenant-scoped. `SourceIdentity` is unique on `(tenant_id, provider, provider_person_id)` and `(tenant_id, provider, normalized_profile_url)` when the URL is non-null. A cross-provider URL lookup may reuse the canonical candidate while still retaining one source-identity row per provider. Apply and force RLS to every table.

- [ ] **Step 4: Implement deterministic resolution order**

```python
# backend/app/candidates/resolver.py
class CandidateResolver:
    def resolve(self, context: RequestContext, person: ProviderPerson) -> ResolutionDecision:
        by_provider = self._by_provider_id(context.tenant_id, person.provider, person.provider_person_id)
        if by_provider is not None:
            return ResolutionDecision.reuse(by_provider.id, "provider_id")
        by_url = self._by_profile_url(context.tenant_id, normalize_profile_url(person.linkedin_url))
        if by_url is not None:
            return ResolutionDecision.reuse(by_url.id, "profile_url")
        fuzzy = self._fuzzy_candidates(context.tenant_id, person)
        return ResolutionDecision.create_with_suggestions(fuzzy)
```

Verified-email resolution is added in Task 9 after encrypted contact points exist. Fuzzy similarity uses normalized name, employer, title, and location with explicit thresholds; it never returns an automatic merge decision.

- [ ] **Step 5: Preserve field-level provenance during ingestion**

For each normalized field store provider, source identity, source timestamp, and observed value hash. A new observation supersedes the display value only when it is newer and not lower-confidence. Never overwrite a non-empty value with an empty provider field.

- [ ] **Step 6: Run migration and repeat-ingestion tests**

Run: `cd backend && uv run alembic upgrade head && uv run pytest tests/unit/candidates tests/integration/candidates -v`

Expected: repeated ingestion is idempotent; fuzzy candidates remain separate and reviewable.

- [ ] **Step 7: Commit candidate identity**

```bash
git add backend/app/candidates backend/alembic/versions/0004_candidates.py backend/tests
git commit -m "feat: add agency candidate identity resolution"
```

### Task 7: Deterministic Matching and Near Matches

**Files:**
- Create: `backend/app/matching/schemas.py`
- Create: `backend/app/matching/engine.py`
- Create: `backend/app/matching/explanations.py`
- Create: `backend/tests/unit/matching/test_hard_gates.py`
- Create: `backend/tests/unit/matching/test_scoring.py`
- Create: `backend/tests/property/test_matching_determinism.py`

**Interfaces:**
- Consumes: `ConfirmedScorecard`, `CandidateProfile`, `IndustryTaxonomy`.
- Produces: `EvidenceState`, `CriterionEvaluation`, `ScoreBreakdown`, `MatchResult`, `MatchingEngine.evaluate(scorecard, candidate)`, and `format_explanation(match_result)`.

- [ ] **Step 1: Write failing hard-gate and scoring tests**

```python
# backend/tests/unit/matching/test_hard_gates.py
def test_failed_must_have_becomes_near_match(engine, scorecard_factory, candidate_factory) -> None:
    scorecard = scorecard_factory(must_have_skills=["payments"])
    candidate = candidate_factory(skills=["retail merchandising"])
    result = engine.evaluate(scorecard, candidate)
    assert result.classification == "near_match"
    assert result.failed_must_haves == ("payments",)
```

```python
# backend/tests/unit/matching/test_scoring.py
def test_unknown_evidence_is_zero_and_score_is_not_renormalized(engine, scorecard, candidate_factory) -> None:
    candidate = candidate_factory(work_eligibility=None)
    result = engine.evaluate(scorecard, candidate)
    assert result.breakdown.location_and_eligibility <= 10
    assert "work_eligibility" in result.unknown_keys
    assert result.total == sum(result.breakdown.model_dump().values())
```

- [ ] **Step 2: Run matching tests and verify they fail**

Run: `cd backend && uv run pytest tests/unit/matching tests/property/test_matching_determinism.py -v`

Expected: FAIL because the matching engine does not exist.

- [ ] **Step 3: Define evidence and result types**

```python
# backend/app/matching/schemas.py
class EvidenceState(StrEnum):
    SUPPORTED = "supported"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ScoreBreakdown(BaseModel):
    role_and_skills: int = Field(ge=0, le=35)
    scope_seniority_years: int = Field(ge=0, le=25)
    industry: int = Field(ge=0, le=20)
    location_and_eligibility: int = Field(ge=0, le=10)
    recency_and_trajectory: int = Field(ge=0, le=10)


class MatchResult(BaseModel):
    classification: Literal["main", "near_match"]
    total: int = Field(ge=0, le=100)
    breakdown: ScoreBreakdown
    criteria: tuple[CriterionEvaluation, ...]
    failed_must_haves: tuple[str, ...]
    unknown_keys: tuple[str, ...]
    scoring_version: str = "matching-v1"
```

- [ ] **Step 4: Implement hard gates and fixed-weight scorers**

Normalize skills and titles through explicit alias dictionaries. Award points only when evidence is supported. Exact industry receives all available industry points; recruiter-approved adjacency receives 60% of those points; unrelated or unknown industry receives zero. Enforce fixed maxima in `ScoreBreakdown` and calculate `total` solely as the sum.

An unsupported mandatory criterion yields `near_match`. An unknown must-have yields `near_match` only when `evidence_required=True`; otherwise it stays in the main ranking with zero criterion points and a visible uncertainty.

- [ ] **Step 5: Generate explanations only from stored evaluations**

```python
# backend/app/matching/explanations.py
def format_explanation(result: MatchResult) -> MatchExplanation:
    return MatchExplanation(
        supported=tuple(item.summary for item in result.criteria if item.state == EvidenceState.SUPPORTED),
        failed=tuple(item.summary for item in result.criteria if item.state == EvidenceState.FAILED),
        unknown=tuple(item.summary for item in result.criteria if item.state == EvidenceState.UNKNOWN),
    )
```

Do not call a language model to calculate or embellish evidence. UI copy may restate these stored facts but may not introduce facts absent from `CriterionEvaluation`.

- [ ] **Step 6: Add property tests for bounds and determinism**

Use Hypothesis to generate reordered skills, experiences, and criteria. Assert the same normalized inputs always produce identical classification, total, breakdown, and explanation; assert totals remain 0–100.

Run: `cd backend && uv run pytest tests/unit/matching tests/property/test_matching_determinism.py -v`

Expected: all matching tests pass.

- [ ] **Step 7: Commit the matching engine**

```bash
git add backend/app/matching backend/tests
git commit -m "feat: add deterministic candidate matching"
```

### Task 8: Durable Sourcing State Machine, Usage Budgets, and Audit

**Files:**
- Create: `backend/app/sourcing/models.py`
- Create: `backend/app/sourcing/schemas.py`
- Create: `backend/app/sourcing/state_machine.py`
- Create: `backend/app/sourcing/tasks.py`
- Create: `backend/app/sourcing/router.py`
- Create: `backend/app/audit/models.py`
- Create: `backend/app/audit/service.py`
- Create: `backend/app/worker.py`
- Create: `backend/alembic/versions/0005_sourcing_audit.py`
- Create: `backend/tests/unit/sourcing/test_state_machine.py`
- Create: `backend/tests/integration/sourcing/test_idempotent_run.py`
- Create: `backend/tests/integration/sourcing/test_usage_budget.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Consumes: `JobService`, `QueryPlanner`, `ProviderGateway`, `CandidateService`, `MatchingEngine`.
- Produces: `RunState`, `SourcingRun`, `RunCandidate`, `UsageLedger`, `TenantNotification`, `AuditService.record()`, `SourcingService.start()`, `transition_run()`, Celery tasks `plan_run`, `source_run`, `match_run`, and routes under `/api/v1/jobs/{job_id}/runs`.

- [ ] **Step 1: Write failing state and idempotency tests**

```python
# backend/tests/unit/sourcing/test_state_machine.py
def test_ready_cannot_transition_back_to_sourcing() -> None:
    with pytest.raises(InvalidTransition):
        transition_run(RunState.READY, RunState.SOURCING)
```

```python
# backend/tests/integration/sourcing/test_idempotent_run.py
def test_replayed_source_task_does_not_duplicate_candidates(run_factory, execute_source_task) -> None:
    run = run_factory(state="sourcing")
    execute_source_task(run.id, idempotency_key="source:q1:p1")
    execute_source_task(run.id, idempotency_key="source:q1:p1")
    assert run_candidate_count(run.id) == 100
    assert completed_checkpoint_count(run.id, "source:q1:p1") == 1
```

- [ ] **Step 2: Run sourcing tests and verify they fail**

Run: `cd backend && uv run pytest tests/unit/sourcing tests/integration/sourcing -v`

Expected: FAIL because sourcing run state and tasks do not exist.

- [ ] **Step 3: Add run, checkpoint, usage, and audit models**

Create `SourcingRun`, `RunCheckpoint`, `RunCandidate`, `UsageLedger`, `TenantNotification`, and append-only `AuditEvent`. `Job.status` owns Draft and Awaiting Scorecard; `SourcingRun` begins at Queued after confirmation. Enforce one active run per `(job_id, scorecard_version_id)` and unique `(run_id, idempotency_key)` checkpoints. `RunCandidate` is unique on `(run_id, candidate_id)` and stores the run's match result until Task 10 materializes the job CRM view. Usage rows record provider, endpoint, requested units, charged units, and provider request ID.

```python
# backend/app/sourcing/state_machine.py
ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset({RunState.SOURCING, RunState.CANCELLED, RunState.FAILED}),
    RunState.SOURCING: frozenset({RunState.MATCHING, RunState.PARTIALLY_READY, RunState.CANCELLED, RunState.FAILED}),
    RunState.MATCHING: frozenset({RunState.ENRICHING, RunState.PARTIALLY_READY, RunState.CANCELLED, RunState.FAILED}),
    RunState.ENRICHING: frozenset({RunState.READY, RunState.PARTIALLY_READY, RunState.CANCELLED, RunState.FAILED}),
    RunState.PARTIALLY_READY: frozenset({RunState.ENRICHING, RunState.READY, RunState.CANCELLED}),
    RunState.READY: frozenset(),
    RunState.CANCELLED: frozenset(),
    RunState.FAILED: frozenset(),
}
```

- [ ] **Step 4: Implement checkpointed Celery tasks**

Each task loads the run under `SELECT ... FOR UPDATE`, checks cancellation and the checkpoint key, commits work in bounded batches, then writes the checkpoint and state transition in the same transaction. Celery acknowledgement occurs after commit. Retries use exponential backoff with jitter and provider-supplied reset time for `429` responses.

`source_run` stops at 300 unique candidates. `match_run` persists score, classification, evidence, scorecard version, and scoring version for each run candidate. A failed provider page after at least one successful page yields `partially_ready`; a failure before any usable candidate yields `failed`.

- [ ] **Step 5: Enforce budgets before provider calls**

Store per-tenant and per-job caps for search pages, enrichments, and estimated credits. Reserve estimated units transactionally before a call, then reconcile with returned usage. When a cap would be exceeded, transition to `partially_ready` with error code `usage_budget_exhausted` and create an in-app `TenantNotification` for Owner and Admin roles. The notification is visible in Run Activity and the agency Settings alert list; email delivery is outside this slice.

- [ ] **Step 6: Add start, cancel, status, and activity routes**

`POST /api/v1/jobs/{job_id}/runs` requires a confirmed scorecard and an idempotency key. `POST /api/v1/runs/{run_id}/cancel` sets a cancellation request. `GET /api/v1/runs/{run_id}` returns state, counts, current stage, budget use, and sanitized errors. `GET /api/v1/runs/{run_id}/activity` returns tenant-scoped audit events. `GET /api/v1/notifications` and `PATCH /api/v1/notifications/{id}` list and acknowledge tenant alerts.

- [ ] **Step 7: Run orchestration tests**

Run: `cd backend && uv run alembic upgrade head && uv run pytest tests/unit/sourcing tests/integration/sourcing -v`

Expected: legal transitions pass, illegal transitions fail, replay creates no duplicates, and budget exhaustion affects only the tenant's run.

- [ ] **Step 8: Commit durable sourcing**

```bash
git add backend/app/sourcing backend/app/audit backend/app/worker.py backend/alembic/versions/0005_sourcing_audit.py backend/tests backend/app/main.py
git commit -m "feat: add durable sourcing orchestration"
```

### Task 9: Contact Enrichment, Encrypted Snapshots, and Replay-Safe Webhooks

**Files:**
- Create: `backend/app/providers/snapshots.py`
- Create: `backend/app/candidates/contacts.py`
- Create: `backend/app/sourcing/enrichment.py`
- Create: `backend/app/sourcing/webhooks.py`
- Create: `backend/alembic/versions/0006_contacts_enrichment.py`
- Create: `backend/tests/unit/candidates/test_contact_cipher.py`
- Create: `backend/tests/contract/providers/test_apollo_enrichment.py`
- Create: `backend/tests/integration/sourcing/test_enrichment_webhook.py`
- Modify: `backend/app/providers/base.py`
- Modify: `backend/app/providers/apollo.py`
- Modify: `backend/app/sourcing/tasks.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Consumes: `ProviderGateway.enrich_batch()`, `SourcingRun`, ranked run candidates, object-store settings.
- Produces: `ContactCipher`, `SnapshotStore.put()`, `SnapshotStore.delete()`, `EnrichmentRequest`, `EnrichmentWebhook`, `enqueue_top_enrichment(run_id, limit=50)`, `reveal_candidate_contact()`, and `/webhooks/apollo/{capability_token}`.

- [ ] **Step 1: Write failing encryption and webhook tests**

```python
# backend/tests/unit/candidates/test_contact_cipher.py
def test_contact_cipher_round_trip_without_plaintext_storage(cipher) -> None:
    encrypted = cipher.encrypt("priya@example.com")
    assert b"priya@example.com" not in encrypted.ciphertext
    assert cipher.decrypt(encrypted) == "priya@example.com"
```

```python
# backend/tests/integration/sourcing/test_enrichment_webhook.py
def test_duplicate_webhook_is_applied_once(api, pending_enrichment, apollo_phone_payload) -> None:
    path = f"/webhooks/apollo/{pending_enrichment.capability_token}"
    first = api.post(path, json=apollo_phone_payload)
    second = api.post(path, json=apollo_phone_payload)
    assert first.status_code == 202
    assert second.status_code == 202
    assert contact_count(pending_enrichment.candidate_id, kind="phone") == 1
```

- [ ] **Step 2: Run enrichment tests and verify they fail**

Run: `cd backend && uv run pytest tests/unit/candidates/test_contact_cipher.py tests/contract/providers/test_apollo_enrichment.py tests/integration/sourcing/test_enrichment_webhook.py -v`

Expected: FAIL because contact encryption and enrichment callbacks are absent.

- [ ] **Step 3: Add encrypted contact and enrichment models**

Create `ContactPoint`, `EnrichmentRequest`, and `WebhookDelivery`. Store contact value ciphertext, keyed lookup HMAC, type, work/personal classification, verification state, confidence, provider, observed timestamp, last-used timestamp, and expiry. Never store plaintext in searchable columns.

Use envelope encryption: generate a random data key per contact, encrypt the value with AES-256-GCM, and encrypt the data key with the configured key-encryption key. Include tenant ID, candidate ID, contact type, and schema version as authenticated associated data.

- [ ] **Step 4: Implement encrypted, expiring provider snapshots**

`SnapshotStore.put()` serializes canonical JSON, encrypts it, writes it under `tenant_id/run_id/provider/request_id`, and returns an opaque reference plus SHA-256 checksum and `expires_at=created_at+30 days`. The object lifecycle rule deletes at 30 days; a daily reconciliation task removes database references after deletion.

- [ ] **Step 5: Implement top-50 and on-demand enrichment**

Order main-ranking candidates by total descending and stable candidate ID tie-breaker. Reserve budget and enrich the first 50 in batches of at most ten. Set `reveal_personal_emails=true` and `reveal_phone_number=true` only when provider contract configuration and regional policy allow them.

Generate 32 random bytes for each callback capability token; put the token only in the HTTPS callback URL and store only its HMAC. Apollo documentation does not define a webhook signature, so the capability token, request ID match, payload schema, source rate limit, and idempotency record collectively protect the callback. Never log the callback URL. Expose on-demand enrichment as `POST /api/v1/job-candidates/{id}/enrich` with a fresh budget reservation and idempotency key.

- [ ] **Step 6: Add polling fallback and provider error behavior**

Persist Apollo's enrichment `request_id`. If no callback arrives by the stage deadline, call `GET /api/v1/webhook_result/{request_id}`. A provider `result_pending` response reschedules using `retry_after_seconds`; a result is applied through the same idempotent handler as a webhook. Enrichment failures mark contact status unavailable or failed without removing the candidate.

- [ ] **Step 7: Complete verified-email resolution**

After decrypting only inside the candidate service, compare the tenant-keyed normalized-email HMAC. A matching verified email may merge source identities when no provider-ID or name conflict exists. Conflicts create `DuplicateSuggestion` rather than an automatic merge.

- [ ] **Step 8: Run contract, replay, and secret-scanning tests**

Run: `cd backend && uv run alembic upgrade head && uv run pytest tests/unit/candidates/test_contact_cipher.py tests/contract/providers/test_apollo_enrichment.py tests/integration/sourcing/test_enrichment_webhook.py -v`

Expected: encryption round-trip passes, plaintext is absent, duplicate callbacks create one contact, and polling uses the same handler.

- [ ] **Step 9: Commit enrichment and contacts**

```bash
git add backend/app/providers backend/app/candidates backend/app/sourcing backend/alembic/versions/0006_contacts_enrichment.py backend/tests backend/app/main.py
git commit -m "feat: add encrypted contact enrichment"
```

### Task 10: CRM Review API, Activity, Filtering, and CSV Export

**Files:**
- Create: `backend/app/crm/models.py`
- Create: `backend/app/crm/schemas.py`
- Create: `backend/app/crm/service.py`
- Create: `backend/app/crm/exports.py`
- Create: `backend/app/crm/router.py`
- Create: `backend/app/candidates/router.py`
- Create: `backend/alembic/versions/0007_crm.py`
- Create: `backend/tests/unit/crm/test_stage_transitions.py`
- Create: `backend/tests/integration/crm/test_review_api.py`
- Create: `backend/tests/integration/crm/test_export_redaction.py`
- Create: `backend/tests/integration/crm/test_candidate_directory.py`
- Modify: `backend/app/sourcing/tasks.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Consumes: persisted matching results, `CandidateService`, `AuditService`, `RequestContext`.
- Produces: `CandidateStage`, `JobCandidateView`, `CandidateFilter`, `CrmService.transition()`, `CrmService.add_note()`, `CrmService.assign()`, review/table/near-match routes, and `export_shortlist_csv()`.

- [ ] **Step 1: Write failing stage and export tests**

```python
# backend/tests/unit/crm/test_stage_transitions.py
def test_rejection_requires_reason(crm_service, job_candidate, recruiter_context) -> None:
    with pytest.raises(ValidationError, match="rejection reason"):
        crm_service.transition(recruiter_context, job_candidate.id, "rejected", reason=None)
```

```python
# backend/tests/integration/crm/test_export_redaction.py
def test_export_contains_decrypted_contact_only_for_authorized_user(api, recruiter_token, shortlist) -> None:
    response = api.get(f"/api/v1/jobs/{shortlist.job_id}/export.csv", headers=recruiter_token)
    assert response.status_code == 200
    assert "work_email" in response.text
    assert "ciphertext" not in response.text
    assert "raw_snapshot" not in response.text


def test_acceptance_metric_uses_fixed_top_twenty(crm_service, ready_job_with_decisions) -> None:
    report = crm_service.acceptance_report(ready_job_with_decisions.id, as_of_day=7)
    assert report.denominator == 20
    assert report.accepted == report.reviewed + report.shortlisted
    assert report.rate == report.accepted / 20


def test_candidate_directory_hides_ungranted_client_jobs(api, recruiter_token, directory_fixture) -> None:
    response = api.get("/api/v1/candidates?q=Priya", headers=recruiter_token)
    assert response.status_code == 200
    assert response.json()["items"][0]["name"] == "Priya Sharma"
    assert response.json()["items"][0]["job_ids"] == [str(directory_fixture.granted_job_id)]
```

- [ ] **Step 2: Run CRM tests and verify they fail**

Run: `cd backend && uv run pytest tests/unit/crm tests/integration/crm -v`

Expected: FAIL because CRM records and routes do not exist.

- [ ] **Step 3: Add CRM records and allowed transitions**

Create `JobCandidate`, `CandidateNote`, `Tag`, `JobCandidateTag`, and `ActivityEvent`. `JobCandidate` is unique on `(job_id, candidate_id)` and stores classification, score JSON, scorecard version, scoring version, stage, owner, and rejection reason.

Allow New → Reviewed, Shortlisted, or Rejected; Reviewed → Shortlisted or Rejected; Shortlisted → Reviewed or Rejected; Rejected → Reviewed. Every transition records an append-only activity with actor and previous/new stage. Rejected requires a controlled reason code plus optional note.

- [ ] **Step 4: Persist match results and expose review queries**

Update the matching task to upsert `JobCandidate` records idempotently. Add:

- `GET /api/v1/jobs/{job_id}/candidates?classification=main&sort=-score`
- `GET /api/v1/jobs/{job_id}/candidates?classification=near_match`
- `GET /api/v1/job-candidates/{id}`
- `PATCH /api/v1/job-candidates/{id}/stage`
- `POST /api/v1/job-candidates/{id}/notes`
- `PATCH /api/v1/job-candidates/{id}/owner`
- `PUT /api/v1/job-candidates/{id}/tags`
- `GET /api/v1/jobs/{job_id}/acceptance`
- `GET /api/v1/candidates?q={text}&location={location}&industry={code}&cursor={cursor}`
- `GET /api/v1/candidates/{candidate_id}/jobs`

Job filters cover score range, stage, owner, tags, location, industry, contact availability, and text search. The agency candidate directory searches canonical name, current title, employer, normalized skills, and experience facts with PostgreSQL full-text and trigram indexes; it shows every authorized job match without exposing jobs outside the recruiter's client grants. Paginate with stable cursor `(score, id)` for ranked views and `(updated_at, id)` for activity and directory views. Acceptance reporting becomes final seven days after Ready, or earlier when all top-20 candidates have left New; Reviewed and Shortlisted count as accepted, while New and Rejected do not.

- [ ] **Step 5: Implement auditable contact reveal and CSV export**

Candidate detail returns masked contacts by default. `POST /api/v1/contact-points/{id}/reveal` verifies client access, decrypts the field, updates legitimate-use time, and records `contact_revealed`. CSV export includes only Shortlisted candidates, creates an export audit event, and streams rows without writing a plaintext file to disk or object storage.

- [ ] **Step 6: Run CRM and tenant-isolation tests**

Run: `cd backend && uv run alembic upgrade head && uv run pytest tests/unit/crm tests/integration/crm tests/integration/identity/test_tenant_isolation.py -v`

Expected: transitions, filters, reveal auditing, export redaction, and tenant isolation pass.

- [ ] **Step 7: Commit the CRM API**

```bash
git add backend/app/crm backend/app/candidates/router.py backend/app/sourcing/tasks.py backend/alembic/versions/0007_crm.py backend/tests backend/app/main.py
git commit -m "feat: add candidate review CRM API"
```

### Task 11: Privacy Requests, Retention, Deletion, and Suppression

**Files:**
- Create: `backend/app/privacy/models.py`
- Create: `backend/app/privacy/schemas.py`
- Create: `backend/app/privacy/service.py`
- Create: `backend/app/privacy/tasks.py`
- Create: `backend/app/privacy/router.py`
- Create: `backend/alembic/versions/0008_privacy.py`
- Create: `backend/tests/unit/privacy/test_suppression.py`
- Create: `backend/tests/integration/privacy/test_candidate_deletion.py`
- Create: `backend/tests/integration/privacy/test_retention.py`
- Modify: `backend/app/candidates/service.py`
- Modify: `backend/app/worker.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Consumes: `ContactCipher`, `SnapshotStore`, `CandidateService`, `AuditService`.
- Produces: `PrivacyRequestType`, `PrivacyRequestState`, `SuppressionService.digest()`, `PrivacyService.submit()`, `PrivacyService.execute()`, `PrivacyService.execute_delete()`, `expire_contacts()`, `expire_snapshots()`, and `/api/v1/privacy-requests` routes.

- [ ] **Step 1: Write failing suppression and deletion tests**

```python
# backend/tests/unit/privacy/test_suppression.py
def test_suppression_digest_is_tenant_scoped(suppression_service) -> None:
    first = suppression_service.digest(TENANT_A, "email", "Priya@Example.com")
    second = suppression_service.digest(TENANT_A, "email", "priya@example.com")
    other_tenant = suppression_service.digest(TENANT_B, "email", "priya@example.com")
    assert first == second
    assert first != other_tenant
```

```python
# backend/tests/integration/privacy/test_candidate_deletion.py
def test_deletion_removes_personal_data_and_blocks_reimport(privacy_service, candidate, provider_person) -> None:
    privacy_service.execute_delete(candidate.tenant_id, candidate.id)
    assert candidate_personal_fields(candidate.id) == {}
    result = ingest_provider_person(candidate.tenant_id, provider_person)
    assert result.suppressed is True
```

- [ ] **Step 2: Run privacy tests and verify they fail**

Run: `cd backend && uv run pytest tests/unit/privacy tests/integration/privacy -v`

Expected: FAIL because privacy and suppression services do not exist.

- [ ] **Step 3: Add privacy-request and suppression records**

Create `PrivacyRequest` and `SuppressionIdentifier`. Types are Access, Correction, Deletion, and Opt Out; states are Received, Identity Verification Required, Approved, Executing, Completed, and Rejected. Store only tenant-keyed HMAC digests for suppression identifiers. Never store the normalized identifier or contact ciphertext on a suppression row.

- [ ] **Step 4: Implement deletion as a resumable workflow**

Deletion checkpoints: collect HMACs, delete contacts, delete snapshots, redact profile and experience fields, delete candidate notes, preserve legally required non-personal audit metadata, write suppression rows, and complete the request. CSV exports are streamed and never persisted, so there is no export artifact to delete. A retry begins after the last committed checkpoint. Access and correction requests use the same authorization and audit framework.

- [ ] **Step 5: Enforce suppression before candidate persistence**

For each incoming provider person, calculate digests for available provider ID, normalized profile URL, and contact identifiers. If any digest matches the tenant suppression set, record only a non-personal suppressed-import audit event and do not create or update candidate records.

- [ ] **Step 6: Add daily retention tasks**

At 02:00 UTC, expire contact points whose `max(last_verified_at, last_used_at)` is older than 180 days. Delete the ciphertext and wrapped key, retain only non-reversible expiry audit metadata, and mark the contact expired. Reconcile snapshots whose 30-day expiry passed and raise an alert if object deletion fails for more than 24 hours.

- [ ] **Step 7: Add privacy routes and verify full lifecycle**

Owners and Admins may submit and operate requests; recruiters may submit but not approve deletion. Add request create, list, verify, approve, and status routes. Provider-specific shorter retention configuration wins over platform defaults.

Run: `cd backend && uv run alembic upgrade head && uv run pytest tests/unit/privacy tests/integration/privacy -v`

Expected: normalized identifiers suppress within one tenant, not across tenants; deletion is replay-safe; 180-day and 30-day expiry tests pass under a frozen clock.

- [ ] **Step 8: Commit privacy and retention**

```bash
git add backend/app/privacy backend/app/candidates/service.py backend/app/worker.py backend/alembic/versions/0008_privacy.py backend/tests backend/app/main.py
git commit -m "feat: add privacy and retention workflows"
```

### Task 12: Authenticated Web Shell, Client Selection, and Scorecard Flow

**Files:**
- Create: `web/package.json`
- Create: `web/tsconfig.json`
- Create: `web/next.config.ts`
- Create: `web/vitest.config.ts`
- Create: `web/app/globals.css`
- Create: `web/app/api/auth/[...nextauth]/route.ts`
- Create: `web/app/(app)/layout.tsx`
- Create: `web/app/(app)/jobs/page.tsx`
- Create: `web/app/(app)/jobs/new/page.tsx`
- Create: `web/app/(app)/jobs/[jobId]/scorecard/page.tsx`
- Create: `web/app/(app)/clients/page.tsx`
- Create: `web/components/layout/AppShell.tsx`
- Create: `web/components/clients/ClientManager.tsx`
- Create: `web/components/jobs/JobList.tsx`
- Create: `web/components/jobs/JobIntakeForm.tsx`
- Create: `web/components/scorecards/ScorecardEditor.tsx`
- Create: `web/lib/auth.ts`
- Create: `web/lib/api.ts`
- Create: `web/lib/schemas.ts`
- Create: `web/tests/fixtures.ts`
- Create: `web/tests/setup.ts`
- Create: `web/tests/jobs/job-intake.test.tsx`
- Create: `web/tests/clients/client-manager.test.tsx`
- Create: `web/tests/scorecards/scorecard-editor.test.tsx`

**Interfaces:**
- Consumes: OIDC issuer/client configuration; client and job REST APIs; scorecard schemas from the OpenAPI document.
- Produces: authenticated app shell, typed `apiFetch<T>()`, `JobIntakeForm`, `ScorecardEditor`, and job/scorecard pages.

- [ ] **Step 1: Write failing intake and scorecard UI tests**

```tsx
// web/tests/jobs/job-intake.test.tsx
it("requires a client and job description", async () => {
  render(<JobIntakeForm clients={[{ id: "c1", name: "PayFlow" }]} />)
  await userEvent.click(screen.getByRole("button", { name: "Generate scorecard" }))
  expect(await screen.findByText("Select a client")).toBeVisible()
  expect(screen.getByText("Enter a job description")).toBeVisible()
})
```

```tsx
// web/tests/scorecards/scorecard-editor.test.tsx
it("separates extracted criteria from inferred suggestions", () => {
  render(<ScorecardEditor draft={scorecardDraftFixture} />)
  expect(screen.getByText("From job description")).toBeVisible()
  expect(screen.getByText("Suggested — confirm before use")).toBeVisible()
  expect(screen.getByRole("button", { name: "Confirm and source" })).toBeDisabled()
})
```

- [ ] **Step 2: Run frontend tests and verify they fail**

Run: `cd web && npm test -- --run tests/jobs/job-intake.test.tsx tests/scorecards/scorecard-editor.test.tsx`

Expected: FAIL because the web application and components do not exist.

- [ ] **Step 3: Scaffold Next.js, strict TypeScript, and generated API types**

Configure strict TypeScript, ESLint, Vitest with jsdom, Testing Library, Mock Service Worker, Playwright, TanStack Query, React Hook Form, and Zod. Add `npm run api:generate` to generate `web/lib/generated-api.ts` from the backend OpenAPI JSON. CI must fail when the generated client differs from the committed file. Run `npm install` once and commit `package-lock.json`; all later installs use `npm ci`.

- [ ] **Step 4: Add Auth.js OIDC and the API client**

Configure one OIDC provider from `OIDC_ISSUER`, `OIDC_CLIENT_ID`, and `OIDC_CLIENT_SECRET`. Store access tokens only in encrypted, HTTP-only session cookies. The agency switcher stores the selected tenant ID in a signed, HTTP-only cookie; the backend still verifies membership on every request. Route server-side API calls through `apiFetch`, which attaches the access token, `X-Tenant-ID`, and a caller-supplied stable idempotency key for mutations.

```typescript
// web/lib/api.ts
type ApiInit = RequestInit & { idempotencyKey?: string }

export async function apiFetch<T>(path: string, tenantId: string, init: ApiInit = {}): Promise<T> {
  const session = await auth()
  if (!session?.accessToken) throw new Error("unauthenticated")
  const { idempotencyKey, ...requestInit } = init
  const headers = new Headers(init.headers)
  headers.set("Authorization", `Bearer ${session.accessToken}`)
  headers.set("X-Tenant-ID", tenantId)
  headers.set("Accept", "application/json")
  if (init.method && init.method !== "GET") {
    if (!idempotencyKey) throw new Error("idempotency_key_required")
    headers.set("Idempotency-Key", idempotencyKey)
  }
  const response = await fetch(`${process.env.API_BASE_URL}${path}`, { ...requestInit, headers, cache: "no-store" })
  if (!response.ok) throw await ApiError.fromResponse(response)
  return response.json() as Promise<T>
}
```

- [ ] **Step 5: Build the agency shell, client manager, and job list**

Match the approved structure: top navigation for Jobs, Candidates, Clients, and Settings; agency identity and user menu; active-job sidebar; responsive main content. Owners and Admins can create clients, choose the primary industry, approve adjacent industries, and grant recruiter access. Recruiters see only authorized clients. Render only routes allowed by the current role. Do not expose provider credentials or platform-operator controls.

- [ ] **Step 6: Build job intake and scorecard confirmation**

Job intake selects an authorized client, captures the job description, location, and employment model, then calls scorecard generation. The editor groups must-haves, preferences, exclusions, inferred adjacent industries, and uncertainties. Every inferred item requires explicit confirmation or deletion. The Confirm and Source button submits the expected revision, then starts a run using the confirmed version.

Validation and server errors stay attached to the relevant field. A model extraction failure opens the same editor with `manual_required` status and no invented criteria.

- [ ] **Step 7: Run unit and accessibility checks**

Run: `cd web && npm run lint && npm run typecheck && npm test -- --run && npm run test:a11y`

Expected: commands exit 0; keyboard navigation, labels, focus order, and error announcements pass.

- [ ] **Step 8: Commit the job and scorecard web flow**

```bash
git add web
git commit -m "feat: add authenticated job scorecard flow"
```

### Task 13: Live Run Progress and Candidate Review Workspace

**Files:**
- Create: `web/app/(app)/jobs/[jobId]/page.tsx`
- Create: `web/app/(app)/jobs/[jobId]/loading.tsx`
- Create: `web/app/(app)/candidates/page.tsx`
- Create: `web/components/jobs/RunStatus.tsx`
- Create: `web/components/candidates/ReviewWorkspace.tsx`
- Create: `web/components/candidates/RankedCandidateList.tsx`
- Create: `web/components/candidates/CandidateDetail.tsx`
- Create: `web/components/candidates/CandidateTable.tsx`
- Create: `web/components/candidates/NearMatches.tsx`
- Create: `web/components/candidates/ActivityPanel.tsx`
- Create: `web/components/candidates/ContactReveal.tsx`
- Create: `web/components/candidates/CandidateDirectory.tsx`
- Create: `web/components/layout/AgencyAlerts.tsx`
- Create: `web/components/layout/MembershipManager.tsx`
- Create: `web/app/(app)/settings/page.tsx`
- Create: `web/tests/candidates/review-workspace.test.tsx`
- Create: `web/tests/candidates/near-matches.test.tsx`
- Create: `web/tests/candidates/contact-reveal.test.tsx`
- Create: `web/tests/candidates/candidate-directory.test.tsx`
- Create: `web/tests/settings/membership-manager.test.tsx`
- Create: `web/e2e/job-review.spec.ts`

**Interfaces:**
- Consumes: run status/activity, candidate list/detail, stage, note, assignment, tag, enrichment, reveal, and CSV export APIs.
- Produces: progressive run status, selected review workspace, all-candidates table, near-match view, scorecard/activity tabs, contact reveal, and shortlist export.

- [ ] **Step 1: Write failing review and near-match tests**

```tsx
// web/tests/candidates/review-workspace.test.tsx
it("shows evidence and uncertainty for the selected ranked candidate", async () => {
  render(<ReviewWorkspace jobId="j1" initialCandidates={[priyaFixture, marcusFixture]} />)
  await userEvent.click(screen.getByRole("button", { name: /Priya Sharma.*92/ }))
  expect(screen.getByText("5 years in payments and fintech")).toBeVisible()
  expect(screen.getByText("US market experience is unknown")).toBeVisible()
})
```

```tsx
// web/tests/candidates/near-matches.test.tsx
it("names the failed mandatory criterion", () => {
  render(<NearMatches candidates={[nearMatchFixture]} />)
  expect(screen.getByText("Missing required payments experience")).toBeVisible()
})
```

- [ ] **Step 2: Run focused UI tests and verify they fail**

Run: `cd web && npm test -- --run tests/candidates`

Expected: FAIL because review components do not exist.

- [ ] **Step 3: Implement progressive run polling**

Use TanStack Query to poll run status every three seconds while the run is non-terminal and every ten seconds while partially ready. Fetch candidate pages after the matching count changes. Stop polling in Ready, Cancelled, or Failed. Show counts for sourced, matched, enriched, failed, budget used, and sanitized stage error. Cancellation requires confirmation and leaves already available candidates visible.

- [ ] **Step 4: Implement the selected review workspace**

Render the ranked list on the left and evidence-backed detail on the right. Preserve selected candidate ID in the URL query string. Candidate detail shows score breakdown, supported facts, failed facts, unknowns, normalized experience, masked contact availability, provider provenance, stage actions, owner, tags, and notes.

Stage mutations generate one idempotency key when the user acts and reuse it across network retries. They use optimistic UI with rollback on API error. Reject opens a controlled reason selector. Mark Reviewed, Shortlist, Reject, Add Note, Assign Owner, and Tag remain usable by keyboard.

- [ ] **Step 5: Add table, near-match, scorecard, and activity tabs**

The table uses server-side cursor pagination, sorting, and filters. Bulk actions are limited to stage and ownership updates and show an affected-record count. Near Matches always display failed or unknown mandatory criteria before the score. Scorecard shows the immutable version used for the run. Activity never renders raw provider errors or contact values.

The Candidates page uses the agency directory API to search canonical people and open their authorized job-match history. It never displays that the candidate exists in another tenant.

`AgencyAlerts` lists unread tenant notifications in Settings and links a budget alert back to its Run Activity tab. Acknowledgement updates the alert without deleting its audit history. `MembershipManager` lets Owners and Admins create one-time invitation links, change allowed roles, deactivate memberships, and copy an unexpired link; it never accepts an unverified-email claim.

- [ ] **Step 6: Add contact reveal, on-demand enrichment, and export**

If a contact is available, require an explicit reveal click, show it only in the current authenticated view, and avoid browser persistence. If unavailable and the candidate is outside the top 50, offer Enrich Contact with an estimated credit warning. CSV export is available only from the Shortlisted filter and downloads the streaming response without caching.

- [ ] **Step 7: Add the end-to-end happy path**

```typescript
// web/e2e/job-review.spec.ts
test("job description to shortlisted candidate", async ({ page }) => {
  await page.goto("/jobs/new")
  await page.getByLabel("Client").selectOption("payflow")
  await page.getByLabel("Job description").fill(productManagerJobDescription)
  await page.getByRole("button", { name: "Generate scorecard" }).click()
  await page.getByRole("checkbox", { name: "Confirm suggested adjacent industry" }).check()
  await page.getByRole("button", { name: "Confirm and source" }).click()
  await expect(page.getByText("Reviewing")).toBeVisible()
  await page.getByRole("button", { name: /Priya Sharma.*92/ }).click()
  await page.getByRole("button", { name: "Shortlist" }).click()
  await expect(page.getByText("Shortlisted")).toBeVisible()
})
```

- [ ] **Step 8: Run frontend and E2E tests**

Run: `cd web && npm run lint && npm run typecheck && npm test -- --run && npm run e2e -- job-review.spec.ts`

Expected: all unit, accessibility, and happy-path tests pass against provider and LLM fakes.

- [ ] **Step 9: Commit the review workspace**

```bash
git add web
git commit -m "feat: add sourcing review workspace"
```

### Task 14: Evaluation, Observability, Deployment, and Launch Gates

**Files:**
- Create: `backend/app/core/telemetry.py`
- Create: `backend/Dockerfile`
- Create: `web/Dockerfile`
- Create: `.github/workflows/ci.yml`
- Create: `evaluation/fixtures/jobs.jsonl`
- Create: `evaluation/fixtures/judgments.jsonl`
- Create: `evaluation/evaluate_matching.py`
- Create: `evaluation/test_evaluation_schema.py`
- Create: `loadtests/k6-sourcing.js`
- Create: `docs/runbooks/backup-restore.md`
- Create: `docs/runbooks/provider-outage.md`
- Create: `docs/runbooks/rollback.md`
- Create: `backend/tests/integration/test_observability_redaction.py`
- Create: `backend/tests/integration/test_backup_restore.py`
- Create: `web/e2e/tenant-isolation.spec.ts`
- Modify: `backend/app/main.py`
- Modify: `backend/app/worker.py`
- Modify: `compose.yaml`

**Interfaces:**
- Consumes: all completed backend and web interfaces.
- Produces: redacted telemetry, matching evaluation report, CI release gates, production containers, load scenario, restore proof, provider-outage procedure, and rollback procedure.

- [ ] **Step 1: Write failing telemetry and evaluation tests**

```python
# backend/tests/integration/test_observability_redaction.py
def test_contact_values_never_reach_logs(captured_logs, reveal_contact) -> None:
    reveal_contact("priya@example.com")
    serialized = "\n".join(captured_logs)
    assert "priya@example.com" not in serialized
    assert "contact_revealed" in serialized
```

```python
# evaluation/test_evaluation_schema.py
def test_evaluation_has_thirty_jobs_across_both_markets() -> None:
    jobs = load_jobs("evaluation/fixtures/jobs.jsonl")
    assert len(jobs) >= 30
    assert {job.market for job in jobs} == {"IN", "US"}
    assert all(job.judgment_count >= 20 for job in jobs)
```

- [ ] **Step 2: Run launch-gate tests and verify they fail**

Run: `cd backend && uv run pytest tests/integration/test_observability_redaction.py ../evaluation/test_evaluation_schema.py -v`

Expected: FAIL because telemetry redaction and evaluation fixtures are absent.

- [ ] **Step 3: Add structured, redacted telemetry**

Instrument API and Celery entry points with run ID, tenant ID, job ID, stage, duration, outcome, provider endpoint, retry count, and budget units. Hash user and candidate identifiers before sending them to telemetry. Configure a processor that drops keys matching `email`, `phone`, `token`, `authorization`, `api_key`, `payload`, and `snapshot` before logs, traces, or metrics leave the process.

Expose Prometheus metrics for API latency, queue depth, stage duration, provider error rate, retries, stuck runs, budget exhaustion, webhook failures, privacy failures, and snapshot expiry. Add alerts at the thresholds documented in the approved spec and provider-outage runbook.

- [ ] **Step 4: Build the versioned matching evaluation**

Implement a validated JSONL importer and annotation guide, then load the recruiter-panel dataset required by Global Constraints: at least 30 de-identified jobs split across India and US, with at least 20 recruiter judgments per job. `evaluate_matching.py` loads one scorecard and candidate set at a time, runs `MatchingEngine`, and outputs hard-gate precision/recall, NDCG@20, top-20 acceptance proxy, and differences from the committed baseline JSON. A two-job synthetic smoke fixture may test the importer, but if the recruiter dataset is unavailable, stop this task and report the unmet launch prerequisite rather than fabricating labels.

Fail CI if any mandatory hard-gate fixture changes classification, hard-gate F1 falls by more than 0.01, or NDCG@20 falls by more than 0.03. Scoring-version changes must commit a new baseline and an evaluation report.

- [ ] **Step 5: Add production containers and CI**

Use non-root, multi-stage images with locked dependencies and read-only application filesystems. CI jobs run backend lint/type/tests, migration upgrade and downgrade checks, frontend lint/type/unit/accessibility tests, Playwright, OpenAPI client-drift check, evaluation gates, dependency audit, secret scan, container scan, and `git diff --check`.

- [ ] **Step 6: Implement the launch-capacity load scenario**

`k6-sourcing.js` provisions 25 tenant tokens, ramps to 250 active users, and starts 25 concurrent runs against a deterministic fake provider that returns 300 profiles per run. It polls progress, pages candidates, opens detail, transitions stages, and exports a shortlist. Thresholds:

- foreground read API p95 below 500 ms and p99 below 1,000 ms
- mutation API p95 below 750 ms
- error rate below 1%
- exactly 7,500 unique run-candidate records
- zero duplicate canonical records for repeated provider IDs within a tenant
- zero cross-tenant response records
- all queues drain within ten minutes after provider completion

- [ ] **Step 7: Prove backup, restore, and rollback**

The backup runbook covers PostgreSQL point-in-time recovery, object-store versioning, secret references, restore into an isolated environment, integrity counts, and tenant-isolation smoke tests. Automate a weekly restore rehearsal and alert on failure. The rollback runbook uses backward-compatible migrations, previous application images, worker drain, and feature flags for provider and enrichment stages.

- [ ] **Step 8: Add provider-outage and privacy smoke tests**

Exercise `401`, `403`, fixed-window `429`, `5xx`, malformed payload, lost webhook with polling recovery, quota exhaustion, stuck retention, and object deletion failure. Verify platform-wide provider authentication failure alerts operators, while tenant budget exhaustion pauses only that tenant.

- [ ] **Step 9: Run the complete release gate locally**

Run: `docker compose up -d --wait && cd backend && uv run alembic upgrade head && uv run ruff check . && uv run mypy app && uv run pytest -v --cov=app --cov-fail-under=90`

Run: `cd web && npm ci && npm run lint && npm run typecheck && npm test -- --run && npm run e2e`

Run: `cd backend && uv run pytest ../evaluation/test_evaluation_schema.py -v && uv run python ../evaluation/evaluate_matching.py --fail-on-regression`

Run: `k6 run loadtests/k6-sourcing.js`

Expected: every command exits 0, migration is at head, coverage is at least 90%, evaluation has no regression, and all load thresholds pass.

- [ ] **Step 10: Commit production readiness**

```bash
git add .github backend web evaluation loadtests docs/runbooks compose.yaml
git commit -m "feat: add production launch gates"
```

## Final Verification

- [ ] Run `git status --short` and confirm only intentional files are tracked or modified.
- [ ] Run `git log --oneline --decorate -15` and confirm one focused commit per task.
- [ ] Run the complete release gate from Task 14 Step 9 on a clean clone.
- [ ] Provision a staging agency with two clients and two recruiters with different client grants.
- [ ] Run one India job and one US job against the contracted Apollo account with low budgets.
- [ ] Confirm 100–300 profiles, deterministic deduplication, main/near-match separation, top-50 enrichment, contact reveal audit, and CSV export.
- [ ] Submit a deletion request, rerun sourcing, and confirm suppression prevents re-import.
- [ ] Obtain security review, privacy review for India and the US, provider-contract approval, backup-restore evidence, and launch sign-off.

## Implementation References

- Design: `docs/superpowers/specs/2026-08-15-recruitment-sourcing-agent-vertical-slice-design.md`
- Apollo authentication: <https://docs.apollo.io/reference/authentication>
- Apollo people search: <https://docs.apollo.io/reference/people-api-search>
- Apollo people enrichment: <https://docs.apollo.io/reference/people-enrichment>
- Apollo bulk enrichment: <https://docs.apollo.io/reference/bulk-people-enrichment>
- Apollo enrichment polling: <https://docs.apollo.io/reference/poll-webhook-result>
- Apollo rate limits: <https://docs.apollo.io/reference/rate-limits>
