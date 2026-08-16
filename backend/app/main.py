from collections.abc import Callable
from uuid import UUID

from fastapi import FastAPI
from openai import OpenAI

from app.clients.router import router as clients_router
from app.core.config import Settings, get_settings
from app.core.security import TokenVerifier
from app.identity.router import router as identity_router
from app.jobs.llm import OpenAIResponsesScorecardGateway, ScorecardGateway
from app.jobs.router import router as jobs_router
from app.sourcing.router import router as sourcing_router

SourcingDispatcher = Callable[[UUID, UUID, UUID], None]


def _dispatch_sourcing_run(run_id: UUID, tenant_id: UUID, user_id: UUID) -> None:
    from app.sourcing.tasks import plan_run

    plan_run.delay(str(run_id), str(tenant_id), str(user_id), "plan")


def create_app(
    settings: Settings | None = None,
    *,
    scorecard_gateway: ScorecardGateway | None = None,
    sourcing_dispatcher: SourcingDispatcher | None = None,
) -> FastAPI:
    app = FastAPI(title="Recruitment Sourcing API", version="1.0.0")
    app.state.settings = settings or get_settings()
    app.state.token_verifier = TokenVerifier(app.state.settings)
    app.state.scorecard_gateway = scorecard_gateway or OpenAIResponsesScorecardGateway(
        OpenAI(api_key=app.state.settings.openai_api_key.get_secret_value()),
        app.state.settings.scorecard_model,
    )
    app.state.sourcing_dispatcher = sourcing_dispatcher or _dispatch_sourcing_run
    app.include_router(identity_router)
    app.include_router(clients_router)
    app.include_router(jobs_router)
    app.include_router(sourcing_router)

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        return {"status": "ready"}

    return app


app = create_app()
