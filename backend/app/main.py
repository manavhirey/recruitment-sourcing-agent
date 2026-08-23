from collections.abc import Callable, Mapping
from uuid import UUID

from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
)
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from openai import OpenAI
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from app.candidates.contacts import ContactCipher
from app.candidates.router import router as candidates_router
from app.clients.router import router as clients_router
from app.core.config import Settings, get_settings
from app.core.database import TransactionBoundaryMiddleware
from app.core.log_redaction import install_sensitive_data_log_filters
from app.core.security import TokenVerifier
from app.core.telemetry import (
    install_api_telemetry,
    install_logging_defaults,
    start_metrics_server_from_env,
)
from app.crm.router import router as crm_router
from app.identity.router import router as identity_router
from app.jobs.document_router import router as document_router
from app.jobs.document_runner import (
    JobDescriptionExtractionRunner,
    ProcessJobDescriptionExtractionRunner,
)
from app.jobs.llm import OpenAIResponsesScorecardGateway, ScorecardGateway
from app.jobs.router import router as jobs_router
from app.privacy.router import router as privacy_router
from app.sourcing.router import router as sourcing_router
from app.sourcing.webhooks import (
    RedisWebhookRateLimiter,
    WebhookRateLimiter,
)
from app.sourcing.webhooks import (
    router as webhooks_router,
)

if False:  # pragma: no cover - imported only for static typing
    from app.providers.snapshots import SnapshotStore

SourcingDispatcher = Callable[[UUID, UUID, UUID, str], None]
EnrichmentDispatcher = Callable[[UUID, UUID, UUID, str], None]
PrivacyDispatcher = Callable[[UUID, UUID], None]
ReadinessCheck = Callable[[], bool]


def _production_readiness_checks(settings: Settings) -> Mapping[str, ReadinessCheck]:
    if settings.environment == "test":
        return {}

    def database_ready() -> bool:
        from sqlalchemy import create_engine, text

        engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_timeout=2,
            connect_args={"connect_timeout": 2},
        )
        try:
            with engine.connect() as connection:
                return bool(connection.scalar(text("SELECT 1")))
        finally:
            engine.dispose()

    def broker_ready() -> bool:
        client = Redis.from_url(settings.redis_url, socket_timeout=2)
        try:
            return bool(client.ping())
        finally:
            client.close()

    def object_store_ready() -> bool:
        import boto3  # type: ignore[import-untyped]

        client = boto3.client(
            "s3",
            endpoint_url=settings.object_store_endpoint,
            aws_access_key_id=(
                settings.object_store_writer_access_key_id.get_secret_value()
            ),
            aws_secret_access_key=(
                settings.object_store_writer_secret_access_key.get_secret_value()
            ),
            config=Config(
                connect_timeout=2,
                read_timeout=2,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
        client.head_bucket(Bucket=settings.object_store_bucket)
        return True

    return {
        "broker": broker_ready,
        "database": database_ready,
        "object_store": object_store_ready,
    }


def _dispatch_sourcing_run(
    run_id: UUID, tenant_id: UUID, user_id: UUID, dispatch_key: str
) -> None:
    from app.sourcing.tasks import plan_run

    plan_run.apply_async(
        args=(str(run_id), str(tenant_id), str(user_id), "plan"),
        task_id=dispatch_key,
    )


def _dispatch_enrichment_request(
    request_id: UUID, tenant_id: UUID, user_id: UUID, dispatch_key: str
) -> None:
    from app.sourcing.tasks import enrich_request

    enrich_request.apply_async(
        args=(str(request_id), str(tenant_id), str(user_id)),
        task_id=dispatch_key,
    )


def _dispatch_privacy_request(request_id: UUID, tenant_id: UUID) -> None:
    from app.worker import celery_app

    celery_app.send_task(
        "maintenance.execute_privacy_deletion",
        args=(str(request_id), str(tenant_id)),
        queue="maintenance",
    )


def create_app(
    settings: Settings | None = None,
    *,
    job_description_extraction_runner: JobDescriptionExtractionRunner | None = None,
    scorecard_gateway: ScorecardGateway | None = None,
    sourcing_dispatcher: SourcingDispatcher | None = None,
    enrichment_dispatcher: EnrichmentDispatcher | None = None,
    privacy_dispatcher: PrivacyDispatcher | None = None,
    snapshot_store: "SnapshotStore | None" = None,
    contact_cipher: "ContactCipher | None" = None,
    webhook_rate_limiter: WebhookRateLimiter | None = None,
    readiness_checks: Mapping[str, ReadinessCheck] | None = None,
) -> FastAPI:
    app = FastAPI(title="Recruitment Sourcing API", version="1.0.0")
    app.add_middleware(TransactionBoundaryMiddleware)
    app.state.settings = settings or get_settings()
    app.state.token_verifier = TokenVerifier(app.state.settings)
    app.state.job_description_extraction_runner = (
        job_description_extraction_runner
        if job_description_extraction_runner is not None
        else ProcessJobDescriptionExtractionRunner()
    )
    app.state.scorecard_gateway = scorecard_gateway or OpenAIResponsesScorecardGateway(
        OpenAI(api_key=app.state.settings.openai_api_key.get_secret_value()),
        app.state.settings.scorecard_model,
    )
    app.state.sourcing_dispatcher = sourcing_dispatcher or _dispatch_sourcing_run
    app.state.enrichment_dispatcher = (
        enrichment_dispatcher or _dispatch_enrichment_request
    )
    app.state.privacy_dispatcher = privacy_dispatcher or _dispatch_privacy_request
    app.state.snapshot_store = snapshot_store
    app.state.contact_cipher = contact_cipher or ContactCipher(
        app.state.settings.contact_encryption_key.get_secret_value(),
        app.state.settings.suppression_hmac_key.get_secret_value().encode(),
    )
    app.state.webhook_rate_limiter = webhook_rate_limiter or RedisWebhookRateLimiter(
        Redis.from_url(app.state.settings.redis_url)
    )
    install_sensitive_data_log_filters()
    install_logging_defaults()
    app.include_router(identity_router)
    app.include_router(clients_router)
    app.include_router(document_router)
    app.include_router(jobs_router)
    app.include_router(sourcing_router)
    app.include_router(crm_router)
    app.include_router(candidates_router)
    app.include_router(privacy_router)
    app.include_router(webhooks_router)
    metrics = install_api_telemetry(
        app,
        hmac_key=app.state.settings.telemetry_hmac_key.get_secret_value().encode(),
        expose_endpoint=app.state.settings.environment == "test",
    )
    if app.state.settings.environment != "test":
        start_metrics_server_from_env(metrics)

    checks = (
        readiness_checks
        if readiness_checks is not None
        else _production_readiness_checks(app.state.settings)
    )

    @app.get("/health/ready")
    def ready() -> JSONResponse:
        components: dict[str, str] = {}
        for name, check in sorted(checks.items()):
            try:
                components[name] = "ready" if check() else "unavailable"
            except (
                BotoCoreError,
                ClientError,
                OSError,
                RedisError,
                RuntimeError,
                SQLAlchemyError,
            ):
                components[name] = "unavailable"
        if any(value != "ready" for value in components.values()):
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "components": components},
            )
        return JSONResponse(status_code=200, content={"status": "ready"})

    return app


app = create_app()
