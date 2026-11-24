"""Liveness/readiness probes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import __version__
from app.core.config import ModelMode, SourceMode, get_settings
from app.core.errors import ErrorCode, ErrorEnvelope, error_response
from app.db import session as db_session

router = APIRouter(tags=["health"])


class DatabaseHealth(BaseModel):
    status: Literal["ok", "unavailable"]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    version: str
    environment: str
    source_mode: SourceMode
    model_mode: ModelMode
    database: DatabaseHealth
    checked_at: datetime


async def _build_health() -> HealthResponse:
    settings = get_settings()
    db_ok = await run_in_threadpool(db_session.check_database)
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        service=settings.app_name,
        version=__version__,
        environment=settings.app_env,
        source_mode=settings.source_mode,
        model_mode=settings.model_mode,
        database=DatabaseHealth(status="ok" if db_ok else "unavailable"),
        checked_at=datetime.now(UTC),
    )


@router.get("/healthz", response_model=HealthResponse, summary="Liveness + dependency status")
async def healthz() -> HealthResponse:
    """Always 200 while the process is alive; `status=degraded` if the DB is unreachable."""
    return await _build_health()


@router.get(
    "/readyz",
    response_model=HealthResponse,
    responses={503: {"model": ErrorEnvelope}},
    summary="Readiness: 503 until the database is reachable",
)
async def readyz(request: Request) -> HealthResponse | JSONResponse:
    health = await _build_health()
    if health.database.status != "ok":
        return error_response(
            request,
            503,
            ErrorCode.SERVICE_UNAVAILABLE.value,
            "Database is not reachable",
            {"database": health.database.status},
        )
    return health
