"""Domain services for safe inventory reservation, refund processing, and provider events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.models.enums import (
    MovementReason,
    OrderPaymentStatus,
    PaymentEventType,
    PaymentStatus,
    RefundStatus,
    ReservationStatus,
)
from app.models.simulator import (
    InventoryLevel,
    InventoryMovement,
    InventoryReservation,
    SimOrder,
    SimPayment,
    SimPaymentEvent,
    SimRefund,
)


def reserve_inventory(
    db: Session,
    *,
    session_id: str,
    product_id: str,
    warehouse_id: str,
    quantity: int,
    order_id: str | None = None,
    order_item_id: str | None = None,
) -> InventoryReservation:
    """Safely reserve inventory with row-level locking (SELECT FOR UPDATE) to prevent overselling.

    Invariants:
    - Serializes concurrent reservation attempts on the exact product and warehouse.
    - If available (on_hand - reserved) < quantity, strictly rejects with 409 CONFLICT.
    - Check constraint on_hand >= reserved ensures DB-level enforcement.
    """
    if quantity <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "Quantity must be strictly positive", status_code=400)

    stmt = (
        select(InventoryLevel)
        .where(
            InventoryLevel.session_id == session_id,
            InventoryLevel.product_id == product_id,
            InventoryLevel.warehouse_id == warehouse_id,
        )
        .with_for_update()
    )
    inv = db.scalar(stmt)
    if inv is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Inventory level not found for product {product_id} in warehouse {warehouse_id}",
            status_code=404,
        )

    available = inv.on_hand - inv.reserved
    if available < quantity:
        raise AppError(
            ErrorCode.CONFLICT,
            f"Insufficient inventory: requested {quantity}, available {available} "
            f"(on_hand={inv.on_hand}, reserved={inv.reserved})",
            status_code=409,
            details={
                "product_id": product_id,
                "warehouse_id": warehouse_id,
                "available": available,
                "requested": quantity,
            },
        )

    inv.reserved += quantity
    inv.version += 1
    inv.updated_at = datetime.now(UTC)

    reservation_id = f"RES-{uuid.uuid4().hex[:10].upper()}"
    reservation = InventoryReservation(
        id=reservation_id,
        session_id=session_id,
        order_id=order_id,
        order_item_id=order_item_id,
        product_id=product_id,
        warehouse_id=warehouse_id,
        quantity=quantity,
        status=ReservationStatus.CONFIRMED.value,
        source_mode="MOCK",
        created_at=datetime.now(UTC),
    )
    db.add(reservation)

    # Record inventory movement tracking
    movement = InventoryMovement(
        id=f"MOV-{uuid.uuid4().hex[:10].upper()}",
        session_id=session_id,
        product_id=product_id,
        warehouse_id=warehouse_id,
        delta=-quantity,
        reason=MovementReason.RESERVATION_CONFIRMED.value,
        source_ref=reservation_id,
        source_mode="MOCK",
        created_at=datetime.now(UTC),
    )
    db.add(movement)
    db.flush()
    return reservation


def process_refund_request(
    db: Session,
    *,
    session_id: str,
    payment_id: str,
    amount: int,
    reason: str,
    request_id: str | None = None,
    approval_ref: str | None = None,
) -> SimRefund:
    """Safely process a full or partial refund against an authorized/successful payment.

    Invariants:
    - Locks payment row and queries all existing refunds (COMPLETED and PENDING).
    - Prevents refunding if payment is not in SUCCESS status.
    - Prevents a second unsafe refund exceeding remaining eligible amount:
        remaining_eligible = payment.amount - sum(existing_completed + existing_pending)
    - If amount > remaining_eligible, rejects with 409 CONFLICT.
    - Idempotent: same request_id returns existing refund.
    """
    if amount <= 0:
        raise AppError(
            ErrorCode.BAD_REQUEST, "Refund amount must be strictly positive", status_code=400
        )

    # Lock payment
    stmt_pay = (
        select(SimPayment)
        .where(SimPayment.id == payment_id, SimPayment.session_id == session_id)
        .with_for_update()
    )
    payment = db.scalar(stmt_pay)
    if payment is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Payment '{payment_id}' not found", status_code=404)

    if payment.status != PaymentStatus.SUCCESS.value:
        raise AppError(
            ErrorCode.CONFLICT,
            f"Cannot refund payment with status '{payment.status}'; "
            f"payment must be in SUCCESS status",
            status_code=409,
            details={"payment_id": payment_id, "status": payment.status},
        )

    # Idempotency check
    if request_id:
        stmt_existing = select(SimRefund).where(
            SimRefund.session_id == session_id,
            SimRefund.request_id == request_id,
        )
        existing_ref = db.scalar(stmt_existing)
        if existing_ref is not None:
            return existing_ref

    # Query all existing active/pending refunds
    stmt_refunds = (
        select(SimRefund)
        .where(
            SimRefund.payment_id == payment_id,
            SimRefund.session_id == session_id,
            SimRefund.status.in_(
                [
                    RefundStatus.COMPLETED.value,
                    RefundStatus.PENDING.value,
                    RefundStatus.UNKNOWN.value,
                ]
            ),
        )
        .with_for_update()
    )
    existing_refunds = db.scalars(stmt_refunds).all()

    total_refunded_or_pending = sum(
        r.completed_amount if r.status == RefundStatus.COMPLETED.value else r.requested_amount
        for r in existing_refunds
    )
    eligible_remaining = payment.amount - total_refunded_or_pending

    if eligible_remaining <= 0:
        raise AppError(
            ErrorCode.CONFLICT,
            "Payment has already been fully refunded or has pending refunds covering full amount",
            status_code=409,
            details={
                "payment_id": payment_id,
                "payment_amount": payment.amount,
                "total_refunded_or_pending": total_refunded_or_pending,
                "eligible_remaining": eligible_remaining,
            },
        )

    if amount > eligible_remaining:
        raise AppError(
            ErrorCode.CONFLICT,
            f"Refund amount {amount} exceeds remaining eligible refundable "
            f"balance {eligible_remaining}",
            status_code=409,
            details={
                "payment_id": payment_id,
                "requested_amount": amount,
                "eligible_remaining": eligible_remaining,
                "total_refunded_or_pending": total_refunded_or_pending,
            },
        )

    now = datetime.now(UTC)
    refund_id = f"REF-{uuid.uuid4().hex[:10].upper()}"
    new_refund = SimRefund(
        id=refund_id,
        session_id=session_id,
        order_id=payment.order_id,
        payment_id=payment.id,
        amount=amount,
        requested_amount=amount,
        approved_amount=amount,
        completed_amount=amount,
        currency=payment.currency,
        request_id=request_id,
        status=RefundStatus.COMPLETED.value,
        reason=reason,
        approval_ref=approval_ref,
        provider_refund_id=f"MOCK_REF_{uuid.uuid4().hex[:8].upper()}",
        source_mode="MOCK",
        created_at=now,
        updated_at=now,
    )
    db.add(new_refund)

    # If linked to an order, update order payment status
    if payment.order_id:
        stmt_order = (
            select(SimOrder)
            .where(SimOrder.id == payment.order_id, SimOrder.session_id == session_id)
            .with_for_update()
        )
        order = db.scalar(stmt_order)
        if order is not None:
            new_total_refunded = total_refunded_or_pending + amount
            if new_total_refunded >= payment.amount:
                order.payment_status = OrderPaymentStatus.REFUNDED.value
            else:
                order.payment_status = OrderPaymentStatus.PARTIALLY_REFUNDED.value
            order.updated_at = now

    db.flush()
    return new_refund


def process_provider_notification(
    db: Session,
    *,
    session_id: str,
    provider: str,
    provider_ref: str,
    amount: int,
    status: PaymentStatus | str,
    request_id: str | None = None,
    order_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[SimPayment, bool]:
    """Process an incoming provider payment IPN/notification with idempotency deduplication.

    Returns:
        (payment, is_new): is_new is False if duplicate event was deduplicated.
    """
    status_str = status.value if isinstance(status, PaymentStatus) else str(status)

    # Record append-only event
    event = SimPaymentEvent(
        id=f"PEVT-{uuid.uuid4().hex[:10].upper()}",
        session_id=session_id,
        event_type=PaymentEventType.WEBHOOK_RECEIVED.value,
        provider=provider,
        request_id=request_id,
        payload_json=payload
        or {"provider_ref": provider_ref, "amount": amount, "status": status_str},
        source_mode="MOCK",
        created_at=datetime.now(UTC),
    )
    db.add(event)

    # Check duplicate
    stmt = (
        select(SimPayment)
        .where(
            SimPayment.session_id == session_id,
            SimPayment.provider == provider,
            SimPayment.provider_ref == provider_ref,
        )
        .with_for_update()
    )
    existing = db.scalar(stmt)
    if existing is not None:
        # Duplicate notification: do not create double payment or double apply effect
        return existing, False

    now = datetime.now(UTC)
    new_payment = SimPayment(
        id=f"PAY-{uuid.uuid4().hex[:10].upper()}",
        session_id=session_id,
        order_id=order_id,
        merchant_id="MERCHANT_MOCK_01",
        provider=provider,
        provider_ref=provider_ref,
        request_id=request_id,
        amount=amount,
        currency="VND",
        status=status_str,
        verified_at=now if status_str == PaymentStatus.SUCCESS.value else None,
        observed_at=now,
        verification_source="WEBHOOK_IPN",
        source_mode="MOCK",
        created_at=now,
    )
    db.add(new_payment)
    db.flush()
    return new_payment, True
