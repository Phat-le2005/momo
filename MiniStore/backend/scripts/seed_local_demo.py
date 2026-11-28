"""Populate the migrated local FinShop database with repeatable MOCK shopping data.

Uses the existing catalog seed, checkout, provider notification, synchronization and
cancellation services. Existing orders are preserved, including changed seed orders.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.entities import CommerceSession, Customer, Order, Payment, Product
from app.payments.adapter import MockPaymentAdapter
from app.services.checkout import cancel_order, create_order_from_items
from app.services.seed import (
    DEFAULT_CUSTOMER_1_ID,
    DEFAULT_CUSTOMER_2_ID,
    DEFAULT_MERCHANT_ID,
    DEFAULT_SESSION_ID,
    SEED_PRODUCTS,
    seed_commerce_data,
)
from app.services.sync import sync_single_confirmed_payment_to_order


def seed_local_demo(db: Session) -> dict[str, Any]:
    settings = get_settings()
    local_environments = {"local", "dev", "development", "docker", "test"}
    if (
        settings.source_mode.strip().upper() != "MOCK"
        or settings.app_env.strip().lower() not in local_environments
    ):
        raise RuntimeError("This seed is restricted to local MOCK environments")

    seed_commerce_data(db)
    world = db.get(CommerceSession, DEFAULT_SESSION_ID)
    if world is None or not world.is_active or world.source_mode != "MOCK":
        raise RuntimeError("An active MOCK commerce session is required")

    created = 0
    order_ids: list[str] = []
    adapter = MockPaymentAdapter()
    for index, product in enumerate(SEED_PRODUCTS[:12]):
        key = f"LOCAL-DEMO-SEED-V1-{world.world_epoch}-{index + 1:02d}"
        existing = db.scalar(
            select(Order).where(
                Order.merchant_id == DEFAULT_MERCHANT_ID,
                Order.commerce_session_id == world.id,
                Order.idempotency_key == key,
            )
        )
        if existing is not None:
            order_ids.append(existing.id)
            continue

        customer_id = DEFAULT_CUSTOMER_1_ID if index % 2 == 0 else DEFAULT_CUSTOMER_2_ID
        customer = db.get(Customer, customer_id)
        if customer is None or not customer.is_test_identity:
            raise RuntimeError("Expected a seeded test customer")
        order, payment, _ = create_order_from_items(
            db,
            customer=customer,
            session_obj=world,
            idempotency_key=key,
            recipient_name=customer.display_name,
            recipient_phone=customer.phone,
            shipping_address=customer.default_address,
            customer_note="[MOCK SEED] Đơn mẫu để kiểm tra FinShop và Guardian.",
            payment_method="LOCAL_MOCK",
            direct_items=[(product["variants"][0][0], 1, None)],
        )

        if index < 8 or index >= 10:
            adapter.process_provider_notification(
                db,
                provider_ref=payment.provider_ref,
                notification_id=f"{key}-PAYMENT",
                merchant_id=order.merchant_id,
                order_id=order.id,
                amount_vnd=payment.amount_vnd,
                outcome="SUCCEEDED" if index < 8 else "FAILED",
                raw_payload={"source": "LOCAL_DEMO_SEED", "synthetic_data_only": True},
            )
            db.refresh(order)
            if index < 8:
                _, _, outcome = sync_single_confirmed_payment_to_order(
                    db,
                    order_id=order.id,
                    payment_id=payment.id,
                    merchant_id=order.merchant_id,
                    commerce_session_id=world.id,
                    world_epoch=world.world_epoch,
                    sync_source="LOCAL_DEMO_SEED",
                )
                if outcome not in {"EXECUTED", "ALREADY_SYNCED"}:
                    raise RuntimeError(f"Seed order synchronization failed: {outcome}")
            else:
                cancel_order(
                    db, order=order, actor="LOCAL_DEMO_SEED", reason="[MOCK] Test payment failed"
                )

        order_ids.append(order.id)
        created += 1

    db.expire_all()
    orders = list(db.scalars(select(Order).where(Order.id.in_(order_ids))).all())
    payments = list(db.scalars(select(Payment).where(Payment.order_id.in_(order_ids))).all())
    return {
        "source_mode": "MOCK",
        "merchant_id": DEFAULT_MERCHANT_ID,
        "commerce_session_id": world.id,
        "products": db.scalar(select(func.count(Product.id))) or 0,
        "seed_orders": len(orders),
        "orders_created": created,
        "orders_preserved": len(orders) - created,
        "seed_payments": len(payments),
        "order_statuses": dict(Counter(order.status for order in orders)),
        "payment_statuses": dict(Counter(payment.status for payment in payments)),
    }


if __name__ == "__main__":
    with get_sessionmaker()() as db:
        print(json.dumps(seed_local_demo(db), ensure_ascii=False, indent=2))
