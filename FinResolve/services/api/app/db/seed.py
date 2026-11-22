"""Synthetic seed fixtures for demo scenarios.

All records are scoped to a specific DemoSession and marked source_mode=MOCK.
Scenarios:
1. payment_sync: provider payment VERIFIED SUCCESS but order PENDING.
2. refund_risk: an already-refunded order (or completed refund) receives a repeat complaint.
3. unrelated_delayed: pending order with delayed provider payment (not verified).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import (
    FulfillmentStatus,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
)
from app.models.simulator import (
    InventoryLevel,
    OrderItem,
    Product,
    SimOrder,
    SimPayment,
    SimRefund,
    Warehouse,
)


def seed_demo_session(db: Session, session_id: str) -> None:
    """Seed synthetic simulator records for a new demo session."""
    existing = db.scalar(select(SimOrder).where(SimOrder.session_id == session_id).limit(1))
    if existing is not None:
        return

    now = datetime.now(UTC)
    suffix = session_id[:8].upper()

    # 1. Warehouse
    wh = Warehouse(
        id=f"WH-MAIN-{suffix}",
        session_id=session_id,
        code=f"WH-MAIN-{suffix}",
        name="Kho Tổng TP.HCM",
        source_mode="MOCK",
        created_at=now,
        updated_at=now,
    )
    db.add(wh)

    # 2. Products
    p1 = Product(
        id=f"PROD-TSHIRT-{suffix}",
        session_id=session_id,
        name="Áo thun cotton FinResolve",
        sku=f"SKU-TSHIRT-{suffix}",
        price=250000,
        currency="VND",
        active=True,
        source_mode="MOCK",
        created_at=now,
        updated_at=now,
    )
    p2 = Product(
        id=f"PROD-THERMOS-{suffix}",
        session_id=session_id,
        name="Bình giữ nhiệt thông minh",
        sku=f"SKU-THERMOS-{suffix}",
        price=500000,
        currency="VND",
        active=True,
        source_mode="MOCK",
        created_at=now,
        updated_at=now,
    )
    p3 = Product(
        id=f"PROD-NOTEBOOK-{suffix}",
        session_id=session_id,
        name="Sổ tay cao cấp FinResolve",
        sku=f"SKU-NOTEBOOK-{suffix}",
        price=180000,
        currency="VND",
        active=True,
        source_mode="MOCK",
        created_at=now,
        updated_at=now,
    )
    db.add_all([p1, p2, p3])
    db.flush()

    # 3. Inventory Levels
    inv1 = InventoryLevel(
        id=f"INV-1-{suffix}",
        session_id=session_id,
        product_id=p1.id,
        warehouse_id=wh.id,
        on_hand=100,
        reserved=1,
        version=1,
        source_mode="MOCK",
        updated_at=now,
    )
    inv2 = InventoryLevel(
        id=f"INV-2-{suffix}",
        session_id=session_id,
        product_id=p2.id,
        warehouse_id=wh.id,
        on_hand=50,
        reserved=0,
        version=1,
        source_mode="MOCK",
        updated_at=now,
    )
    inv3 = InventoryLevel(
        id=f"INV-3-{suffix}",
        session_id=session_id,
        product_id=p3.id,
        warehouse_id=wh.id,
        on_hand=1,  # Only 1 on hand, perfect for concurrent oversell test
        reserved=0,
        version=1,
        source_mode="MOCK",
        updated_at=now,
    )
    db.add_all([inv1, inv2, inv3])

    # Scenario 1: payment_sync
    # Provider payment is confirmed SUCCESS, but order is still PENDING (e.g. dropped webhook).
    sync_order_id = f"ORD-SYNC-{suffix}"
    sync_order = SimOrder(
        id=sync_order_id,
        session_id=session_id,
        customer_name="Nguyễn Văn An",
        customer_phone="0901234567",
        amount=250000,
        currency="VND",
        subtotal=250000,
        discount=0,
        tax=0,
        shipping=0,
        grand_total=250000,
        status=OrderStatus.PENDING.value,
        payment_status=OrderPaymentStatus.UNPAID.value,
        fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
        version=1,
        source_mode="MOCK",
        created_at=now,
        updated_at=now,
    )
    sync_item = OrderItem(
        id=f"ITEM-SYNC-{suffix}",
        session_id=session_id,
        order_id=sync_order_id,
        product_id=p1.id,
        sku_snapshot=p1.sku,
        product_name_snapshot=p1.name,
        quantity=1,
        unit_price=250000,
        discount=0,
        tax=0,
        line_total=250000,
        currency="VND",
        created_at=now,
    )
    sync_payment = SimPayment(
        id=f"PAY-SYNC-{suffix}",
        session_id=session_id,
        order_id=sync_order_id,
        merchant_id="MERCHANT_MOCK_01",
        provider="MOCK_MOMO",
        provider_ref=f"MOMO_TXN_{suffix}_888",
        provider_order_id=f"MOMO_ORD_{suffix}_888",
        request_id=f"REQ_SYNC_{suffix}",
        result_code=0,
        amount=250000,
        currency="VND",
        status=PaymentStatus.SUCCESS.value,
        verified_at=now,
        observed_at=now,
        verification_source="PROVIDER_QUERY",
        source_mode="MOCK",
        created_at=now,
    )

    # Scenario 2: refund_risk
    # Order was already REFUNDED and a completed refund record exists.
    ref_order_id = f"ORD-REF-{suffix}"
    ref_order = SimOrder(
        id=ref_order_id,
        session_id=session_id,
        customer_name="Trần Thị Bình",
        customer_phone="0912345678",
        amount=500000,
        currency="VND",
        subtotal=500000,
        discount=0,
        tax=0,
        shipping=0,
        grand_total=500000,
        status=OrderStatus.REFUNDED.value,
        payment_status=OrderPaymentStatus.REFUNDED.value,
        fulfillment_status=FulfillmentStatus.CANCELLED.value,
        version=2,
        source_mode="MOCK",
        created_at=now,
        updated_at=now,
    )
    ref_item = OrderItem(
        id=f"ITEM-REF-{suffix}",
        session_id=session_id,
        order_id=ref_order_id,
        product_id=p2.id,
        sku_snapshot=p2.sku,
        product_name_snapshot=p2.name,
        quantity=1,
        unit_price=500000,
        discount=0,
        tax=0,
        line_total=500000,
        currency="VND",
        created_at=now,
    )
    ref_payment = SimPayment(
        id=f"PAY-REF-{suffix}",
        session_id=session_id,
        order_id=ref_order_id,
        merchant_id="MERCHANT_MOCK_01",
        provider="MOCK_MOMO",
        provider_ref=f"MOMO_TXN_{suffix}_777",
        provider_order_id=f"MOMO_ORD_{suffix}_777",
        request_id=f"REQ_REF_{suffix}",
        result_code=0,
        amount=500000,
        currency="VND",
        status=PaymentStatus.SUCCESS.value,
        verified_at=now,
        observed_at=now,
        verification_source="PROVIDER_QUERY",
        source_mode="MOCK",
        created_at=now,
    )
    ref_refund = SimRefund(
        id=f"REF-{suffix}-01",
        session_id=session_id,
        order_id=ref_order_id,
        payment_id=ref_payment.id,
        amount=500000,
        requested_amount=500000,
        approved_amount=500000,
        completed_amount=500000,
        currency="VND",
        request_id=f"REQ_REFUND_{suffix}_01",
        status=RefundStatus.COMPLETED.value,
        reason="Khách hàng yêu cầu hủy đơn và hoàn tiền trước đó",
        approval_ref="APPR_DEMO_01",
        provider_refund_id=f"MOCK_REF_{suffix}_01",
        source_mode="MOCK",
        created_at=now,
        updated_at=now,
    )

    # Scenario 3: unrelated_delayed
    # Normal delayed order; provider payment is still PENDING (not confirmed).
    delay_order_id = f"ORD-DELAY-{suffix}"
    delay_order = SimOrder(
        id=delay_order_id,
        session_id=session_id,
        customer_name="Lê Văn Cường",
        customer_phone="0988776655",
        amount=180000,
        currency="VND",
        subtotal=180000,
        discount=0,
        tax=0,
        shipping=0,
        grand_total=180000,
        status=OrderStatus.PENDING.value,
        payment_status=OrderPaymentStatus.UNPAID.value,
        fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
        version=1,
        source_mode="MOCK",
        created_at=now,
        updated_at=now,
    )
    delay_item = OrderItem(
        id=f"ITEM-DELAY-{suffix}",
        session_id=session_id,
        order_id=delay_order_id,
        product_id=p3.id,
        sku_snapshot=p3.sku,
        product_name_snapshot=p3.name,
        quantity=1,
        unit_price=180000,
        discount=0,
        tax=0,
        line_total=180000,
        currency="VND",
        created_at=now,
    )
    delay_payment = SimPayment(
        id=f"PAY-DELAY-{suffix}",
        session_id=session_id,
        order_id=delay_order_id,
        merchant_id="MERCHANT_MOCK_01",
        provider="MOCK_BANK",
        provider_ref=f"BANK_TXN_{suffix}_333",
        provider_order_id=f"BANK_ORD_{suffix}_333",
        request_id=f"REQ_DELAY_{suffix}",
        result_code=9000,
        amount=180000,
        currency="VND",
        status=PaymentStatus.PENDING.value,
        verified_at=None,
        observed_at=now,
        verification_source="PROVIDER_QUERY",
        source_mode="MOCK",
        created_at=now,
    )

    db.add_all(
        [
            sync_order,
            sync_item,
            sync_payment,
            ref_order,
            ref_item,
            ref_payment,
            ref_refund,
            delay_order,
            delay_item,
            delay_payment,
        ]
    )
    db.flush()
