import hashlib
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.audit.models import AuditEvent


class AuditService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        tenant_id: UUID,
        event_key: str,
        action: str,
        entity_type: str,
        entity_id: UUID,
        actor_user_id: UUID | None = None,
        run_id: UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditEvent:
        if self._session.get_bind().dialect.name == "postgresql":
            lock_digest = hashlib.sha256(
                f"audit-event\0{tenant_id}\0{event_key}".encode()
            ).digest()
            self._session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": int.from_bytes(lock_digest[:8], "big", signed=True)},
            )
        existing = self._session.scalar(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant_id,
                AuditEvent.event_key == event_key,
            )
        )
        if existing is not None:
            return existing
        event = AuditEvent(
            tenant_id=tenant_id,
            run_id=run_id,
            actor_user_id=actor_user_id,
            event_key=event_key,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload or {},
        )
        self._session.add(event)
        self._session.flush()
        return event

    def for_run(self, tenant_id: UUID, run_id: UUID) -> list[AuditEvent]:
        return list(
            self._session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.tenant_id == tenant_id,
                    AuditEvent.run_id == run_id,
                )
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
        )
