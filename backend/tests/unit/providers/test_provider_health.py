from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.providers.health import (
    ProviderConnectorState,
    disable_provider,
    is_provider_enabled,
)


def test_provider_auth_failure_disables_shared_connector_until_operator_reset() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[ProviderConnectorState.__table__])
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    assert is_provider_enabled(sessions, "apollo") is True

    disable_provider(sessions, "apollo", "authentication_error")

    assert is_provider_enabled(sessions, "apollo") is False
    with Session(engine) as session:
        state = session.get(ProviderConnectorState, "apollo")
        assert state is not None
        assert state.disabled_reason == "authentication_error"
        assert state.enabled is False
    engine.dispose()


def test_provider_disable_reason_is_allowlisted() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[ProviderConnectorState.__table__])
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    disable_provider(sessions, "apollo", "secret provider response")

    with Session(engine) as session:
        state = session.get(ProviderConnectorState, "apollo")
        assert state is not None
        assert state.disabled_reason == "provider_access_error"
    engine.dispose()


def test_concurrent_first_disable_is_an_atomic_upsert(tmp_path) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'connector.db'}")
    Base.metadata.create_all(engine, tables=[ProviderConnectorState.__table__])
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda _: disable_provider(sessions, "apollo", "authentication_error"),
                range(16),
            )
        )

    with Session(engine) as session:
        states = session.query(ProviderConnectorState).all()
        assert len(states) == 1
        assert states[0].enabled is False
        assert states[0].disabled_reason == "authentication_error"
    engine.dispose()
