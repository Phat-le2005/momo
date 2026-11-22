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

from app.models.enums import OrderStatus, PaymentStatus, RefundStatus
from app.models.simulator import SimOrder, SimPayment, SimRefund


def seed_demo_session(db: Session, session_id: str) -> None:
    """Seed synthetic simulator records for a new demo session."""
    existing = db.scalar(select(SimOrder).where(SimOrder.session_id == session_id).limit(1))
    if existing is not None:
        return

    suffix = session_id[:8].upper()

    # Scenario 1: payment_sync
    # Provider payment is confirmed SUCCESS, but order is still PENDING (e.g. dropped webhook).
    sync_order_id = f"ORD-SYNC-{suffix}"
    sync_order = SimOrder(
        id=sync_order_id,
        session_id=session_id,
        customer_name="Nguyễn Văn An",
        customer_phone="0901234567",
        amount=250000,
        status=OrderStatus.PENDING.value,
        version=1,
        source_mode="MOCK",
    )
    sync_payment = SimPayment(
        id=f"PAY-SYNC-{suffix}",
        session_id=session_id,
        order_id=sync_order_id,
        provider="MOCK_MOMO",
        provider_ref=f"MOMO_TXN_{suffix}_888",
        amount=250000,
        status=PaymentStatus.SUCCESS.value,
        verified_at=datetime.now(UTC),
        source_mode="MOCK",
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
        status=OrderStatus.REFUNDED.value,
        version=2,
        source_mode="MOCK",
    )
    ref_payment = SimPayment(
        id=f"PAY-REF-{suffix}",
        session_id=session_id,
        order_id=ref_order_id,
        provider="MOCK_MOMO",
        provider_ref=f"MOMO_TXN_{suffix}_777",
        amount=500000,
        status=PaymentStatus.SUCCESS.value,
        verified_at=datetime.now(UTC),
        source_mode="MOCK",
    )
    ref_refund = SimRefund(
        id=f"REF-{suffix}-01",
        session_id=session_id,
        order_id=ref_order_id,
        payment_id=ref_payment.id,
        amount=500000,
        status=RefundStatus.COMPLETED.value,
        reason="Khách hàng yêu cầu hủy đơn và hoàn tiền trước đó",
        source_mode="MOCK",
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
        status=OrderStatus.PENDING.value,
        version=1,
        source_mode="MOCK",
    )
    delay_payment = SimPayment(
        id=f"PAY-DELAY-{suffix}",
        session_id=session_id,
        order_id=delay_order_id,
        provider="MOCK_BANK",
        provider_ref=f"BANK_TXN_{suffix}_333",
        amount=180000,
        status=PaymentStatus.PENDING.value,
        verified_at=None,
        source_mode="MOCK",
    )

    db.add_all(
        [
            sync_order,
            sync_payment,
            ref_order,
            ref_payment,
            ref_refund,
            delay_order,
            delay_payment,
        ]
    )
    db.flush()
