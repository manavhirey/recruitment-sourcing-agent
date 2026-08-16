from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from app.core.models import Base

_REASONS = frozenset({"authentication_error", "permission_error"})


class ProviderConnectorState(Base):
    __tablename__ = "provider_connector_states"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    disabled_reason: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


def is_provider_enabled(session_factory: sessionmaker[Session], provider: str) -> bool:
    with session_factory() as session:
        state = session.get(ProviderConnectorState, provider)
        return state is None or state.enabled


def disable_provider(
    session_factory: sessionmaker[Session], provider: str, reason: str
) -> None:
    safe_reason = reason if reason in _REASONS else "provider_access_error"
    with session_factory() as session:
        values = {
            "provider": provider,
            "enabled": False,
            "disabled_reason": safe_reason,
            "updated_at": datetime.now(UTC),
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            statement = postgresql_insert(ProviderConnectorState).values(**values)
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=[ProviderConnectorState.provider],
                    set_={
                        "enabled": False,
                        "disabled_reason": safe_reason,
                        "updated_at": values["updated_at"],
                    },
                )
            )
        elif dialect == "sqlite":
            sqlite_statement = sqlite_insert(ProviderConnectorState).values(**values)
            session.execute(
                sqlite_statement.on_conflict_do_update(
                    index_elements=[ProviderConnectorState.provider],
                    set_={
                        "enabled": False,
                        "disabled_reason": safe_reason,
                        "updated_at": values["updated_at"],
                    },
                )
            )
        else:  # fail closed rather than silently restoring provider access
            raise RuntimeError("provider_state_dialect_unsupported")
        session.commit()
