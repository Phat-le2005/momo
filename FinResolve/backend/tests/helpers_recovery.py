"""Shared realistic MiniStore receiver response builder for recovery connector tests."""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.connectors.commerce_write import _compute_command_hash


def mock_receiver_response(
    request: httpx.Request,
    *,
    status: str = "SYNCED",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a complete authoritative receiver HTTP 200 payload matching MiniStore's contract."""
    body = json.loads(request.content.decode("utf-8")) if request.content else {}
    cmd = str(body.get("command") or "").strip().upper()
    merchant_id = request.headers.get("x-merchant-id") or ""
    commerce_session_id = request.headers.get("x-commerce-session-id") or ""
    world_epoch = int(request.headers.get("x-world-epoch") or "1")
    order_id = body.get("order_id")
    payment_id = body.get("payment_id")
    event_id = body.get("event_id")
    expected_version = body.get("expected_version")
    req_hash = _compute_command_hash(
        command=cmd,
        merchant_id=merchant_id,
        commerce_session_id=commerce_session_id,
        world_epoch=world_epoch,
        order_id=order_id,
        payment_id=payment_id,
        event_id=event_id,
        expected_version=expected_version,
    )
    payload: dict[str, Any] = {
        "command_id": "RCV-MOCK000001",
        "command": cmd,
        "idempotency_key": body.get("idempotency_key", ""),
        "request_hash": req_hash,
        "merchant_id": merchant_id,
        "commerce_session_id": commerce_session_id,
        "world_epoch": world_epoch,
        "order_id": order_id,
        "payment_id": payment_id,
        "status": status,
    }
    if extra:
        payload.update(extra)
    return payload
