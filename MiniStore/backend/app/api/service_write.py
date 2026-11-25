"""Dedicated Policy-Gated LOCAL_MOCK Recovery Commands API (Prompt 13 Part C).

Security Invariants:
- Protected by dedicated `X-Commerce-Write-Key` (strictly separate from `X-Commerce-Service-Key`
  used for read connectors; read key is ALWAYS rejected here!).
- Enforces full merchant, commerce_session, and world_epoch scope.
- Strictly allowlists 4 narrow recovery commands:
  `REQUEST_ORDER_SYNC`, `REQUEST_EVENT_REDELIVERY`, `REQUEST_PAYMENT_RECONCILIATION`,
  `REQUEST_MANUAL_REVIEW`.
- Strictly restricted to `LOCAL_MOCK` payments; NEVER mutates VNPAY transactions, NEVER issues
  automatic refunds, NEVER resurrects cancelled orders, and enforces `expected_version`!
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import CommerceError, ErrorCode
from app.db.session import get_db
from app.models.entities import EventOutbox, Order, Payment
from app.models.enums import (
    OrderStatus,
    OutboxStatus,
    PaymentProvider,
    PaymentStatus,
    ReconciliationStatus,
)
from app.services.events import record_commerce_event
from app.services.sync import sync_confirmed_payments_to_orders

router = APIRouter(prefix="/service/recovery", tags=["service-recovery-write"])

ALLOWED_RECOVERY_COMMANDS: frozenset[str] = frozenset(
    {
        "REQUEST_ORDER_SYNC",
        "REQUEST_EVENT_REDELIVERY",
        "REQUEST_PAYMENT_RECONCILIATION",
        "REQUEST_MANUAL_REVIEW",
    }
)


class WritePrincipal(BaseModel):
    merchant_id: str
    commerce_session_id: str
    world_epoch: int


def _require_write_principal(
    x_commerce_write_key: str = Header(default=""),
    x_merchant_id: str = Header(default=""),
    x_commerce_session_id: str = Header(default=""),
    x_world_epoch: int = Header(default=0),
) -> WritePrincipal:
    settings = get_settings()
    expected = settings.service_write_api_key.get_secret_value().strip()
    if not expected or not hmac.compare_digest(x_commerce_write_key.strip(), expected):
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            "Invalid or missing X-Commerce-Write-Key (read key is not permitted for writes)",
            status_code=401,
        )
    if not x_merchant_id.strip() or not x_commerce_session_id.strip() or x_world_epoch < 1:
        raise CommerceError(
            ErrorCode.BAD_REQUEST,
            "Missing required scope headers (X-Merchant-Id, X-Commerce-Session-Id, X-World-Epoch)",
            status_code=400,
        )
    return WritePrincipal(
        merchant_id=x_merchant_id.strip(),
        commerce_session_id=x_commerce_session_id.strip(),
        world_epoch=x_world_epoch,
    )


class RecoveryCommandRequest(BaseModel):
    command: str = Field(..., min_length=3, max_length=64)
    order_id: str | None = Field(default=None, max_length=64)
    payment_id: str | None = Field(default=None, max_length=64)
    event_id: str | None = Field(default=None, max_length=128)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(..., min_length=4, max_length=128)
    reason: str = Field(default="FinResolve policy-gated recovery command", max_length=500)


@router.post("/execute")
def execute_recovery_command(
    payload: RecoveryCommandRequest,
    principal: WritePrincipal = Depends(_require_write_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    cmd = payload.command.strip().upper()
    if cmd not in ALLOWED_RECOVERY_COMMANDS:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            f"UNSUPPORTED_RECOVERY_COMMAND: '{cmd}' is not in the permitted allowlist",
            status_code=403,
        )

    now = datetime.now(UTC)

    if cmd == "REQUEST_ORDER_SYNC":
        if not payload.order_id:
            raise CommerceError(
                ErrorCode.BAD_REQUEST,
                "order_id is required for REQUEST_ORDER_SYNC",
                status_code=400,
            )
        order = db.scalar(
            select(Order)
            .where(
                Order.id == payload.order_id,
                Order.merchant_id == principal.merchant_id,
                Order.commerce_session_id == principal.commerce_session_id,
                Order.world_epoch == principal.world_epoch,
            )
            .with_for_update()
        )
        if order is None:
            raise CommerceError(
                ErrorCode.NOT_FOUND, "Order not found in write scope", status_code=404
            )
        if payload.expected_version is not None and order.version != payload.expected_version:
            raise CommerceError(
                ErrorCode.CONFLICT,
                f"VERSION_MISMATCH: expected {payload.expected_version}, got {order.version}",
                status_code=409,
            )
        if order.status == OrderStatus.CANCELLED.value:
            raise CommerceError(
                ErrorCode.CONFLICT,
                "CANNOT_RESURRECT_CANCELLED_ORDER: cancelled orders require refund/manual review",
                status_code=409,
            )

        pay_stmt = select(Payment).where(
            Payment.order_id == order.id,
            Payment.merchant_id == principal.merchant_id,
        )
        if payload.payment_id:
            pay_stmt = pay_stmt.where(Payment.id == payload.payment_id)
        pay = db.scalar(pay_stmt.order_by(Payment.created_at.desc()).limit(1))
        if pay is None:
            raise CommerceError(ErrorCode.NOT_FOUND, "Payment not found for order", status_code=404)
        if pay.provider != PaymentProvider.LOCAL_MOCK.value:
            raise CommerceError(
                ErrorCode.FORBIDDEN,
                f"VNPAY_OR_EXTERNAL_WRITE_FORBIDDEN: provider '{pay.provider}' cannot be mutated",
                status_code=403,
            )
        if pay.status != PaymentStatus.SUCCEEDED.value:
            raise CommerceError(
                ErrorCode.CONFLICT,
                f"PAYMENT_NOT_CONFIRMED: cannot sync order while payment status is '{pay.status}'",
                status_code=409,
            )

        # Idempotent if already confirmed and synced
        if order.status == OrderStatus.CONFIRMED.value and pay.is_synced_to_order:
            return {
                "command": cmd,
                "status": "ALREADY_SYNCED",
                "order_id": order.id,
                "order_status": order.status,
                "order_version": order.version,
                "payment_id": pay.id,
                "idempotency_key": payload.idempotency_key,
            }

        db.commit()  # Release row lock so domain sync worker can acquire canonical lock order
        synced = sync_confirmed_payments_to_orders(db, limit=50)
        db.refresh(order)
        db.refresh(pay)
        if order.status == OrderStatus.CONFIRMED.value:
            record_commerce_event(
                db,
                merchant_id=order.merchant_id,
                commerce_session_id=order.commerce_session_id,
                world_epoch=order.world_epoch,
                event_type="ORDER_UPDATED",
                order_id=order.id,
                payment_id=pay.id,
                payload={
                    "sync_source": "FINRESOLVE_RECOVERY_COMMAND",
                    "idempotency_key": payload.idempotency_key,
                    "new_status": order.status,
                },
            )
            db.commit()
        return {
            "command": cmd,
            "status": "EXECUTED"
            if order.status == OrderStatus.CONFIRMED.value
            else "RECONCILIATION_REQUIRED",
            "synced_count": synced,
            "order_id": order.id,
            "order_status": order.status,
            "order_version": order.version,
            "payment_id": pay.id,
            "is_synced_to_order": pay.is_synced_to_order,
            "idempotency_key": payload.idempotency_key,
        }

    if cmd == "REQUEST_EVENT_REDELIVERY":
        stmt = select(EventOutbox).where(
            EventOutbox.status.in_([OutboxStatus.FAILED.value, OutboxStatus.PENDING.value])
        )
        if payload.event_id:
            stmt = stmt.where(EventOutbox.event_id == payload.event_id)
        rows = list(db.scalars(stmt.limit(100).with_for_update()).all())
        redelivered = 0
        for row in rows:
            row.status = OutboxStatus.PENDING.value
            row.next_attempt_at = now
            redelivered += 1
        db.commit()
        return {
            "command": cmd,
            "status": "EXECUTED",
            "redelivered_count": redelivered,
            "idempotency_key": payload.idempotency_key,
        }

    if cmd == "REQUEST_PAYMENT_RECONCILIATION":
        if not payload.order_id:
            raise CommerceError(ErrorCode.BAD_REQUEST, "order_id required", status_code=400)
        order = db.scalar(
            select(Order)
            .where(
                Order.id == payload.order_id,
                Order.merchant_id == principal.merchant_id,
                Order.commerce_session_id == principal.commerce_session_id,
                Order.world_epoch == principal.world_epoch,
            )
            .with_for_update()
        )
        if order is None:
            raise CommerceError(ErrorCode.NOT_FOUND, "Order not found", status_code=404)
        order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
        order.reconciliation_reason = payload.reason
        order.updated_at = now
        if payload.payment_id:
            pay = db.get(Payment, payload.payment_id)
            if pay is not None and pay.order_id == order.id:
                pay.reconciliation_required = True
                pay.reconciliation_note = payload.reason
        db.commit()
        return {
            "command": cmd,
            "status": "EXECUTED",
            "order_id": order.id,
            "reconciliation_status": order.reconciliation_status,
            "idempotency_key": payload.idempotency_key,
        }

    # REQUEST_MANUAL_REVIEW (pure audit event, zero financial mutation)
    record_commerce_event(
        db,
        merchant_id=principal.merchant_id,
        commerce_session_id=principal.commerce_session_id,
        world_epoch=principal.world_epoch,
        event_type="MANUAL_REVIEW_REQUESTED",
        order_id=payload.order_id,
        payment_id=payload.payment_id,
        payload={
            "reason": payload.reason,
            "idempotency_key": payload.idempotency_key,
            "requested_by": "FINRESOLVE_RECOVERY_COMMAND",
        },
    )
    db.commit()
    return {
        "command": cmd,
        "status": "ESCALATED_MANUAL_REVIEW",
        "order_id": payload.order_id,
        "payment_id": payload.payment_id,
        "idempotency_key": payload.idempotency_key,
    }
