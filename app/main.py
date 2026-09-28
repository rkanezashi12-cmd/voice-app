"""FastAPI アプリ。起動は `uvicorn --factory app.main:create_app`。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.clients import ClientRegistry
from app.config import Settings, get_settings
from app.errors import AppError, ConfigError, ExternalServiceError, PermanentError
from app.logs import log_event, set_trace, setup_logging
from app.routers import recordings
from app.runtime import Runtime

logger = logging.getLogger(__name__)

RECORDER_DIR = Path(__file__).resolve().parent.parent / "web" / "recorder"

# 録音ページ用のセキュリティヘッダー（GCS への直接アップロードだけを許可する）
RECORDER_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "media-src 'self' blob: data:; connect-src 'self' https://storage.googleapis.com; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
    "Permissions-Policy": "microphone=(self), screen-wake-lock=(self)",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-cache",
}


def build_runtime(settings: Settings) -> Runtime:
    return Runtime(settings, ClientRegistry.load(settings.clients_config, settings.clients_config_json))


def create_app(runtime: Runtime | None = None) -> FastAPI:
    settings = runtime.settings if runtime else get_settings()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        rt = runtime or build_runtime(settings)
        app.state.runtime = rt
        log_event(logger, "app.started", dry_run=settings.dry_run, clients=len(rt.registry.all()))
        try:
            yield
        finally:
            await rt.aclose()

    app = FastAPI(title="meeting-notes", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    if runtime is not None:
        app.state.runtime = runtime

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        set_trace(request.headers.get("X-Cloud-Trace-Context"), settings.gcp_project_id)
        response = await call_next(request)
        if request.url.path.startswith("/recorder"):
            response.headers.update(RECORDER_HEADERS)
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        status_code = 500
        if isinstance(exc, ExternalServiceError):
            status_code = 502
        elif isinstance(exc, PermanentError):
            status_code = 422
        elif isinstance(exc, ConfigError):
            status_code = 500
        log_event(
            logger,
            "request.failed",
            logging.ERROR,
            path=request.url.path,
            error_code=exc.code,
            error=exc.message,
        )
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=status_code)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        return {"status": "ok", "dry_run": settings.dry_run}

    app.include_router(recordings.router)
    app.mount("/recorder", StaticFiles(directory=RECORDER_DIR, html=True), name="recorder")
    return app
