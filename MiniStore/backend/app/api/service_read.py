"""Authenticated Scoped Service Read APIs for FinResolve Read Connectors (Prompt 10.1).

Protected strictly by `X-Commerce-Service-Key` (validated against `service_credentials` bound
to merchant_id & commerce_session_id) + `X-Merchant-Id` + `X-Commerce-Session-Id` + required
`X-World-Epoch`. Browser clients NEVER receive service credentials.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.auth import ServicePrincipal, get_service_principal
from app.core.errors import CommerceError, ErrorCode
from app.db.session import get_db
from app.models.entities import (
    CommerceEvent,
    EventOutbox,
    Inventory,
    InventoryReservation,
    LiveSession,
    Order,
    Payment,
    PaymentQueryAttempt,
    ProductVariant,
    RefundTransaction,
    VoucherRedemption,
)
from app.models.enums import OrderStatus, OutboxStatus, PaymentStatus, ReconciliationStatus
from app.payments.adapter import build_payment_observation

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
    obs = build_payment_observation(db, payment=payment)
    return {
        "payment_id": obs.payment_id,
        "order_id": obs.order_id,
        "merchant_id": obs.merchant_id,
        "provider": obs.provider,
        "provider_environment": obs.provider_environment,
        "provider_ref": obs.provider_ref,
        "provider_txn_no": obs.provider_txn_no,
        "provider_txn_date": obs.provider_txn_date,
        "provider_response_code": obs.provider_response_code,
        "provider_transaction_status": obs.provider_transaction_status,
        "observation_source": obs.observation_source,
        "signature_verified": obs.signature_verified,
        "status": obs.status,
        "amount_vnd": obs.amount_vnd,
        "refunded_amount_vnd": obs.refunded_amount_vnd,
        "pending_refund_amount_vnd": obs.pending_refund_amount_vnd,
        "unknown_refund_amount_vnd": obs.unknown_refund_amount_vnd,
        "refundable_remaining_vnd": obs.refundable_remaining_vnd,
        "is_synced_to_order": obs.is_synced_to_order,
        "reconciliation_required": obs.reconciliation_required,
        "reconciliation_note": obs.reconciliation_note,
        "confirmed_at": obs.confirmed_at.isoformat() if obs.confirmed_at else None,
        "last_observed_at": obs.last_observed_at.isoformat() if obs.last_observed_at else None,
        "observed_at": obs.observed_at.isoformat(),
        "authoritative_provenance": obs.authoritative_provenance,
    }


@router.get("/payments/{payment_id}/queries")
def service_get_payment_query_history(
    payment_id: str,
    principal: ServicePrincipal = Depends(get_service_principal),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(PaymentQueryAttempt)
            .where(
                PaymentQueryAttempt.payment_id == payment_id,
                PaymentQueryAttempt.merchant_id == principal.merchant_id,
            )
            .order_by(PaymentQueryAttempt.created_at.desc())
            .limit(50)
        ).all()
    )
    return [
        {
            "id": r.id,
            "payment_id": r.payment_id,
            "order_id": r.order_id,
            "provider": r.provider,
            "request_id": r.request_id,
            "command": r.command,
            "http_status": r.http_status,
            "provider_response_code": r.provider_response_code,
            "provider_transaction_status": r.provider_transaction_status,
            "signature_verified": r.signature_verified,
            "mapped_payment_status": r.mapped_payment_status,
            "observation_provenance": r.observation_provenance,
            "error_message": r.error_message,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/orders/{order_id}/reservations")
def service_get_order_reservations(
    order_id: str,
    principal: ServicePrincipal = Depends(get_service_principal),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(InventoryReservation)
            .where(
                InventoryReservation.order_id == order_id,
                InventoryReservation.merchant_id == principal.merchant_id,
                InventoryReservation.commerce_session_id == principal.commerce_session_id,
            )
            .order_by(InventoryReservation.variant_id.asc())
        ).all()
    )
    return [
        {
            "id": r.id,
            "order_id": r.order_id,
            "variant_id": r.variant_id,
            "live_product_id": r.live_product_id,
            "quantity": r.quantity,
            "status": r.status,
            "expires_at": r.expires_at.isoformat(),
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


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
        .order_by(Inventory.variant_id.asc())
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
    after_id: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    principal: ServicePrincipal = Depends(get_service_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Cursor-based paginated commerce event stream ordered stably by `(occurred_at, id)`."""
    stmt = select(CommerceEvent).where(
        CommerceEvent.merchant_id == principal.merchant_id,
        CommerceEvent.commerce_session_id == principal.commerce_session_id,
        CommerceEvent.world_epoch == principal.world_epoch,
    )
    if after_id:
        anchor = db.scalar(
            select(CommerceEvent).where(
                CommerceEvent.id == after_id,
                CommerceEvent.merchant_id == principal.merchant_id,
                CommerceEvent.commerce_session_id == principal.commerce_session_id,
            )
        )
        if anchor is None:
            raise CommerceError(
                ErrorCode.BAD_REQUEST,
                f"Invalid cursor after_id '{after_id}'",
                status_code=400,
            )
        stmt = stmt.where(
            or_(
                CommerceEvent.occurred_at > anchor.occurred_at,
                and_(
                    CommerceEvent.occurred_at == anchor.occurred_at,
                    CommerceEvent.id > anchor.id,
                ),
            )
        )

    rows = list(
        db.scalars(
            stmt.order_by(CommerceEvent.occurred_at.asc(), CommerceEvent.id.asc()).limit(limit + 1)
        ).all()
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = page[-1].id if has_more and page else None

    return {
        "items": [
            {
                "id": e.id,
                "event_id": e.event_id,
                "event_type": e.event_type,
                "source": e.source,
                "order_id": e.order_id,
                "payment_id": e.payment_id,
                "correlation_id": e.correlation_id,
                "occurred_at": e.occurred_at.isoformat(),
                "payload": e.payload_json,
            }
            for e in page
        ],
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


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
            .order_by(CommerceEvent.occurred_at.asc(), CommerceEvent.id.asc())
        ).all()
    )
    return [
        {
            "id": e.id,
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
    reconciliation_required_orders = int(
        db.scalar(
            select(func.count(Order.id)).where(
                Order.merchant_id == mid,
                Order.commerce_session_id == sid,
                Order.reconciliation_status == ReconciliationStatus.RECONCILIATION_REQUIRED.value,
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
    outbox_lag_count = int(
        db.scalar(
            select(func.count(EventOutbox.id)).where(
                EventOutbox.status == OutboxStatus.PENDING.value
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
        "reconciliation_required_orders": reconciliation_required_orders,
        "succeeded_payments": succeeded_payments,
        "unsynced_payments": unsynced_payments,
        "outbox_lag_count": outbox_lag_count,
    }
