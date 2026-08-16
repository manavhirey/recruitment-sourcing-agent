from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.sourcing import dispatch_recovery


def _claim() -> dispatch_recovery.DispatchClaim:
    run_id, tenant_id, user_id = uuid4(), uuid4(), uuid4()
    return dispatch_recovery.DispatchClaim(
        run_id=run_id,
        tenant_id=tenant_id,
        user_id=user_id,
        claim_token=uuid4(),
        dispatch_key=f"sourcing-plan-{run_id}",
    )


def _enrichment_claim() -> dispatch_recovery.EnrichmentDispatchClaim:
    request_id, tenant_id, user_id = uuid4(), uuid4(), uuid4()
    return dispatch_recovery.EnrichmentDispatchClaim(
        request_id=request_id,
        tenant_id=tenant_id,
        user_id=user_id,
        claim_token=uuid4(),
        dispatch_key=f"enrichment-request-{request_id}",
    )


def test_recovery_acknowledges_only_after_broker_publish() -> None:
    claim = _claim()
    events: list[str] = []

    result = dispatch_recovery.recover_claimed_dispatches(
        [claim],
        publish=lambda item: events.append(f"publish:{item.dispatch_key}"),
        complete=lambda item: events.append(f"complete:{item.dispatch_key}"),
        release=lambda item: events.append(f"release:{item.dispatch_key}"),
    )

    assert events == [
        f"publish:{claim.dispatch_key}",
        f"complete:{claim.dispatch_key}",
    ]
    assert result == dispatch_recovery.RecoveryResult(published=1, failed=0)


def test_recovery_releases_claim_without_clearing_pending_when_publish_fails() -> None:
    claim = _claim()
    completed: list[dispatch_recovery.DispatchClaim] = []
    released: list[dispatch_recovery.DispatchClaim] = []

    def fail(_claim: dispatch_recovery.DispatchClaim) -> None:
        raise ConnectionError("broker unavailable")

    result = dispatch_recovery.recover_claimed_dispatches(
        [claim], publish=fail, complete=completed.append, release=released.append
    )

    assert completed == []
    assert released == [claim]
    assert result == dispatch_recovery.RecoveryResult(published=0, failed=1)


def test_crash_after_publish_is_replayed_with_the_same_deterministic_task_id() -> None:
    claim = _claim()
    published: list[str] = []

    def crash_before_ack(_claim: dispatch_recovery.DispatchClaim) -> None:
        raise RuntimeError("database unavailable after publish")

    with pytest.raises(RuntimeError, match="database unavailable after publish"):
        dispatch_recovery.recover_claimed_dispatches(
            [claim],
            publish=lambda item: published.append(item.dispatch_key),
            complete=crash_before_ack,
            release=lambda _item: None,
        )

    dispatch_recovery.recover_claimed_dispatches(
        [claim],
        publish=lambda item: published.append(item.dispatch_key),
        complete=lambda _item: None,
        release=lambda _item: None,
    )

    assert published == [claim.dispatch_key, claim.dispatch_key]


def test_enrichment_recovery_publishes_the_persisted_identity_once_before_ack() -> None:
    claim = _enrichment_claim()
    events: list[str] = []

    result = dispatch_recovery.recover_claimed_dispatches(
        [claim],
        publish=lambda item: events.append(f"publish:{item.dispatch_key}"),
        complete=lambda item: events.append(f"complete:{item.dispatch_key}"),
        release=lambda item: events.append(f"release:{item.dispatch_key}"),
    )

    assert events == [
        f"publish:{claim.dispatch_key}",
        f"complete:{claim.dispatch_key}",
    ]
    assert result == dispatch_recovery.RecoveryResult(published=1, failed=0)


def test_periodic_task_uses_only_maintenance_database_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    settings = SimpleNamespace(
        maintenance_database_url="postgresql://maintenance-capability",
        redis_url="redis://broker",
    )
    monkeypatch.setattr(
        dispatch_recovery, "get_maintenance_settings", lambda: settings
    )
    monkeypatch.setattr(
        dispatch_recovery,
        "recover_pending_dispatches",
        lambda database_url, publish: observed.update(
            database_url=database_url, publish=publish
        ),
    )
    monkeypatch.setattr(
        dispatch_recovery,
        "recover_pending_enrichment_dispatches",
        lambda database_url, publish: observed.update(
            enrichment_database_url=database_url,
            enrichment_publish=publish,
        ),
    )

    dispatch_recovery.recover_sourcing_dispatches.run()

    assert observed["database_url"] == settings.maintenance_database_url
    assert callable(observed["publish"])
    assert observed["enrichment_database_url"] == settings.maintenance_database_url
    assert callable(observed["enrichment_publish"])
