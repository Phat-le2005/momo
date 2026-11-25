"""Hardened background business processes (Prompt 10.1):
1. Asynchronous Order Synchronization after `Payment(SUCCEEDED)` (separate from payment capture!).
2. Expired `InventoryReservation` release & cancellation of unpaid expired orders.

Safety Invariants:
- NEVER confirms or fulfills an order whose payment is already `REFUNDED`.
- NEVER resurrects a `CANCELLED` order or deducts already-released inventory on late payment;
  instead marks `reconciliation_required=True` and emits `PAYMENT_RECONCILIATION_REQUIRED`.
- Validates `Inventory.reserved_qty` and `on_hand_qty` strictly without silent clamping.
- Moves `LiveProduct.flash_sale_reserved_qty -> flash_sale_sold_qty` upon order confirmation.
- Marks `RECONCILIATION_REQUIRED` if an order expires while payment status is `UNKNOWN`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    Inventory,
    InventoryReservation,
    LiveProduct,
    Order,
    Payment,
)
from app.models.enums import (
    OrderStatus,
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
            select(Order).where(Order.id == pay_ref.order_id).with_for_update(skip_locked=True)
        )
        if order is None:
            continue
        pay = db.scalar(
            select(Payment).where(Payment.id == pay_id).with_for_update(skip_locked=True)
        )
        if pay is None or pay.is_synced_to_order:
            continue

        # Case 1: Payment was already fully REFUNDED before order sync -> NEVER confirm!
        if pay.status == PaymentStatus.REFUNDED.value:
            reason = (
                "Payment was fully REFUNDED before order synchronization completed. "
                "Order was NOT confirmed or fulfilled; manual reconciliation required."
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

        # Case 3: Normal confirmation from PENDING_PAYMENT with ACTIVE reservations
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
            if not reservations:
                reason = (
                    "No ACTIVE inventory reservations remain for PENDING_PAYMENT order at sync. "
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
