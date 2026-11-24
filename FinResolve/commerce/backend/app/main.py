"""FastAPI application entrypoint for FinShop LIVE independent Commerce API."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.merchant import router as merchant_router
from app.api.service_read import router as service_read_router
from app.api.storefront import router as storefront_router
from app.core.config import get_settings
from app.core.errors import CommerceError
from app.db.session import get_engine, get_sessionmaker
from app.services.seed import seed_commerce_data

logger = logging.getLogger("finshop.commerce.api")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    try:
        maker = get_sessionmaker()
        with maker() as db:
            seed_commerce_data(db)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Startup seed skipped (DB may not be migrated yet): %s", exc)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="FinShop LIVE — Independent Commerce API",
        version="0.1.0",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _limit_request_body(request: Request, call_next: Any) -> Any:
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > settings.max_request_body_bytes:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={
                    "error": {
                        "code": "PAYLOAD_TOO_LARGE",
                        "message": "Request body exceeds maximum allowed size",
                    }
                },
            )
        return await call_next(request)

    @app.exception_handler(CommerceError)
    async def _handle_commerce_error(_request: Request, exc: CommerceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code.value,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": settings.app_name, "source_mode": settings.source_mode}

    @app.get("/readyz", tags=["health"])
    def readyz() -> dict[str, str]:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready", "database": "connected", "source_mode": settings.source_mode}

    app.include_router(storefront_router, prefix="/api/v1")
    app.include_router(merchant_router, prefix="/api/v1")
    app.include_router(service_read_router, prefix="/api/v1")
    return app


app = create_app()
