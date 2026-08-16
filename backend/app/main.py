from fastapi import FastAPI

from app.core.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="Recruitment Sourcing API", version="1.0.0")
    app.state.settings = settings or get_settings()

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        return {"status": "ready"}

    return app


app = create_app()
