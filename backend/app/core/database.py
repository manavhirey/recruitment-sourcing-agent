import logging
from collections.abc import Callable, Generator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import models as core_models
from app.core.config import get_settings

Base = core_models.Base
engine = create_engine(get_settings().database_url, pool_pre_ping=True)
session_factory = sessionmaker(bind=engine, expire_on_commit=False)
_REQUEST_SESSION_KEY = "sourcing.database_session"
_AFTER_COMMIT_CALLBACKS_KEY = "sourcing.after_commit_callbacks"
logger = logging.getLogger("sourcing.database")


def defer_until_after_commit(request: Request, callback: Callable[[], None]) -> None:
    callbacks = request.scope.setdefault(_AFTER_COMMIT_CALLBACKS_KEY, [])
    callbacks.append(callback)


class TransactionBoundaryMiddleware:
    """Finish the request transaction before publishing a response status."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        finished = False

        async def send_after_transaction(message: Message) -> None:
            nonlocal finished
            if message["type"] == "http.response.start" and not finished:
                session = scope.get(_REQUEST_SESSION_KEY)
                if isinstance(session, Session):
                    try:
                        action = (
                            session.commit
                            if message["status"] < 400
                            else session.rollback
                        )
                        await run_in_threadpool(action)
                    except Exception:
                        scope.pop(_AFTER_COMMIT_CALLBACKS_KEY, None)
                        await run_in_threadpool(session.rollback)
                        raise
                callbacks = scope.pop(_AFTER_COMMIT_CALLBACKS_KEY, ())
                if message["status"] < 400:
                    for callback in callbacks:
                        try:
                            await run_in_threadpool(callback)
                        except Exception:
                            logger.exception("post_commit_callback_failed")
                finished = True
            await send(message)

        await self.app(scope, receive, send_after_transaction)


def get_db(request: Request) -> Generator[Session, None, None]:
    with session_factory() as session:
        request.scope[_REQUEST_SESSION_KEY] = session
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            if session.in_transaction():
                session.rollback()
