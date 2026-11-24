"""Durable background business processes:
1. Asynchronous Order Synchronization after `Payment(SUCCEEDED)` (separate from payment capture!).
2. Expired `InventoryReservation` release & cancellation of unpaid expired orders.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Inventory, InventoryReservation, Order, Payment
from app.models.enums import OrderStatus, PaymentStatus, ReservationStatus
from app.services.checkout import release_order_reservations_and_voucher
from app.services.events import record_commerce_event


def sync_confirmed_payments_to_orders(db: Session, *, limit: int = 25) -> int:
    """Synchronize `Payment(SUCCEEDED, is_synced_to_order=False)` -> `Order(CONFIRMED)` and
    confirm `InventoryReservation` (deducting `on_hand_qty` and `reserved_qty` atomically)."""
    payments = list(
        db.scalars(
            select(Payment)
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
            .with_for_update(skip_locked=True)
        ).all()
    )
    synced = 0
    now = datetime.now(UTC)

    for pay in payments:
        order = db.scalar(select(Order).where(Order.id == pay.order_id).with_for_update())
        if order is None:
            pay.is_synced_to_order = True
            continue

        if order.status == OrderStatus.PENDING_PAYMENT.value:
            # Confirm ACTIVE reservations -> deduct on_hand_qty and reserved_qty atomically
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
            for res in reservations:
                inv = db.scalar(
                    select(Inventory)
                    .where(Inventory.variant_id == res.variant_id)
                    .with_for_update()
                )
                if inv is not None:
                    inv.reserved_qty = max(0, inv.reserved_qty - res.quantity)
                    inv.on_hand_qty = max(0, inv.on_hand_qty - res.quantity)
                    inv.version += 1
                    inv.updated_at = now
                res.status = ReservationStatus.CONFIRMED.value
                res.updated_at = now

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
        if order.payment_status == PaymentStatus.SUCCEEDED.value:
            continue

        release_order_reservations_and_voucher(
            db, order=order, reason_status=ReservationStatus.EXPIRED
        )
        order.status = OrderStatus.CANCELLED.value
        order.version += 1
        order.updated_at = now
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
