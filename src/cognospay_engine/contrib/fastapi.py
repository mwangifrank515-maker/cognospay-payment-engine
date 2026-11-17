"""Optional FastAPI adapter (``pip install cognospay-engine[fastapi]``).

    from cognospay_engine.contrib.fastapi import create_callback_router, register_error_handlers
    app.include_router(create_callback_router(engine, get_db))
    register_error_handlers(app)

The router paths are ``{prefix}/stk/{secret}``, ``{prefix}/b2b/{secret}`` and
``{prefix}/b2b-timeout/{secret}``; ``prefix`` must equal ``DarajaConfig.callback_path_prefix``.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..constants import ACCEPTED
from ..errors import EngineError
from ..redaction import install_callback_redaction

logger = logging.getLogger("cognospay_engine.fastapi")


def create_callback_router(engine, get_session, *, prefix: str = "/callback") -> APIRouter:
    """``get_session`` is your FastAPI dependency yielding a SQLAlchemy Session."""
    install_callback_redaction()
    router = APIRouter(prefix=prefix, tags=["M-Pesa Callback"])

    async def _handle(kind: str, secret: str, request: Request, session):
        if not engine.verify_callback_secret(secret):
            logger.warning("callback rejected: bad secret")
            raise HTTPException(status_code=404)
        try:
            body = await request.json()
        except Exception:
            logger.warning("%s callback rejected: body was not JSON", kind)
            return ACCEPTED
        # Verification and payout use blocking HTTP: keep them off the event loop or one slow
        # Daraja answer stalls every request on this worker.
        if kind == "stk":
            await run_in_threadpool(engine.handle_stk_callback, session, body)
        else:
            await run_in_threadpool(engine.handle_b2b_callback, session, body, kind)
        return ACCEPTED

    @router.post("/stk/{secret}")
    async def stk_callback(secret: str, request: Request, session=Depends(get_session)):
        return await _handle("stk", secret, request, session)

    @router.post("/b2b/{secret}")
    async def b2b_callback(secret: str, request: Request, session=Depends(get_session)):
        return await _handle("b2b", secret, request, session)

    @router.post("/b2b-timeout/{secret}")
    async def b2b_timeout_callback(secret: str, request: Request, session=Depends(get_session)):
        return await _handle("b2b_timeout", secret, request, session)

    return router


def register_error_handlers(app) -> None:
    """Map :class:`EngineError` to ``{"detail": message}`` with the error's HTTP status."""
    @app.exception_handler(EngineError)
    async def _engine_error(_request, exc: EngineError):
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})
