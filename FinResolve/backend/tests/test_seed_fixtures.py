"""Tests for synthetic seed fixtures scoped to DemoSession."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.enums import OrderStatus, PaymentStatus, RefundStatus
from app.models.simulator import SimOrder, SimPayment, SimRefund


def test_seed_fixtures_created_for_new_session(client: TestClient) -> None:
    res = client.post(
        "/api/v1/demo/sessions",
        json={"ttl_minutes": 60, "max_cases": 10, "seed_fixtures": True},
    )
    assert res.status_code == 201
    session_id = res.json()["session_id"]

    maker = get_sessionmaker()
    with maker() as db:
        orders = db.scalars(select(SimOrder).where(SimOrder.session_id == session_id)).all()
        payments = db.scalars(select(SimPayment).where(SimPayment.session_id == session_id)).all()
        refunds = db.scalars(select(SimRefund).where(SimRefund.session_id == session_id)).all()

        assert len(orders) == 3
        assert len(payments) == 3
        assert len(refunds) == 1

        # Check Scenario 1: payment_sync (paid provider record, order pending)
        sync_order = next(o for o in orders if "SYNC" in o.id)
        assert sync_order.status == OrderStatus.PENDING.value
        assert sync_order.amount == 250000
        assert sync_order.source_mode == "MOCK"

        sync_pay = next(p for p in payments if p.order_id == sync_order.id)
        assert sync_pay.status == PaymentStatus.SUCCESS.value
        assert sync_pay.provider == "MOCK_MOMO"
        assert sync_pay.source_mode == "MOCK"

        # Check Scenario 2: refund_risk (duplicate refund candidate)
        ref_order = next(o for o in orders if "REF" in o.id)
        assert ref_order.status == OrderStatus.REFUNDED.value
        assert ref_order.amount == 500000
        assert ref_order.source_mode == "MOCK"

        ref_record = refunds[0]
        assert ref_record.order_id == ref_order.id
        assert ref_record.status == RefundStatus.COMPLETED.value
        assert ref_record.amount == 500000
        assert ref_record.source_mode == "MOCK"

        # Check Scenario 3: unrelated_delayed (delayed bank payment, not verified)
        delay_order = next(o for o in orders if "DELAY" in o.id)
        assert delay_order.status == OrderStatus.PENDING.value
        assert delay_order.source_mode == "MOCK"

        delay_pay = next(p for p in payments if p.order_id == delay_order.id)
        assert delay_pay.status == PaymentStatus.PENDING.value
        assert delay_pay.verified_at is None
        assert delay_pay.source_mode == "MOCK"
