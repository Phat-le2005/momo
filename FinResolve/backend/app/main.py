"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.health import router as health_router
from app.api.v1.router import api_v1_router
from app.core.body_limit import MaxBodySizeMiddleware
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.request_id import REQUEST_ID_HEADER, RequestIdMiddleware
from app.supervisor.worker import (
    start_embedded_supervisor_worker,
    stop_embedded_supervisor_worker,
)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.enable_embedded_supervisor_worker and settings.app_env.strip().lower() != "test":
        start_embedded_supervisor_worker()
    try:
        yield
    finally:
        stop_embedded_supervisor_worker()


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO)

    app = FastAPI(
        title="FinResolve AI API",
        version=__version__,
        description=(
            "AI-native payment & commerce exception operations MVP. "
            "Synthetic MOCK data only; no real-money operations."
        ),
        openapi_url="/api/v1/openapi.json",
        docs_url="/docs",
        redoc_url=None,
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "X-Demo-Session",
            "X-Demo-Manager-Token",
            "X-Idempotency-Key",
            REQUEST_ID_HEADER,
        ],
        expose_headers=[REQUEST_ID_HEADER],
    )
    app.add_middleware(MaxBodySizeMiddleware, max_body_bytes=settings.max_request_body_bytes)
    app.add_middleware(RequestIdMiddleware)

    register_error_handlers(app)
    app.include_router(health_router)
    app.include_router(api_v1_router)
    return app


app = create_app()
