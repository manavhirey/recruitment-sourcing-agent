from collections.abc import Callable
from uuid import UUID

from fastapi import FastAPI
from openai import OpenAI
from redis import Redis

from app.clients.router import router as clients_router
from app.core.config import Settings, get_settings
from app.core.log_redaction import install_sensitive_data_log_filters
from app.core.security import TokenVerifier
from app.identity.router import router as identity_router
from app.jobs.llm import OpenAIResponsesScorecardGateway, ScorecardGateway
from app.jobs.router import router as jobs_router
from app.sourcing.router import router as sourcing_router
from app.sourcing.webhooks import (
    RedisWebhookRateLimiter,
    WebhookRateLimiter,
)
from app.sourcing.webhooks import (
    router as webhooks_router,
)

if False:  # pragma: no cover - imported only for static typing
    from app.candidates.contacts import ContactCipher
    from app.providers.snapshots import SnapshotStore

SourcingDispatcher = Callable[[UUID, UUID, UUID], None]
EnrichmentDispatcher = Callable[[UUID, UUID, UUID], None]


def _dispatch_sourcing_run(run_id: UUID, tenant_id: UUID, user_id: UUID) -> None:
    from app.sourcing.tasks import plan_run

    plan_run.delay(str(run_id), str(tenant_id), str(user_id), "plan")


def _dispatch_enrichment_request(
    request_id: UUID, tenant_id: UUID, user_id: UUID
) -> None:
    from app.sourcing.tasks import enrich_request

    enrich_request.delay(str(request_id), str(tenant_id), str(user_id))


def create_app(
    settings: Settings | None = None,
    *,
    scorecard_gateway: ScorecardGateway | None = None,
    sourcing_dispatcher: SourcingDispatcher | None = None,
    enrichment_dispatcher: EnrichmentDispatcher | None = None,
    snapshot_store: "SnapshotStore | None" = None,
    contact_cipher: "ContactCipher | None" = None,
    webhook_rate_limiter: WebhookRateLimiter | None = None,
) -> FastAPI:
    app = FastAPI(title="Recruitment Sourcing API", version="1.0.0")
    app.state.settings = settings or get_settings()
    app.state.token_verifier = TokenVerifier(app.state.settings)
    app.state.scorecard_gateway = scorecard_gateway or OpenAIResponsesScorecardGateway(
        OpenAI(api_key=app.state.settings.openai_api_key.get_secret_value()),
        app.state.settings.scorecard_model,
    )
    app.state.sourcing_dispatcher = sourcing_dispatcher or _dispatch_sourcing_run
    app.state.enrichment_dispatcher = (
        enrichment_dispatcher or _dispatch_enrichment_request
    )
    app.state.snapshot_store = snapshot_store
    app.state.contact_cipher = contact_cipher
    app.state.webhook_rate_limiter = webhook_rate_limiter or RedisWebhookRateLimiter(
        Redis.from_url(app.state.settings.redis_url)
    )
    install_sensitive_data_log_filters()
    app.include_router(identity_router)
    app.include_router(clients_router)
    app.include_router(jobs_router)
    app.include_router(sourcing_router)
    app.include_router(webhooks_router)

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        return {"status": "ready"}

    return app


app = create_app()
