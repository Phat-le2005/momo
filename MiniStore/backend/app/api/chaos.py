"""Restricted Chaos Admin API for Blind Challenge Lab (Prompt 13 Part B).

Strictly protected by `X-Chaos-Admin-Key` (separate from read/write service keys),
disabled by default (`COMMERCE_CHAOS_ENABLED=false`), and explicitly blocked on the
Public HTTPS Gateway. Never accessible to FinResolve Agent read tools.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.chaos.engine import inject_blind_challenge_fault, restore_chaos_faults
from app.core.config import get_settings
from app.core.errors import CommerceError, ErrorCode
from app.db.session import get_db

router = APIRouter(prefix="/chaos", tags=["chaos-lab-restricted"])


def _require_chaos_admin(x_chaos_admin_key: str = Header(default="")) -> None:
    settings = get_settings()
    if not settings.chaos_enabled:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "CHAOS_DISABLED: Chaos Admin API is disabled by default",
            status_code=403,
        )
    expected = settings.chaos_admin_key.get_secret_value().strip()
    if not expected or not hmac.compare_digest(x_chaos_admin_key.strip(), expected):
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            "Invalid or missing X-Chaos-Admin-Key",
            status_code=401,
        )


class ChaosInjectRequest(BaseModel):
    family: str = Field(..., min_length=3, max_length=32)
    seed: int = Field(default=42, ge=0, le=2_147_483_647)
    merchant_id: str = Field(default="MERCHANT_FINSHOP_01", max_length=64)
    commerce_session_id: str = Field(default="CSESS_FINSHOP_DEFAULT_01", max_length=64)
    world_epoch: int = Field(default=1, ge=1)
    background_orders: int = Field(default=1, ge=0, le=5)
    burst_count: int = Field(default=12, ge=10, le=30)


@router.post("/inject")
def api_chaos_inject(
    payload: ChaosInjectRequest,
    _: None = Depends(_require_chaos_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    m = inject_blind_challenge_fault(
        db,
        family=payload.family,
        seed=payload.seed,
        merchant_id=payload.merchant_id,
        commerce_session_id=payload.commerce_session_id,
        world_epoch=payload.world_epoch,
        background_orders=payload.background_orders,
        burst_count=payload.burst_count,
    )
    return {
        "challenge_family": m.challenge_family,
        "seed": m.seed,
        "merchant_id": m.merchant_id,
        "commerce_session_id": m.commerce_session_id,
        "world_epoch": m.world_epoch,
        "affected_order_id": m.affected_order_id,
        "affected_payment_id": m.affected_payment_id,
        "affected_variant_id": m.affected_variant_id,
        "affected_event_id": m.affected_event_id,
        "background_order_ids": m.background_order_ids,
        "ground_truth": m.ground_truth,
        "expected_constraints": m.expected_constraints,
        "metrics": m.metrics,
    }


@router.post("/restore")
def api_chaos_restore(
    merchant_id: str = "MERCHANT_FINSHOP_01",
    _: None = Depends(_require_chaos_admin),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    return restore_chaos_faults(db, merchant_id=merchant_id)


class FreeformTrafficRequest(BaseModel):
    request_count: int = Field(default=15, ge=1, le=50)
    concurrency: int = Field(default=5, ge=1, le=10)
    endpoint: str = Field(default="/api/v1/store/products", max_length=128)


class ScopedRestoreRequest(BaseModel):
    order_id: str | None = Field(default=None, max_length=64)
    event_id: str | None = Field(default=None, max_length=128)


@router.post("/controls/traffic-load")
def api_freeform_traffic_load(
    payload: FreeformTrafficRequest,
    _: None = Depends(_require_chaos_admin),
) -> dict[str, Any]:
    """Bounded local concurrent HTTP load experiment recording real request telemetry."""
    import concurrent.futures
    import time

    from fastapi.testclient import TestClient

    from app.core.telemetry import get_telemetry_collector
    from app.main import create_app

    allowed_endpoints = ("/api/v1/store/products", "/api/v1/store/bootstrap", "/healthz")
    target = payload.endpoint.strip()
    if target not in allowed_endpoints:
        raise CommerceError(
            ErrorCode.BAD_REQUEST,
            f"Endpoint '{target}' not allowed for local traffic experiment",
            status_code=400,
        )

    app_inst = create_app()
    t0 = time.perf_counter()
    statuses: dict[int, int] = {}

    def _one_req(_: int) -> int:
        with TestClient(app_inst) as tc:
            r = tc.get(target)
            return int(r.status_code)

    with concurrent.futures.ThreadPoolExecutor(max_workers=payload.concurrency) as pool:
        for code in pool.map(_one_req, range(payload.request_count)):
            statuses[code] = statuses.get(code, 0) + 1

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    snap = get_telemetry_collector().snapshot_window(60)
    return {
        "endpoint": target,
        "requests_executed": payload.request_count,
        "concurrency": payload.concurrency,
        "elapsed_ms": elapsed_ms,
        "status_codes": statuses,
        "measured_window_60s": snap,
    }


@router.post("/controls/restore-scoped")
def api_scoped_restore(
    payload: ScopedRestoreRequest,
    _: None = Depends(_require_chaos_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Restore ONLY explicitly scoped outbox event(s) — never global repair!"""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models.entities import CommerceEvent, EventOutbox
    from app.models.enums import OutboxStatus

    if not payload.order_id and not payload.event_id:
        raise CommerceError(
            ErrorCode.BAD_REQUEST,
            "order_id or event_id required for scoped restore",
            status_code=400,
        )
    stmt = (
        select(EventOutbox)
        .join(CommerceEvent, EventOutbox.commerce_event_id == CommerceEvent.id)
        .where(EventOutbox.status == OutboxStatus.FAILED.value)
    )
    if payload.order_id:
        stmt = stmt.where(CommerceEvent.order_id == payload.order_id)
    if payload.event_id:
        stmt = stmt.where(EventOutbox.event_id == payload.event_id)
    rows = list(db.scalars(stmt.with_for_update()).all())
    now = datetime.now(UTC)
    for row in rows:
        row.status = OutboxStatus.PENDING.value
        row.next_attempt_at = now
    db.commit()
    return {
        "scoped_order_id": payload.order_id,
        "scoped_event_id": payload.event_id,
        "outbox_resumed": len(rows),
    }
