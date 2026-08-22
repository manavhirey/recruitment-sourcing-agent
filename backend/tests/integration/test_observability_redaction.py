from __future__ import annotations

import json
import logging
from uuid import UUID

import pytest
from celery import Celery, signals
from fastapi.testclient import TestClient
from prometheus_client import generate_latest
from uvicorn.logging import AccessFormatter

from app.core.config import Settings
from app.core.log_redaction import SensitiveDataLogFilter
from app.core.telemetry import (
    PlatformMetrics,
    Telemetry,
    configure_structured_logging,
    identifier_digest,
    install_logging_defaults,
    install_task_telemetry,
    redacting_processor,
    safe_request_fields,
    safe_task_fields,
    sanitize_event,
    start_metrics_server_from_env,
)
from app.main import create_app


def test_sensitive_fields_are_removed_recursively_before_serialization() -> None:
    event = sanitize_event(
        {
            "event": "contact_revealed",
            "tenant_id": "tenant-safe",
            "email": "priya@example.com",
            "nested": {
                "phone_number": "+1-555-0100",
                "result": "ok",
                "provider_payload": {"name": "Priya"},
            },
            "items": [
                {"authorization": "Bearer secret", "outcome": "success"},
                {"snapshot_reference": "tenant/run/provider/request"},
            ],
        }
    )

    serialized = json.dumps(event, sort_keys=True)
    assert "priya@example.com" not in serialized
    assert "+1-555-0100" not in serialized
    assert "Bearer secret" not in serialized
    assert "tenant/run/provider/request" not in serialized
    assert "Priya" not in serialized
    assert event == {
        "event": "contact_revealed",
        "tenant_id": "tenant-safe",
        "nested": {"result": "ok"},
        "items": [{"outcome": "success"}, {}],
    }
    assert sanitize_event(UUID("00000000-0000-4000-8000-000000000001")) == (
        "00000000-0000-4000-8000-000000000001"
    )


def test_camel_case_sensitive_fields_and_arbitrary_query_values_are_redacted() -> None:
    event = sanitize_event(
        {
            "providerPayload": {"name": "Priya"},
            "personalEmail": "priya@example.test",
            "accessToken": "secret",
            "outcome": "safe",
        }
    )
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/health?arbitrary=private-value", "1.1", 200),
        None,
    )

    SensitiveDataLogFilter().filter(record)

    assert event == {"outcome": "safe"}
    assert "private-value" not in record.getMessage()
    assert "/health?arbitrary=[REDACTED]" in record.getMessage()
    formatted = AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s'
    ).format(record)
    assert "private-value" not in formatted
    assert "/health?arbitrary=[REDACTED]" in formatted


def test_telemetry_hashes_user_and_candidate_identifiers_before_logging() -> None:
    captured: list[dict[str, object]] = []
    telemetry = Telemetry(
        hmac_key=b"telemetry-test-key",
        sink=lambda event: captured.append(event),
    )
    user_id = UUID("00000000-0000-4000-8000-000000000101")
    candidate_id = UUID("00000000-0000-4000-8000-000000000202")

    telemetry.emit(
        "contact_revealed",
        user_id=user_id,
        candidate_id=candidate_id,
        email="priya@example.com",
        outcome="success",
    )

    assert captured == [
        {
            "event": "contact_revealed",
            "user_hash": identifier_digest(user_id, b"telemetry-test-key"),
            "candidate_hash": identifier_digest(candidate_id, b"telemetry-test-key"),
            "outcome": "success",
        }
    ]
    assert str(user_id) not in json.dumps(captured)
    assert str(candidate_id) not in json.dumps(captured)


def test_api_metrics_use_route_templates_and_never_query_values() -> None:
    app = create_app(Settings.for_test())
    with TestClient(app) as client:
        response = client.get("/health/ready?email=priya@example.com")
        metrics = client.get("/metrics")

    assert response.status_code == 200
    assert metrics.status_code == 200
    assert 'route="/health/ready"' in metrics.text
    assert "priya@example.com" not in metrics.text


