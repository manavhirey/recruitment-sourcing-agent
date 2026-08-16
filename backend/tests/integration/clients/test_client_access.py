from collections.abc import Generator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.clients.models import ClientCompany
from app.core.config import Settings
from app.core.database import Base, get_db
from app.identity.models import Membership, Tenant, User
from app.identity.schemas import IdentityClaims, Role
from app.main import create_app


class StaticVerifier:
    def __init__(self, claims: IdentityClaims) -> None:
        self.claims = claims

    def verify(self, token: str) -> IdentityClaims:
        return self.claims


@pytest.fixture
def client_access_api(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[dict[str, Any], None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    tenant = Tenant(id=uuid4(), slug="agency")
    tenant_id = tenant.id
    owner = User(
        id=uuid4(),
        oidc_subject="oidc|owner",
        email="owner@agency.test",
        display_name="Owner",
    )
    recruiter = User(
        id=uuid4(),
        oidc_subject="oidc|recruiter",
        email="recruiter@agency.test",
        display_name="Recruiter",
    )
    with Session(engine) as session:
        session.add_all((tenant, owner, recruiter))
        session.flush()
        recruiter_membership = Membership(
            tenant_id=tenant.id,
            user_id=recruiter.id,
            role=Role.RECRUITER,
            allowed_client_ids=[],
        )
        session.add_all(
            (
                Membership(tenant_id=tenant.id, user_id=owner.id, role=Role.OWNER),
                recruiter_membership,
            )
        )
        session.flush()
        recruiter_membership_id = recruiter_membership.id
        session.commit()

    app = create_app(Settings.for_test())
    verifier = StaticVerifier(
        IdentityClaims(
            subject="oidc|recruiter",
            email="recruiter@agency.test",
            name="Recruiter",
            email_verified=True,
        )
    )
    app.state.token_verifier = verifier

    def database_session() -> Generator[Session, None, None]:
        with Session(engine) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    app.dependency_overrides[get_db] = database_session
    monkeypatch.setattr(
        "app.identity.dependencies.apply_tenant_context",
        lambda session, tenant_id: None,
    )
    with TestClient(app) as api:
        yield {
            "api": api,
            "engine": engine,
            "tenant_id": tenant_id,
            "recruiter_membership_id": recruiter_membership_id,
            "verifier": verifier,
        }


@pytest.fixture
def client_factory(client_access_api: dict[str, Any]):
    def create_client(*, name: str) -> ClientCompany:
        client = ClientCompany(
            id=uuid4(),
            tenant_id=client_access_api["tenant_id"],
            name=name,
            normalized_name=name.casefold(),
        )
        with Session(client_access_api["engine"], expire_on_commit=False) as session:
            session.add(client)
            session.commit()
        return client

    return create_client


def test_recruiter_cannot_read_ungranted_client(
    client_access_api: dict[str, Any], client_factory
) -> None:
    hidden = client_factory(name="Hidden Client")

    response = client_access_api["api"].get(
        f"/api/v1/clients/{hidden.id}",
        headers={
            "Authorization": "Bearer signed-token",
            "X-Tenant-ID": str(client_access_api["tenant_id"]),
        },
    )

    assert response.status_code == 404


def test_owner_grant_allows_recruiter_to_read_only_granted_client(
    client_access_api: dict[str, Any], client_factory
) -> None:
    headers = {
        "Authorization": "Bearer signed-token",
        "X-Tenant-ID": str(client_access_api["tenant_id"]),
    }
    client_access_api["verifier"].claims = IdentityClaims(
        subject="oidc|owner",
        email="owner@agency.test",
        name="Owner",
        email_verified=True,
    )
    created = client_access_api["api"].post(
        "/api/v1/clients",
        headers={**headers, "Idempotency-Key": "create-granted-client"},
        json={"name": "Granted Client", "industry_codes": ["technology.fintech"]},
    )
    created_retry = client_access_api["api"].post(
        "/api/v1/clients",
        headers={**headers, "Idempotency-Key": "create-granted-client"},
        json={"name": "Granted Client", "industry_codes": ["technology.fintech"]},
    )
    assert created.status_code == 201
    assert created_retry.json() == created.json()
    client_id = created.json()["id"]
    grant = client_access_api["api"].post(
        f"/api/v1/clients/{client_id}/grants",
        headers={**headers, "Idempotency-Key": "grant-client"},
        json={"membership_id": str(client_access_api["recruiter_membership_id"])},
    )
    assert grant.status_code == 200

    client_access_api["verifier"].claims = IdentityClaims(
        subject="oidc|recruiter",
        email="recruiter@agency.test",
        name="Recruiter",
        email_verified=True,
    )
    visible = client_access_api["api"].get(
        f"/api/v1/clients/{client_id}", headers=headers
    )
    hidden = client_factory(name="Not Granted")
    not_visible = client_access_api["api"].get(
        f"/api/v1/clients/{hidden.id}", headers=headers
    )

    assert visible.status_code == 200
    assert not_visible.status_code == 404


def test_owner_cannot_approve_adjacency_from_unassigned_industry(
    client_access_api: dict[str, Any],
) -> None:
    headers = {
        "Authorization": "Bearer signed-token",
        "X-Tenant-ID": str(client_access_api["tenant_id"]),
    }
    client_access_api["verifier"].claims = IdentityClaims(
        subject="oidc|owner",
        email="owner@agency.test",
        name="Owner",
        email_verified=True,
    )
    created = client_access_api["api"].post(
        "/api/v1/clients",
        headers={**headers, "Idempotency-Key": "create-healthcare-client"},
        json={"name": "Healthcare Client", "industry_codes": ["healthcare"]},
    )
    assert created.status_code == 201

    approval = client_access_api["api"].put(
        f"/api/v1/clients/{created.json()['id']}/adjacent-industries",
        headers={**headers, "Idempotency-Key": "approve-unassigned-adjacency"},
        json={
            "industry_code": "financial_services.banking",
            "adjacent_industry_code": "technology.fintech",
        },
    )

    assert approval.status_code == 400
    assert approval.json() == {"detail": {"code": "client_industry_not_assigned"}}
