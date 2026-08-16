from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.core.security import TokenVerifier
from app.identity.router import router as identity_router


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Recruitment Sourcing API", version="1.0.0")
    app.state.settings = settings or get_settings()
    app.state.token_verifier = TokenVerifier(app.state.settings)
    app.include_router(identity_router)

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        return {"status": "ready"}

    return app


app = create_app()
