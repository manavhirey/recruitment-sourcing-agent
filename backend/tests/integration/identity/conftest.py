import os
from collections.abc import Callable, Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.identity.models import Tenant

API_ROLE = "sourcing_api_test"
API_PASSWORD = "sourcing-api-test"


@pytest.fixture(scope="session")
def owner_engine() -> Generator[Engine, None, None]:
    database_url = os.getenv("TEST_DATABASE_URL", Settings.for_test().database_url)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        database_name = connection.scalar(text("SELECT current_database()"))
        assert isinstance(database_name, str)
        role_exists = connection.scalar(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
            {"role": API_ROLE},
        )
        if role_exists is None:
            connection.execute(
                text(f"CREATE ROLE \"{API_ROLE}\" LOGIN PASSWORD '{API_PASSWORD}'")
            )
        quoted_database = database_name.replace('"', '""')
        connection.execute(
            text(f'GRANT CONNECT ON DATABASE "{quoted_database}" TO "{API_ROLE}"')
        )
        connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{API_ROLE}"'))
        connection.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                f'IN SCHEMA public TO "{API_ROLE}"'
            )
        )
    yield engine
    engine.dispose()


@pytest.fixture
def owner_session(owner_engine: Engine) -> Generator[Session, None, None]:
    factory = sessionmaker(bind=owner_engine, expire_on_commit=False)
    with factory() as session:
        yield session
        session.rollback()
        session.execute(text("DELETE FROM identity_idempotency_keys"))
        session.execute(text("DELETE FROM membership_invitations"))
        session.execute(text("DELETE FROM memberships"))
        session.execute(text("DELETE FROM users"))
        session.execute(text("DELETE FROM tenants"))
        session.commit()


@pytest.fixture
def session(owner_engine: Engine) -> Generator[Session, None, None]:
    owner_url = make_url(str(owner_engine.url))
    api_url = owner_url.set(username=API_ROLE, password=API_PASSWORD)
    api_engine = create_engine(api_url)
    factory = sessionmaker(bind=api_engine, expire_on_commit=False)
    with factory() as api_session:
        yield api_session
        api_session.rollback()
    api_engine.dispose()


@pytest.fixture
def tenant_factory(owner_session: Session) -> Callable[..., Tenant]:
    def create_tenant(*, slug: str) -> Tenant:
        tenant = Tenant(id=uuid4(), slug=slug)
        owner_session.add(tenant)
        owner_session.commit()
        return tenant

    return create_tenant
