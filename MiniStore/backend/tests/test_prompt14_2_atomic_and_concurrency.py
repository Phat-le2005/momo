"""Prompt 14.2 Fix 2: Atomic Recovery Rollback & Concurrent Same-Key Execution (PostgreSQL)."""

from __future__ import annotations

import concurrent.futures
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.entities import (
    CommerceEvent,
    Inventory,
    InventoryReservation,
    Order,
    Payment,
    ReceivedRecoveryCommand,
)
from app.services.seed import DEFAULT_MERCHANT_ID, DEFAULT_SESSION_ID
from app.services.sync import sync_single_confirmed_payment_to_order
from tests.test_prompt14_1_write_scope_and_idempotency import sign_test_authorization


def test_atomic_recovery_rollback_reverts_order_inventory_and_command(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated crash AFTER business mutation but BEFORE commit leaves ZERO partial state."""
    monkeypatch.setenv("COMMERCE_CHAOS_ENABLED", "true")
    get_settings.cache_clear()
    settings = get_settings()
    chaos_h = {"X-Chaos-Admin-Key": settings.chaos_admin_key.get_secret_value()}

    m1 = client.post(
        "/api/v1/chaos/inject", headers=chaos_h, json={"family": "C01", "seed": 701}
    ).json()
    oid = m1["affected_order_id"]
    pid = m1["affected_payment_id"]

    with get_sessionmaker()() as db:
        order_before = db.get(Order, oid)
        assert order_before is not None
        ver_before = order_before.version
        res_before = db.scalar(
            select(InventoryReservation).where(InventoryReservation.order_id == oid)
        )
        assert res_before is not None
        inv_before = db.scalar(
            select(Inventory).where(Inventory.variant_id == res_before.variant_id)
        )
        assert inv_before is not None
        on_hand_before = inv_before.on_hand_qty

    # Execute sync_single_confirmed_payment_to_order with commit=False, then simulate crash rollback
    with get_sessionmaker()() as db:
        order, pay, status_str = sync_single_confirmed_payment_to_order(
            db,
            order_id=oid,
            payment_id=pid,
            merchant_id=DEFAULT_MERCHANT_ID,
            commerce_session_id=DEFAULT_SESSION_ID,
            world_epoch=1,
            expected_version=ver_before,
            commit=False,
        )
        assert status_str == "EXECUTED"
        assert order.status == "CONFIRMED"
        assert pay.is_synced_to_order is True
        # Simulated crash before final commit -> rollback!
        db.rollback()

    # Verify ZERO partial mutation persisted in PostgreSQL
    with get_sessionmaker()() as db:
        order_after = db.get(Order, oid)
        pay_after = db.get(Payment, pid)
        inv_after = db.scalar(
            select(Inventory).where(Inventory.variant_id == res_before.variant_id)
        )
        assert order_after is not None and order_after.status == "PENDING_PAYMENT"
        assert order_after.version == ver_before
        assert pay_after is not None and pay_after.is_synced_to_order is False
        assert inv_after is not None and inv_after.on_hand_qty == on_hand_before


def test_concurrent_same_idempotency_key_executes_inventory_and_order_at_most_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simultaneous requests with same idempotency_key confirm order & decrement stock ONCE."""
    monkeypatch.setenv("COMMERCE_CHAOS_ENABLED", "true")
    get_settings.cache_clear()
    settings = get_settings()
    chaos_h = {"X-Chaos-Admin-Key": settings.chaos_admin_key.get_secret_value()}
    write_key = settings.service_write_api_key.get_secret_value()

    m1 = client.post(
        "/api/v1/chaos/inject", headers=chaos_h, json={"family": "C01", "seed": 702}
    ).json()
    oid = m1["affected_order_id"]
    pid = m1["affected_payment_id"]

    with get_sessionmaker()() as db:
        ord_row = db.get(Order, oid)
        assert ord_row is not None
        ver = ord_row.version
        res_row = db.scalar(
            select(InventoryReservation).where(InventoryReservation.order_id == oid)
        )
        assert res_row is not None
        qty = res_row.quantity
        inv_row = db.scalar(select(Inventory).where(Inventory.variant_id == res_row.variant_id))
        assert inv_row is not None
        on_hand_before = inv_row.on_hand_qty

    idemp_key = f"IDEMP-CONC-{uuid.uuid4().hex[:8]}"
    tok = sign_test_authorization(
        command="REQUEST_ORDER_SYNC",
        idempotency_key=idemp_key,
        order_id=oid,
        payment_id=pid,
        expected_version=ver,
    )
    headers = {
        "X-Commerce-Write-Key": write_key,
        "X-Merchant-Id": DEFAULT_MERCHANT_ID,
        "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
        "X-World-Epoch": "1",
        "X-Command-Authorization": tok,
    }
    body = {
        "command": "REQUEST_ORDER_SYNC",
        "order_id": oid,
        "payment_id": pid,
        "expected_version": ver,
        "idempotency_key": idemp_key,
    }

    def _fire(_: int) -> int:
        r = client.post("/api/v1/service/recovery/execute", headers=headers, json=body)
        return r.status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(_fire, range(4)))

    assert 200 in codes
    assert all(c in (200, 409) for c in codes)

    # Verify exact once-only business effects in PostgreSQL!
    with get_sessionmaker()() as db:
        ord_after = db.get(Order, oid)
        inv_after = db.scalar(select(Inventory).where(Inventory.variant_id == res_row.variant_id))
        ev_count = int(
            db.scalar(
                select(func.count(CommerceEvent.id)).where(
                    CommerceEvent.order_id == oid,
                    CommerceEvent.event_type == "ORDER_UPDATED",
                )
            )
            or 0
        )
        cmd_count = int(
            db.scalar(
                select(func.count(ReceivedRecoveryCommand.id)).where(
                    ReceivedRecoveryCommand.idempotency_key == idemp_key
                )
            )
            or 0
        )
        assert ord_after is not None and ord_after.status == "CONFIRMED"
        assert ord_after.version == ver + 1
        assert inv_after is not None and inv_after.on_hand_qty == on_hand_before - qty
        assert ev_count == 1
        assert cmd_count == 1
