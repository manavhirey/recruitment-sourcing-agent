from fastapi import FastAPI
from openai import OpenAI

from app.clients.router import router as clients_router
from app.core.config import Settings, get_settings
from app.core.security import TokenVerifier
from app.identity.router import router as identity_router
from app.jobs.llm import OpenAIResponsesScorecardGateway, ScorecardGateway
from app.jobs.router import router as jobs_router


def create_app(
    settings: Settings | None = None,
    *,
    scorecard_gateway: ScorecardGateway | None = None,
) -> FastAPI:
    app = FastAPI(title="Recruitment Sourcing API", version="1.0.0")
    app.state.settings = settings or get_settings()
    app.state.token_verifier = TokenVerifier(app.state.settings)
    app.state.scorecard_gateway = scorecard_gateway or OpenAIResponsesScorecardGateway(
        OpenAI(api_key=app.state.settings.openai_api_key.get_secret_value()),
        app.state.settings.scorecard_model,
    )
    app.include_router(identity_router)
    app.include_router(clients_router)
    app.include_router(jobs_router)

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        return {"status": "ready"}

    return app


app = create_app()