def test_worker_metrics_server_uses_explicit_bounded_port(
    monkeypatch,
) -> None:
    opened: list[tuple[int, object]] = []
    metrics = PlatformMetrics()
    monkeypatch.setenv("METRICS_PORT", "9101")
    monkeypatch.setattr(
        "app.core.telemetry.start_http_server",
        lambda port, registry: opened.append((port, registry)),
    )

    start_metrics_server_from_env(metrics)

    assert opened == [(9101, metrics.registry)]


def test_enrichment_task_telemetry_does_not_mislabel_request_as_run() -> None:
    request_id = "00000000-0000-4000-8000-000000000301"
    tenant_id = "00000000-0000-4000-8000-000000000302"
    user_id = "00000000-0000-4000-8000-000000000303"

    fields = safe_task_fields(
        "sourcing.poll_enrichment_result",
        (request_id, tenant_id, user_id),
    )

    assert fields == {
        "stage": "sourcing.poll_enrichment_result",
        "enrichment_request_id": request_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
    }


def test_request_telemetry_keeps_only_valid_known_route_identifiers() -> None:
    tenant_id = "00000000-0000-4000-8000-000000000401"
    candidate_id = "00000000-0000-4000-8000-000000000402"

    fields = safe_request_fields(
        {
            "tenant_id": tenant_id,
            "candidate_id": candidate_id,
            "email": "priya@example.com",
            "run_id": "not-a-uuid",
        }
    )

    assert fields == {
        "tenant_id": tenant_id,
        "candidate_id": candidate_id,
    }


def test_task_signals_emit_only_safe_fields_for_the_owning_worker() -> None:
    captured: list[dict[str, object]] = []
    app = Celery("task14-observability")
    metrics = PlatformMetrics()
    telemetry = Telemetry(
        hmac_key=b"telemetry-test-key",
        sink=captured.append,
    )
    installed = install_task_telemetry(
        app,
        hmac_key=b"telemetry-test-key",
        metrics=metrics,
        telemetry=telemetry,
        queue_depth_probes={"sourcing": lambda: 7},
        stuck_after_seconds=0,
    )

    class Sender:
        def __init__(self, owner: Celery, retries: int) -> None:
            self.app = owner
            self.name = "sourcing.source_run"
            self.request = type("Request", (), {"retries": retries})()

    sender = Sender(app, 2)
    foreign_sender = Sender(Celery("foreign-worker"), 9)
    args = (
        "00000000-0000-4000-8000-000000000501",
        "00000000-0000-4000-8000-000000000502",
        "00000000-0000-4000-8000-000000000503",
    )

    signals.task_prerun.send(sender=foreign_sender, task_id="foreign", args=args)
    signals.task_prerun.send(sender=sender, task_id="owned", args=args)
    active_metrics = generate_latest(metrics.registry).decode()
    assert 'sourcing_queue_depth{queue="sourcing"} 7.0' in active_metrics
    assert 'sourcing_stuck_runs{stage="sourcing.source_run"} 1.0' in active_metrics
    signals.task_retry.send(sender=sender)
    signals.task_postrun.send(
        sender=sender,
        task_id="owned",
        args=args,
        state="SUCCESS",
    )

    assert install_task_telemetry(app, hmac_key=b"ignored") is installed
    assert [event["event"] for event in captured] == [
        "worker_task_started",
        "worker_task_retry",
        "worker_task_completed",
    ]
    serialized = json.dumps(captured)
    assert args[2] not in serialized
    assert "user_hash" in serialized
    assert "foreign" not in serialized
    assert metrics.retries.labels("sourcing.source_run")._value.get() == 1


@pytest.mark.parametrize("port", ["not-a-port", "1023", "65536"])
def test_worker_metrics_server_rejects_invalid_or_privileged_ports(
    monkeypatch: pytest.MonkeyPatch,
    port: str,
) -> None:
    monkeypatch.setenv("METRICS_PORT", port)

    with pytest.raises(RuntimeError, match="metrics_port_invalid"):
        start_metrics_server_from_env(PlatformMetrics())


def test_structured_logging_bootstrap_keeps_recursive_redactor() -> None:
    configure_structured_logging()
    install_logging_defaults()

    event = redacting_processor(
        object(),
        "info",
        {"event": "probe", "nested": {"api-key": "secret"}},
    )

    assert event == {"event": "probe", "nested": {}}
    assert sanitize_event(object()) == "object"
