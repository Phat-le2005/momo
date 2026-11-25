"""Hardened background business processes (Prompt 11):
1. Asynchronous Order Synchronization after `Payment(SUCCEEDED)` (with exact OrderItem vs
   InventoryReservation quantity validation and safe pre-sync refund reservation release).
2. Expired `InventoryReservation` release & cancellation of unpaid expired orders.
3. Bounded VNPAY Sandbox QueryDr background reconciliation for pending/unknown payments.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.models.entities import (
    Inventory,
    InventoryReservation,
    LiveProduct,
    Order,
    Payment,
    PaymentQueryAttempt,
)
from app.models.enums import (
    OrderStatus,
    PaymentProvider,
    PaymentStatus,
    ReconciliationStatus,
    ReservationStatus,
)
from app.services.checkout import release_order_reservations_and_voucher
from app.services.events import record_commerce_event


def sync_confirmed_payments_to_orders(db: Session, *, limit: int = 25) -> int:
    """Synchronize unsynced `Payment` records to `Order` safely with reconciliation checks."""
    candidate_ids = list(
        db.scalars(
            select(Payment.id)
            .where(
                Payment.status.in_(
                    [
                        PaymentStatus.SUCCEEDED.value,
                        PaymentStatus.PARTIALLY_REFUNDED.value,
                        PaymentStatus.REFUNDED.value,
                    ]
                ),
                Payment.is_synced_to_order.is_(False),
            )
            .order_by(Payment.confirmed_at.asc())
            .limit(limit)
        ).all()
    )
    synced = 0
    now = datetime.now(UTC)

    for pay_id in candidate_ids:
        pay_ref = db.get(Payment, pay_id)
        if pay_ref is None or pay_ref.is_synced_to_order:
            continue

        # Canonical lock order: Order -> Payment
        order = db.scalar(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == pay_ref.order_id)
            .with_for_update(skip_locked=True)
        )
        if order is None:
            continue
        pay = db.scalar(
            select(Payment).where(Payment.id == pay_id).with_for_update(skip_locked=True)
        )
        if pay is None or pay.is_synced_to_order:
            continue

        # Case 1 (Section 5.3): Payment fully REFUNDED before sync -> NEVER confirm or fulfill!
        # Release eligible ACTIVE reservations & voucher capacity exactly once.
        if pay.status == PaymentStatus.REFUNDED.value:
            released = 0
            if order.status == OrderStatus.PENDING_PAYMENT.value:
                released = release_order_reservations_and_voucher(
                    db, order=order, reason_status=ReservationStatus.RELEASED
                )
                order.status = OrderStatus.CANCELLED.value
                order.version += 1

            reason = (
                "Payment was fully REFUNDED before order synchronization completed. "
                f"Order was NOT confirmed/fulfilled; released {released} active reservation(s)."
            )
            pay.is_synced_to_order = True
            pay.reconciliation_required = True
            pay.reconciliation_note = reason
            pay.updated_at = now
            order.payment_status = PaymentStatus.REFUNDED.value
            order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
            order.reconciliation_reason = reason
            order.updated_at = now
            record_commerce_event(
                db,
                merchant_id=order.merchant_id,
                commerce_session_id=order.commerce_session_id,
                world_epoch=order.world_epoch,
                event_type="PAYMENT_RECONCILIATION_REQUIRED",
                order_id=order.id,
                payment_id=pay.id,
                correlation_id=order.correlation_id,
                payload={
                    "payment_status": pay.status,
                    "order_status": order.status,
                    "released_reservations": released,
                    "reason": reason,
                },
            )
            synced += 1
            continue

        # Case 2: Late payment arriving after Order is already CANCELLED -> NEVER resurrect!
        if order.status == OrderStatus.CANCELLED.value:
            reason = (
                "Confirmed payment cannot resurrect a CANCELLED order. "
                "Inventory was already released; manual reconciliation required."
            )
            pay.is_synced_to_order = True
            pay.reconciliation_required = True
            pay.reconciliation_note = reason
            pay.updated_at = now
            order.payment_status = pay.status
            order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
            order.reconciliation_reason = reason
            order.updated_at = now
            record_commerce_event(
                db,
                merchant_id=order.merchant_id,
                commerce_session_id=order.commerce_session_id,
                world_epoch=order.world_epoch,
                event_type="PAYMENT_RECONCILIATION_REQUIRED",
                order_id=order.id,
                payment_id=pay.id,
                correlation_id=order.correlation_id,
                payload={
                    "payment_status": pay.status,
                    "order_status": order.status,
                    "reason": reason,
                },
            )
            synced += 1
            continue

        # Case 3 (Section 5.1): Normal confirmation from PENDING_PAYMENT with FULL reservation match
        if order.status == OrderStatus.PENDING_PAYMENT.value:
            reservations = list(
                db.scalars(
                    select(InventoryReservation)
                    .where(
                        InventoryReservation.order_id == order.id,
                        InventoryReservation.status == ReservationStatus.ACTIVE.value,
                    )
                    .order_by(InventoryReservation.variant_id.asc())
                    .with_for_update()
                ).all()
            )
            # Verify ALL order item quantities are covered by ACTIVE reservations
            expected_qty: dict[str, int] = {}
            for oi in order.items:
                expected_qty[oi.variant_id] = expected_qty.get(oi.variant_id, 0) + oi.quantity
            reserved_qty: dict[str, int] = {}
            for r in reservations:
                reserved_qty[r.variant_id] = reserved_qty.get(r.variant_id, 0) + r.quantity

            if not reservations or expected_qty != reserved_qty:
                reason = (
                    "ACTIVE inventory reservations do not match OrderItems at sync "
                    f"(expected={expected_qty}, reserved={reserved_qty}). "
                    "Order NOT confirmed; manual reconciliation required."
                )
                pay.is_synced_to_order = True
                pay.reconciliation_required = True
                pay.reconciliation_note = reason
                pay.updated_at = now
                order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
                order.reconciliation_reason = reason
                order.updated_at = now
                record_commerce_event(
                    db,
                    merchant_id=order.merchant_id,
                    commerce_session_id=order.commerce_session_id,
                    world_epoch=order.world_epoch,
                    event_type="PAYMENT_RECONCILIATION_REQUIRED",
                    order_id=order.id,
                    payment_id=pay.id,
                    correlation_id=order.correlation_id,
                    payload={"reason": reason},
                )
                synced += 1
                continue

            # Validate ALL inventory reservations strictly before mutating
            inv_pairs: list[tuple[InventoryReservation, Inventory]] = []
            integrity_error: str | None = None
            for res in reservations:
                inv = db.scalar(
                    select(Inventory)
                    .where(Inventory.variant_id == res.variant_id)
                    .with_for_update()
                )
                if inv is None or inv.reserved_qty < res.quantity or inv.on_hand_qty < res.quantity:
                    integrity_error = (
                        f"Inventory integrity failure on confirm for variant {res.variant_id}: "
                        f"on_hand={inv.on_hand_qty if inv else 'MISSING'}, "
                        f"reserved={inv.reserved_qty if inv else 'MISSING'}, qty={res.quantity}"
                    )
                    record_commerce_event(
                        db,
                        merchant_id=order.merchant_id,
                        commerce_session_id=order.commerce_session_id,
                        world_epoch=order.world_epoch,
                        event_type="INVENTORY_INCONSISTENCY_DETECTED",
                        order_id=order.id,
                        payment_id=pay.id,
                        correlation_id=order.correlation_id,
                        payload={"variant_id": res.variant_id, "detail": integrity_error},
                    )
                    break
                inv_pairs.append((res, inv))

            if integrity_error is not None:
                pay.is_synced_to_order = True
                pay.reconciliation_required = True
                pay.reconciliation_note = integrity_error
                pay.updated_at = now
                order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
                order.reconciliation_reason = integrity_error
                order.updated_at = now
                synced += 1
                continue

            for res, inv in inv_pairs:
                inv.reserved_qty -= res.quantity
                inv.on_hand_qty -= res.quantity
                inv.version += 1
                inv.updated_at = now
                res.status = ReservationStatus.CONFIRMED.value
                res.updated_at = now

                if res.live_product_id:
                    lp = db.scalar(
                        select(LiveProduct)
                        .where(LiveProduct.id == res.live_product_id)
                        .with_for_update()
                    )
                    if lp is not None:
                        if lp.flash_sale_reserved_qty >= res.quantity:
                            lp.flash_sale_reserved_qty -= res.quantity
                        lp.flash_sale_sold_qty += res.quantity

            order.status = OrderStatus.CONFIRMED.value
            order.payment_status = pay.status
            order.version += 1
            order.updated_at = now

            record_commerce_event(
                db,
                merchant_id=order.merchant_id,
                commerce_session_id=order.commerce_session_id,
                world_epoch=order.world_epoch,
                event_type="ORDER_UPDATED",
                order_id=order.id,
                payment_id=pay.id,
                correlation_id=order.correlation_id,
                payload={
                    "previous_status": OrderStatus.PENDING_PAYMENT.value,
                    "new_status": OrderStatus.CONFIRMED.value,
                    "payment_status": pay.status,
                    "sync_source": "COMMERCE_ORDER_SYNC_WORKER",
                },
            )

        pay.is_synced_to_order = True
        pay.updated_at = now
        synced += 1

    if synced > 0:
        db.commit()
    return synced


def expire_stale_reservations(db: Session, *, limit: int = 25) -> int:
    """Release expired ACTIVE inventory reservations and cancel unpaid PENDING_PAYMENT orders."""
    now = datetime.now(UTC)
    expired_order_ids = list(
        db.scalars(
            select(InventoryReservation.order_id)
            .where(
                InventoryReservation.status == ReservationStatus.ACTIVE.value,
                InventoryReservation.expires_at <= now,
            )
            .distinct()
            .limit(limit)
        ).all()
    )
    processed = 0
    for oid in expired_order_ids:
        order = db.scalar(select(Order).where(Order.id == oid).with_for_update(skip_locked=True))
        if order is None:
            continue
        if order.status != OrderStatus.PENDING_PAYMENT.value:
            continue
        if order.payment_status in (
            PaymentStatus.SUCCEEDED.value,
            PaymentStatus.PARTIALLY_REFUNDED.value,
            PaymentStatus.REFUNDED.value,
        ):
            continue

        was_unknown_payment = order.payment_status == PaymentStatus.UNKNOWN.value

        release_order_reservations_and_voucher(
            db, order=order, reason_status=ReservationStatus.EXPIRED
        )
        order.status = OrderStatus.CANCELLED.value
        order.version += 1
        order.updated_at = now

        if was_unknown_payment:
            reason = (
                "Order reservation expired while payment status was UNKNOWN. "
                "Reservations released; manual reconciliation required."
            )
            order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
            order.reconciliation_reason = reason
            record_commerce_event(
                db,
                merchant_id=order.merchant_id,
                commerce_session_id=order.commerce_session_id,
                world_epoch=order.world_epoch,
                event_type="PAYMENT_RECONCILIATION_REQUIRED",
                order_id=order.id,
                correlation_id=order.correlation_id,
                payload={"reason": reason, "payment_status": PaymentStatus.UNKNOWN.value},
            )

        record_commerce_event(
            db,
            merchant_id=order.merchant_id,
            commerce_session_id=order.commerce_session_id,
            world_epoch=order.world_epoch,
            event_type="ORDER_UPDATED",
            order_id=order.id,
            correlation_id=order.correlation_id,
            payload={
                "previous_status": OrderStatus.PENDING_PAYMENT.value,
                "new_status": OrderStatus.CANCELLED.value,
                "reason": "RESERVATION_EXPIRED",
            },
        )
        processed += 1

    if processed > 0:
        db.commit()
    return processed


def reconcile_pending_vnpay_payments(
    db: Session,
    *,
    limit: int = 10,
    http_client: object | None = None,
) -> int:
    """Bounded background QueryDr reconciliation for pending/unknown VNPAY_SANDBOX payments.

    Skips silently when VNPAY is not configured; never queries terminal payments; enforces
    `vnpay_querydr_max_attempts` and exponential backoff between attempts.
    """
    settings = get_settings()
    if not settings.is_vnpay_configured:
        return 0

    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.vnpay_querydr_min_age_seconds)
    max_attempts = settings.vnpay_querydr_max_attempts

    candidates = list(
        db.scalars(
            select(Payment)
            .where(
                Payment.provider == PaymentProvider.VNPAY_SANDBOX.value,
                Payment.status.in_(
                    [
                        PaymentStatus.INITIATED.value,
                        PaymentStatus.PENDING.value,
                        PaymentStatus.UNKNOWN.value,
                    ]
                ),
                Payment.created_at <= cutoff,
            )
            .order_by(Payment.created_at.asc())
            .limit(limit)
        ).all()
    )
    if not candidates:
        return 0

    from app.payments.vnpay.adapter import VNPaySandboxAdapter
    from app.payments.vnpay.client import VNPayHttpClient

    client_obj = http_client if isinstance(http_client, VNPayHttpClient) else None
    adapter = VNPaySandboxAdapter(http_client=client_obj)
    queried = 0

    for pay in candidates:
        attempt_count = int(
            db.scalar(
                select(func.count(PaymentQueryAttempt.id)).where(
                    PaymentQueryAttempt.payment_id == pay.id
                )
            )
            or 0
        )
        if attempt_count >= max_attempts:
            continue

        last_attempt = db.scalar(
            select(PaymentQueryAttempt)
            .where(PaymentQueryAttempt.payment_id == pay.id)
            .order_by(PaymentQueryAttempt.created_at.desc())
            .limit(1)
        )
        if last_attempt is not None:
            min_backoff_s = min(300, 15 * (2 ** min(attempt_count, 4)))
            if (now - last_attempt.created_at).total_seconds() < min_backoff_s:
                continue

        adapter.query_payment_status(db, payment_id=pay.id)
        queried += 1

    return queried
