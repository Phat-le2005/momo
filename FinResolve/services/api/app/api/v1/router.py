"""Versioned API root mounting demo sessions, cases, and policy-gated actions."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__
from app.api.v1.actions import router as actions_router
from app.api.v1.cases import router as cases_router
from app.api.v1.sessions import router as sessions_router
from app.core.config import ModelMode, SourceMode, get_settings

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(sessions_router)
api_v1_router.include_router(cases_router)
api_v1_router.include_router(actions_router)


class ApiMeta(BaseModel):
    api_version: str
    service_version: str
    source_mode: SourceMode
    model_mode: ModelMode
    synthetic_data_only: bool
    notice: str


@api_v1_router.get("", response_model=ApiMeta, tags=["meta"], summary="API metadata")
async def api_meta() -> ApiMeta:
    settings = get_settings()
    return ApiMeta(
        api_version="v1",
        service_version=__version__,
        source_mode=settings.source_mode,
        model_mode=settings.model_mode,
        synthetic_data_only=True,
        notice=(
            "Dữ liệu mô phỏng (MOCK). Không có giao dịch tiền thật, "
            "không kết nối hệ thống thanh toán production."
        ),
    )
