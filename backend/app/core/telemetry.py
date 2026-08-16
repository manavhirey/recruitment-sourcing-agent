from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any
from uuid import UUID

import structlog
from celery import signals  # type: ignore[import-untyped]
from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)

_SENSITIVE_KEY_PARTS = (
    "email",
    "phone",
    "token",
    "authorization",
    "apikey",
    "payload",
    "snapshot",
)
_IDENTIFIER_FIELDS = frozenset({"user_id", "candidate_id"})
_ROUTE_ID_FIELDS = frozenset(
    {"tenant_id", "job_id", "run_id", "candidate_id", "client_id"}
)


def _sensitive_key(value: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).casefold())
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def sanitize_event(value: Any) -> Any:
    """Return a copy with sensitive key/value subtrees removed."""
    if isinstance(value, Mapping):
        return {
            str(key): sanitize_event(item)
            for key, item in value.items()
            if not _sensitive_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_event(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return type(value).__name__


def identifier_digest(value: UUID | str, key: bytes) -> str:
    return hmac.new(key, str(value).encode(), hashlib.sha256).hexdigest()


def redacting_processor(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> dict[str, Any]:
    del logger, method_name
    return sanitize_event(event_dict)


def configure_structured_logging() -> None:
    structlog.configure(
        processors=(
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redacting_processor,
            structlog.processors.JSONRenderer(sort_keys=True),
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


class Telemetry:
    def __init__(
        self,
        *,
        hmac_key: bytes,
        sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._hmac_key = hmac_key
        self._sink = sink
        self._logger = structlog.get_logger("sourcing.telemetry")

    def emit(self, event: str, **fields: object) -> None:
        safe_fields = dict(fields)
        for field in _IDENTIFIER_FIELDS:
            identifier = safe_fields.pop(field, None)
            if identifier is not None:
                safe_fields[field.removesuffix("_id") + "_hash"] = identifier_digest(
                    str(identifier), self._hmac_key
                )
        safe_event: dict[str, object] = sanitize_event({"event": event, **safe_fields})
        if self._sink is not None:
            self._sink(safe_event)
            return
        event_name = str(safe_event.pop("event"))
        self._logger.info(event_name, **safe_event)


class PlatformMetrics:
    """Low-cardinality platform metrics; identifiers are never labels."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self.api_latency = Histogram(
            "sourcing_api_request_duration_seconds",
            "Foreground API request duration.",
            ("method", "route", "status_class"),
            registry=self.registry,
        )
        self.queue_depth = Gauge(
            "sourcing_queue_depth",
            "Messages waiting by bounded queue name.",
            ("queue",),
            registry=self.registry,
        )
        self.stage_duration = Histogram(
            "sourcing_stage_duration_seconds",
            "Sourcing stage duration.",
            ("stage", "outcome"),
            registry=self.registry,
        )
        self.provider_requests = Counter(
            "sourcing_provider_operations_total",
            "Worker provider operations by bounded operation and safe outcome.",
            ("provider", "endpoint", "outcome"),
            registry=self.registry,
        )
        self.retries = Counter(
            "sourcing_worker_retries_total",
            "Worker retries by stage.",
            ("stage",),
            registry=self.registry,
        )
        self.stuck_runs = Gauge(
            "sourcing_stuck_runs",
            "Runs beyond the documented stage threshold.",
            ("stage",),
            registry=self.registry,
        )
        self.budget_exhaustion = Counter(
            "sourcing_budget_exhaustion_total",
            "Tenant-scoped budget exhaustion events.",
            registry=self.registry,
        )
        self.webhook_failures = Counter(
            "sourcing_webhook_failures_total",
            "Webhook authentication or processing failures.",
            ("reason",),
            registry=self.registry,
        )
        self.privacy_failures = Counter(
            "sourcing_privacy_failures_total",
            "Privacy workflow failures by operation.",
            ("operation",),
            registry=self.registry,
        )
        self.snapshot_expiry_failures = Counter(
            "sourcing_snapshot_expiry_failures_total",
            "Snapshot expiry failures.",
            registry=self.registry,
        )
        self.cross_tenant_denials = Counter(
            "sourcing_cross_tenant_denials_total",
            "Requests made without membership in the selected tenant.",
            registry=self.registry,
        )


def safe_request_fields(path_params: Mapping[str, object]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for name in _ROUTE_ID_FIELDS:
        value = path_params.get(name)
        try:
            fields[name] = str(UUID(str(value)))
        except (AttributeError, TypeError, ValueError):
            continue
    return fields


def install_api_telemetry(
    app: FastAPI,
    *,
    hmac_key: bytes,
    metrics: PlatformMetrics | None = None,
    telemetry: Telemetry | None = None,
    expose_endpoint: bool = False,
) -> PlatformMetrics:
    platform_metrics = metrics or PlatformMetrics()
    event_sink = telemetry or Telemetry(hmac_key=hmac_key)
    app.state.metrics = platform_metrics
    app.state.telemetry = event_sink

    @app.middleware("http")
    async def observe_request(
        request: Request, call_next: Callable[..., Any]
    ) -> Response:
        started = time.perf_counter()
        status_code = 500
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            route_template = getattr(route, "path", "unmatched")
            duration = time.perf_counter() - started
            platform_metrics.api_latency.labels(
                request.method,
                route_template,
                f"{status_code // 100}xx",
            ).observe(duration)
            if (
                route_template == "/webhooks/apollo/{capability_token}"
                and status_code >= 400
            ):
                platform_metrics.webhook_failures.labels(
                    f"http_{status_code // 100}xx"
                ).inc()
            context = getattr(request.state, "request_context", None)
            identity_fields: dict[str, object] = {}
            if context is not None:
                identity_fields = {
                    "tenant_id": context.tenant_id,
                    "user_id": context.user_id,
                }
            event_sink.emit(
                "api_request_completed",
                **safe_request_fields(request.path_params),
                **identity_fields,
                route=route_template,
                method=request.method,
                duration_seconds=round(duration, 6),
                outcome="success" if status_code < 400 else "failure",
                status_code=status_code,
            )

    if expose_endpoint:

        @app.get("/metrics", include_in_schema=False)
        def metrics_endpoint() -> Response:
            return Response(
                content=generate_latest(platform_metrics.registry),
                media_type=CONTENT_TYPE_LATEST,
            )

    return platform_metrics


def safe_task_fields(task_name: str, args: tuple[object, ...]) -> dict[str, object]:
    fields: dict[str, object] = {"stage": task_name}
    if task_name.startswith("sourcing."):
        if len(args) > 0:
            identifier_name = (
                "enrichment_request_id"
                if task_name
                in {
                    "sourcing.enrich_request",
                    "sourcing.poll_enrichment_result",
                }
                else "run_id"
            )
            fields[identifier_name] = args[0]
        if len(args) > 1:
            fields["tenant_id"] = args[1]
        if len(args) > 2:
            fields["user_id"] = args[2]
    return fields


def install_task_telemetry(
    celery_app: Any,
    *,
    hmac_key: bytes,
    metrics: PlatformMetrics | None = None,
    telemetry: Telemetry | None = None,
    queue_depth_probes: Mapping[str, Callable[[], int]] | None = None,
    stuck_run_probes: Mapping[str, Callable[[], int]] | None = None,
    stuck_after_seconds: float = 900,
) -> PlatformMetrics:
    if getattr(celery_app, "_safe_telemetry_installed", False):
        return celery_app._platform_metrics
    platform_metrics = metrics or PlatformMetrics()
    event_sink = telemetry or Telemetry(hmac_key=hmac_key)
    started: dict[str, tuple[float, str]] = {}
    for queue, probe in (queue_depth_probes or {}).items():
        if queue not in {"sourcing", "maintenance"}:
            raise ValueError("queue_metric_name_invalid")
        platform_metrics.queue_depth.labels(queue).set_function(probe)
    for stage, probe in (stuck_run_probes or {}).items():
        if stage not in {"queued", "sourcing", "matching", "enriching"}:
            raise ValueError("stuck_metric_stage_invalid")
        platform_metrics.stuck_runs.labels(stage).set_function(probe)

    def belongs_to_app(sender: object) -> bool:
        return getattr(sender, "app", None) is celery_app

    @signals.task_prerun.connect(weak=False)
    def task_started(
        sender: object = None,
        task_id: str | None = None,
        args: tuple[object, ...] | None = None,
        **kwargs: object,
    ) -> None:
        del kwargs
        if not belongs_to_app(sender):
            return
        identifier = task_id or "unknown"
        task_name = str(getattr(sender, "name", "unknown"))
        started[identifier] = (time.perf_counter(), task_name)

        def stuck_count(stage: str = task_name) -> int:
            return sum(
                1
                for began, owned_stage in started.values()
                if owned_stage == stage
                and time.perf_counter() - began >= stuck_after_seconds
            )

        platform_metrics.stuck_runs.labels(task_name).set_function(stuck_count)
        request = getattr(sender, "request", None)
        event_sink.emit(
            "worker_task_started",
            **safe_task_fields(task_name, args or ()),
            retry_count=int(getattr(request, "retries", 0) or 0),
        )

    @signals.task_postrun.connect(weak=False)
    def task_finished(
        sender: object = None,
        task_id: str | None = None,
        args: tuple[object, ...] | None = None,
        state: str | None = None,
        **kwargs: object,
    ) -> None:
        del kwargs
        if not belongs_to_app(sender):
            return
        identifier = task_id or "unknown"
        began, _ = started.pop(identifier, (time.perf_counter(), "unknown"))
        duration = time.perf_counter() - began
        task_name = str(getattr(sender, "name", "unknown"))
        outcome = "success" if state == "SUCCESS" else "failure"
        platform_metrics.stage_duration.labels(task_name, outcome).observe(duration)
        if outcome == "failure":
            if task_name in {
                "maintenance.execute_privacy_deletion",
                "maintenance.resume_privacy_deletions",
            }:
                platform_metrics.privacy_failures.labels(task_name).inc()
            if task_name == "maintenance.reconcile_expired_snapshots":
                platform_metrics.snapshot_expiry_failures.inc()
        event_sink.emit(
            "worker_task_completed",
            **safe_task_fields(task_name, args or ()),
            duration_seconds=round(duration, 6),
            outcome=outcome,
        )

    @signals.task_retry.connect(weak=False)
    def task_retried(sender: object = None, **kwargs: object) -> None:
        del kwargs
        if not belongs_to_app(sender):
            return
        task_name = str(getattr(sender, "name", "unknown"))
        platform_metrics.retries.labels(task_name).inc()
        event_sink.emit("worker_task_retry", stage=task_name)

    celery_app._safe_telemetry_installed = True
    celery_app._platform_metrics = platform_metrics
    celery_app._telemetry = event_sink
    return platform_metrics


def start_metrics_server_from_env(metrics: PlatformMetrics) -> None:
    raw_port = os.environ.get("METRICS_PORT")
    if raw_port is None:
        return
    try:
        port = int(raw_port)
    except ValueError as error:
        raise RuntimeError("metrics_port_invalid") from error
    if port < 1024 or port > 65535:
        raise RuntimeError("metrics_port_invalid")
    start_http_server(port, registry=metrics.registry)


def install_logging_defaults() -> None:
    configure_structured_logging()
    logging.getLogger("sourcing.telemetry").setLevel(logging.INFO)
