"""Authenticated Scoped Service Read APIs for future FinResolve Read Connectors (Section 12).

Protected strictly by `X-Commerce-Service-Key` + `X-Merchant-Id` + `X-Commerce-Session-Id`
(+ optional `X-World-Epoch`). Browser clients NEVER receive service credentials.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.auth import ServicePrincipal, get_service_principal
from app.core.errors import CommerceError, ErrorCode
from app.db.session import get_db
from app.models.entities import (
    CommerceEvent,
    Inventory,
    LiveSession,
    Order,
    Payment,
    ProductVariant,
    RefundTransaction,
    VoucherRedemption,
)
from app.models.enums import OrderStatus, PaymentStatus
from app.payments.adapter import MockPaymentAdapter

router = APIRouter(prefix="/service", tags=["service-read-connectors"])


@router.get("/orders/{order_id}")
def service_get_order(
    order_id: str,
    principal: ServicePrincipal = Depends(get_service_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from app.api.storefront import _to_order_response

    order = db.scalar(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.payments))
        .where(
            Order.id == order_id,
            Order.merchant_id == principal.merchant_id,
            Order.commerce_session_id == principal.commerce_session_id,
        )
    )
    if order is None:
        raise CommerceError(
            ErrorCode.NOT_FOUND, "Order not found in service scope", status_code=404
        )
    return _to_order_response(order).model_dump()


@router.get("/payments/{payment_id}")
def service_get_payment_observation(
    payment_id: str,
    principal: ServicePrincipal = Depends(get_service_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    payment = db.scalar(
        select(Payment).where(
            Payment.id == payment_id,
            Payment.merchant_id == principal.merchant_id,
            Payment.commerce_session_id == principal.commerce_session_id,
        )
    )
    if payment is None:
        raise CommerceError(
            ErrorCode.NOT_FOUND, "Payment not found in service scope", status_code=404
        )
    obs = MockPaymentAdapter().get_authoritative_observation(db, payment_id=payment.id)
    return {
        "payment_id": obs.payment_id,
        "order_id": obs.order_id,
        "merchant_id": obs.merchant_id,
        "provider": obs.provider,
        "provider_ref": obs.provider_ref,
        "status": obs.status,
        "amount_vnd": obs.amount_vnd,
        "refunded_amount_vnd": obs.refunded_amount_vnd,
        "refundable_remaining_vnd": obs.refundable_remaining_vnd,
        "is_synced_to_order": obs.is_synced_to_order,
        "confirmed_at": obs.confirmed_at.isoformat() if obs.confirmed_at else None,
        "observed_at": obs.observed_at.isoformat(),
    }


@router.get("/payments/{payment_id}/refunds")
def service_get_refund_history(
    payment_id: str,
    principal: ServicePrincipal = Depends(get_service_principal),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(RefundTransaction)
            .where(
                RefundTransaction.payment_id == payment_id,
                RefundTransaction.merchant_id == principal.merchant_id,
            )
            .order_by(RefundTransaction.created_at.asc())
        ).all()
    )
    return [
        {
            "id": r.id,
            "refund_request_id": r.refund_request_id,
            "payment_id": r.payment_id,
            "provider": r.provider,
            "provider_refund_ref": r.provider_refund_ref,
            "amount_vnd": r.amount_vnd,
            "status": r.status,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/inventory")
def service_get_inventory(
    variant_id: str | None = Query(default=None),
    principal: ServicePrincipal = Depends(get_service_principal),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = (
        select(Inventory)
        .options(selectinload(Inventory.variant))
        .where(Inventory.merchant_id == principal.merchant_id)
    )
    if variant_id:
        stmt = stmt.where(Inventory.variant_id == variant_id)
    rows = list(db.scalars(stmt).all())
    return [
        {
            "variant_id": i.variant_id,
            "sku": i.variant.sku if isinstance(i.variant, ProductVariant) else "",
            "on_hand_qty": i.on_hand_qty,
            "reserved_qty": i.reserved_qty,
            "available_qty": i.available_qty,
            "low_stock_threshold": i.low_stock_threshold,
            "version": i.version,
        }
        for i in rows
    ]


@router.get("/events")
def service_get_recent_events(
    limit: int = Query(default=50, ge=1, le=200),
    principal: ServicePrincipal = Depends(get_service_principal),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(CommerceEvent)
            .where(
                CommerceEvent.merchant_id == principal.merchant_id,
                CommerceEvent.commerce_session_id == principal.commerce_session_id,
                CommerceEvent.world_epoch == principal.world_epoch,
            )
            .order_by(CommerceEvent.occurred_at.desc())
            .limit(limit)
        ).all()
    )
    return [
        {
            "event_id": e.event_id,
            "event_type": e.event_type,
            "source": e.source,
            "order_id": e.order_id,
            "payment_id": e.payment_id,
            "correlation_id": e.correlation_id,
            "occurred_at": e.occurred_at.isoformat(),
            "payload": e.payload_json,
        }
        for e in rows
    ]


@router.get("/orders/{order_id}/timeline")
def service_get_order_timeline(
    order_id: str,
    principal: ServicePrincipal = Depends(get_service_principal),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(CommerceEvent)
            .where(
                CommerceEvent.order_id == order_id,
                CommerceEvent.merchant_id == principal.merchant_id,
                CommerceEvent.commerce_session_id == principal.commerce_session_id,
            )
            .order_by(CommerceEvent.occurred_at.asc())
        ).all()
    )
    return [
        {
            "event_id": e.event_id,
            "event_type": e.event_type,
            "occurred_at": e.occurred_at.isoformat(),
            "payload": e.payload_json,
        }
        for e in rows
    ]


@router.get("/vouchers/redemptions")
def service_get_voucher_redemptions(
    principal: ServicePrincipal = Depends(get_service_principal),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(VoucherRedemption)
            .where(VoucherRedemption.merchant_id == principal.merchant_id)
            .order_by(VoucherRedemption.created_at.desc())
            .limit(100)
        ).all()
    )
    return [
        {
            "id": r.id,
            "voucher_id": r.voucher_id,
            "customer_id": r.customer_id,
            "order_id": r.order_id,
            "discount_vnd": r.discount_vnd,
            "status": r.status,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/live-sessions")
def service_get_live_sessions(
    principal: ServicePrincipal = Depends(get_service_principal),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(LiveSession)
            .options(selectinload(LiveSession.live_products))
            .where(
                LiveSession.merchant_id == principal.merchant_id,
                LiveSession.commerce_session_id == principal.commerce_session_id,
            )
        ).all()
    )
    return [
        {
            "id": ls.id,
            "title": ls.title,
            "status": ls.status,
            "is_simulated_stream": ls.is_simulated_stream,
            "products_count": len(ls.live_products),
            "starts_at": ls.starts_at.isoformat(),
            "ends_at": ls.ends_at.isoformat(),
        }
        for ls in rows
    ]


@router.get("/metrics")
def service_get_operational_metrics(
    principal: ServicePrincipal = Depends(get_service_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    mid = principal.merchant_id
    sid = principal.commerce_session_id
    orders_total = int(
        db.scalar(
            select(func.count(Order.id)).where(
                Order.merchant_id == mid, Order.commerce_session_id == sid
            )
        )
        or 0
    )
    pending_orders = int(
        db.scalar(
            select(func.count(Order.id)).where(
                Order.merchant_id == mid,
                Order.commerce_session_id == sid,
                Order.status == OrderStatus.PENDING_PAYMENT.value,
            )
        )
        or 0
    )
    succeeded_payments = int(
        db.scalar(
            select(func.count(Payment.id)).where(
                Payment.merchant_id == mid,
                Payment.commerce_session_id == sid,
                Payment.status == PaymentStatus.SUCCEEDED.value,
            )
        )
        or 0
    )
    unsynced_payments = int(
        db.scalar(
            select(func.count(Payment.id)).where(
                Payment.merchant_id == mid,
                Payment.commerce_session_id == sid,
                Payment.status == PaymentStatus.SUCCEEDED.value,
                Payment.is_synced_to_order.is_(False),
            )
        )
        or 0
    )
    return {
        "merchant_id": mid,
        "commerce_session_id": sid,
        "world_epoch": principal.world_epoch,
        "orders_total": orders_total,
        "pending_payment_orders": pending_orders,
        "succeeded_payments": succeeded_payments,
        "unsynced_payments": unsynced_payments,
    }
