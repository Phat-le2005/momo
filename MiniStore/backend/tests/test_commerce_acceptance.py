"""Comprehensive Acceptance Test Suite for FinShop LIVE Commerce (Prompt 10 + 10.1 auth)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.auth import MOCK_PROVIDER_SECRET_HEADER
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.entities import (
    CommerceEvent,
    EventOutbox,
    Inventory,
    InventoryReservation,
    Order,
)
from app.models.enums import OrderStatus, OutboxStatus, PaymentStatus
from app.services.seed import (
    DEFAULT_LIVE_SESSION_ID,
    DEFAULT_MERCHANT_ID,
    DEFAULT_SESSION_ID,
)
from app.services.sync import expire_stale_reservations
from app.worker import run_worker_tick


def _create_guest_headers(client: TestClient, name: str = "Khách Test") -> dict[str, str]:
    res = client.post(
        "/api/v1/store/customers/guest",
        json={
            "display_name": name,
            "phone": "0901234567",
            "default_address": "123 Nguyễn Huệ, Q1, TP.HCM",
        },
    )
    assert res.status_code == 201
    return {"X-Customer-Token": res.json()["session_token"]}


def _merchant_headers(client: TestClient) -> dict[str, str]:
    settings = get_settings()
    res = client.post(
        "/api/v1/merchant/auth/login",
        json={
            "username": settings.test_merchant_username,
            "password": settings.test_merchant_password.get_secret_value(),
        },
    )
    assert res.status_code == 200
    return {"X-Merchant-Token": res.json()["merchant_token"]}


def _mock_provider_headers() -> dict[str, str]:
    return {
        MOCK_PROVIDER_SECRET_HEADER: get_settings().mock_provider_secret.get_secret_value(),
    }


def _service_headers(
    *,
    merchant_id: str = DEFAULT_MERCHANT_ID,
    session_id: str = DEFAULT_SESSION_ID,
    world_epoch: str = "1",
) -> dict[str, str]:
    return {
        "X-Commerce-Service-Key": get_settings().service_api_key.get_secret_value(),
        "X-Merchant-Id": merchant_id,
        "X-Commerce-Session-Id": session_id,
        "X-World-Epoch": world_epoch,
    }


def test_end_to_end_shopping_cart_voucher_payment_sync_complaint_and_refund(
    client: TestClient,
) -> None:
    """Full lifecycle: browse -> cart -> voucher -> checkout -> MOCK pay -> async sync ->
    fulfill -> complaint (CLAIM) -> partial refund -> reject excessive refund."""
    # 1. Browse persisted products & bootstrap (must NOT expose customer tokens!)
    boot = client.get("/api/v1/store/bootstrap")
    assert boot.status_code == 200
    assert boot.json()["brand"] == "FinShop LIVE"
    assert len(boot.json()["categories"]) >= 5
    assert "test_customers" not in boot.json()

    prods = client.get("/api/v1/store/products").json()
    assert len(prods) >= 12

    cust1 = _create_guest_headers(client, "Nguyễn Minh Anh")

    # 2. Add to cart & test invalid vs valid voucher
    add_res = client.post(
        "/api/v1/store/cart/items",
        headers=cust1,
        json={"variant_id": "VAR-FASH-01-M", "quantity": 1},
    )
    assert add_res.status_code == 200
    assert add_res.json()["subtotal_vnd"] == 249000

    bad_v = client.post(
        "/api/v1/store/cart/voucher",
        headers=cust1,
        json={"voucher_code": "INVALID_CODE_999"},
    )
    assert bad_v.status_code == 400

    good_v = client.post(
        "/api/v1/store/cart/voucher",
        headers=cust1,
        json={"voucher_code": "FINSHOP30K"},
    )
    assert good_v.status_code == 200
    cart_data = good_v.json()
    assert cart_data["discount_vnd"] == 30000
    # 249000 - 30000 + 25000 shipping = 244000
    assert cart_data["total_vnd"] == 244000

    # 3. Manipulated frontend total MUST be rejected!
    tampered = client.post(
        "/api/v1/store/checkout",
        headers={**cust1, "X-Idempotency-Key": "IDEMP-TAMPER-1"},
        json={
            "recipient_name": "Nguyễn Minh Anh",
            "recipient_phone": "0901234567",
            "shipping_address": "123 Nguyễn Huệ, Q1",
            "expected_total_vnd": 1000,
        },
    )
    assert tampered.status_code == 400

    # 4. Valid idempotent checkout (and duplicate click replay even after cart is cleared)
    co_payload = {
        "recipient_name": "Nguyễn Minh Anh",
        "recipient_phone": "0901234567",
        "shipping_address": "123 Nguyễn Huệ, Q1",
        "voucher_code": "FINSHOP30K",
        "expected_total_vnd": 244000,
    }
    co1 = client.post(
        "/api/v1/store/checkout",
        headers={**cust1, "X-Idempotency-Key": "IDEMP-ORDER-001"},
        json=co_payload,
    )
    assert co1.status_code == 201
    order = co1.json()["order"]
    payment = co1.json()["payment"]
    assert order["status"] == OrderStatus.PENDING_PAYMENT.value
    assert order["total_vnd"] == 244000
    assert payment["status"] == PaymentStatus.INITIATED.value

    co2 = client.post(
        "/api/v1/store/checkout",
        headers={**cust1, "X-Idempotency-Key": "IDEMP-ORDER-001"},
        json=co_payload,
    )
    assert co2.status_code == 200
    assert co2.json()["idempotent_replay"] is True
    assert co2.json()["order"]["id"] == order["id"]

    # 5. Payment succeeds BEFORE async order sync (authenticated provider callback!)
    notify_body = {
        "provider_ref": payment["provider_ref"],
        "notification_id": "NOTIF-001",
        "merchant_id": DEFAULT_MERCHANT_ID,
        "order_id": order["id"],
        "amount_vnd": 244000,
        "outcome": "SUCCEEDED",
        "auto_sync_order": False,
    }
    n1 = client.post(
        "/api/v1/test-payments/mock/notify",
        headers=_mock_provider_headers(),
        json=notify_body,
    )
    assert n1.status_code == 200
    assert n1.json()["payment"]["status"] == PaymentStatus.SUCCEEDED.value
    assert n1.json()["payment"]["is_synced_to_order"] is False

    # Duplicate notification is idempotent
    n2 = client.post(
        "/api/v1/test-payments/mock/notify",
        headers=_mock_provider_headers(),
        json=notify_body,
    )
    assert n2.status_code == 200
    assert n2.json()["is_duplicate_notification"] is True

    # Invalid transition SUCCEEDED -> FAILED rejected with 409
    n_bad = client.post(
        "/api/v1/test-payments/mock/notify",
        headers=_mock_provider_headers(),
        json={**notify_body, "notification_id": "NOTIF-002", "outcome": "FAILED"},
    )
    assert n_bad.status_code == 409

    # Background worker syncs confirmed payment -> CONFIRMED order & deducts inventory
    stats = run_worker_tick()
    assert stats["synced_orders"] == 1

    ord_detail = client.get(f"/api/v1/store/orders/{order['id']}", headers=cust1).json()
    assert ord_detail["order"]["status"] == OrderStatus.CONFIRMED.value
    assert ord_detail["order"]["payment"]["is_synced_to_order"] is True

    # 6. Submit complaint -> stored strictly as CLAIM
    cmp_res = client.post(
        f"/api/v1/store/orders/{order['id']}/complaints",
        headers=cust1,
        json={
            "category": "DELIVERY_DELAY",
            "description": "Đơn hàng đã thanh toán, mong shop xác nhận sớm.",
            "claimed_receipt_ref": "RCPT-CLAIM-001",
        },
    )
    assert cmp_res.status_code == 201
    assert cmp_res.json()["evidence_kind"] == "CLAIM"

    # 7. Partial refund + excessive refund rejection
    r_req = client.post(
        f"/api/v1/store/orders/{order['id']}/refund-requests",
        headers=cust1,
        json={"requested_amount_vnd": 100000, "reason": "Đổi trả một phần"},
    )
    assert r_req.status_code == 201
    r_id = r_req.json()["id"]

    m_headers = _merchant_headers(client)
    dec = client.post(
        f"/api/v1/merchant/refunds/{r_id}/decision",
        headers=m_headers,
        json={"decision": "APPROVE", "merchant_note": "Đã kiểm tra đơn"},
    )
    assert dec.status_code == 200
    assert dec.json()["status"] == "COMPLETED"

    # Requesting 200000 when only 144000 remains MUST be rejected with 409
    r_over = client.post(
        f"/api/v1/store/orders/{order['id']}/refund-requests",
        headers=cust1,
        json={"requested_amount_vnd": 200000, "reason": "Yêu cầu vượt số dư"},
    )
    assert r_over.status_code == 409


def test_concurrent_last_item_checkout_prevents_overselling(client: TestClient) -> None:
    """Multiple concurrent customers trying to buy the last 1 available item: strictly 1 wins."""
    cust1 = _create_guest_headers(client, "Race 1")["X-Customer-Token"]
    cust2 = _create_guest_headers(client, "Race 2")["X-Customer-Token"]

    maker = get_sessionmaker()
    with maker() as db:
        inv = db.scalar(select(Inventory).where(Inventory.variant_id == "VAR-FOOD-02-500G"))
        assert inv is not None
        inv.on_hand_qty = 1
        inv.reserved_qty = 0
        db.commit()

    def _attempt(idx: int) -> int:
        tok = cust1 if idx % 2 == 0 else cust2
        res = client.post(
            "/api/v1/store/checkout",
            headers={"X-Customer-Token": tok, "X-Idempotency-Key": f"IDEMP-RACE-{idx}"},
            json={
                "recipient_name": f"Khách Race {idx}",
                "recipient_phone": "0901234567",
                "shipping_address": "TP. HCM",
                "direct_items": [
                    {
                        "variant_id": "VAR-FOOD-02-500G",
                        "quantity": 1,
                        "live_session_id": DEFAULT_LIVE_SESSION_ID,
                    }
                ],
            },
        )
        return res.status_code

    with ThreadPoolExecutor(max_workers=5) as pool:
        statuses = list(pool.map(_attempt, range(5)))

    assert statuses.count(201) == 1
    assert statuses.count(409) == 4

    with maker() as db:
        inv = db.scalar(select(Inventory).where(Inventory.variant_id == "VAR-FOOD-02-500G"))
        assert inv is not None
        assert inv.on_hand_qty == 1
        assert inv.reserved_qty == 1
        assert inv.available_qty == 0


def test_expired_reservation_releases_stock_and_cancels_unpaid_order(client: TestClient) -> None:
    """Expired unpaid reservations release reserved_qty and cancel the unpaid order."""
    cust1 = _create_guest_headers(client, "Khách Hết Hạn")
    co = client.post(
        "/api/v1/store/checkout",
        headers={**cust1, "X-Idempotency-Key": "IDEMP-EXPIRE-1"},
        json={
            "recipient_name": "Khách Hết Hạn",
            "recipient_phone": "0901234567",
            "shipping_address": "Hà Nội",
            "direct_items": [{"variant_id": "VAR-TECH-02-TITAN", "quantity": 2}],
        },
    )
    assert co.status_code == 201
    order_id = co.json()["order"]["id"]

    maker = get_sessionmaker()
    with maker() as db:
        inv_before = db.scalar(select(Inventory).where(Inventory.variant_id == "VAR-TECH-02-TITAN"))
        assert inv_before is not None and inv_before.reserved_qty == 2

        res_row = db.scalar(
            select(InventoryReservation).where(InventoryReservation.order_id == order_id)
        )
        assert res_row is not None
        res_row.expires_at = datetime.now(UTC) - timedelta(seconds=5)
        db.commit()

        expired_cnt = expire_stale_reservations(db)
        assert expired_cnt == 1

        inv_after = db.scalar(select(Inventory).where(Inventory.variant_id == "VAR-TECH-02-TITAN"))
        ord_after = db.get(Order, order_id)
        assert inv_after is not None and inv_after.reserved_qty == 0
        assert ord_after is not None and ord_after.status == OrderStatus.CANCELLED.value


def test_security_isolation_outbox_integrity_and_service_read_connectors(
    client: TestClient,
) -> None:
    """Verify cross-customer isolation, unauthorized merchant rejection, atomic outbox,
    and scoped service connector APIs (including cross-merchant & stale world_epoch rejection)."""
    cust1 = _create_guest_headers(client, "Khách 1")
    cust2 = _create_guest_headers(client, "Khách 2")

    co = client.post(
        "/api/v1/store/checkout",
        headers={**cust1, "X-Idempotency-Key": "IDEMP-SEC-001"},
        json={
            "recipient_name": "Khách 1",
            "recipient_phone": "0901234567",
            "shipping_address": "Đà Nẵng",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 1}],
        },
    )
    assert co.status_code == 201
    order_id = co.json()["order"]["id"]
    payment_id = co.json()["payment"]["id"]

    # 1. Cross-customer order access rejected (404)
    r_other = client.get(f"/api/v1/store/orders/{order_id}", headers=cust2)
    assert r_other.status_code == 404

    # 2. Unauthorized merchant operations rejected (401)
    r_no_m = client.get("/api/v1/merchant/overview")
    assert r_no_m.status_code == 401
    r_forged_m = client.get(
        "/api/v1/merchant/overview",
        headers={"X-Merchant-Token": "mch_forged_token_value"},
    )
    assert r_forged_m.status_code == 401

    # 3. Atomic CommerceEvent + EventOutbox persisted with explicit FinResolve mapping
    maker = get_sessionmaker()
    with maker() as db:
        evs = list(
            db.scalars(select(CommerceEvent).where(CommerceEvent.order_id == order_id)).all()
        )
        outs = list(
            db.scalars(
                select(EventOutbox).where(EventOutbox.commerce_event_id.in_([e.id for e in evs]))
            ).all()
        )
        assert len(evs) == len(outs) >= 3
        out_by_type = {o.event_type: o for o in outs}
        assert out_by_type["ORDER_CREATED"].status == OutboxStatus.PENDING.value
        assert out_by_type["ORDER_CREATED"].finresolve_mapped_type == "ORDER_CREATED"
        assert out_by_type["INVENTORY_RESERVED"].status == OutboxStatus.SKIPPED_UNSUPPORTED.value

    # 4. Scoped service read connectors work with valid credentials
    s_headers = _service_headers()
    assert client.get(f"/api/v1/service/orders/{order_id}", headers=s_headers).status_code == 200
    assert (
        client.get(f"/api/v1/service/payments/{payment_id}", headers=s_headers).status_code == 200
    )
    assert client.get("/api/v1/service/inventory", headers=s_headers).status_code == 200
    ev_resp = client.get("/api/v1/service/events", headers=s_headers)
    assert ev_resp.status_code == 200
    assert "items" in ev_resp.json()
    assert (
        client.get(f"/api/v1/service/orders/{order_id}/timeline", headers=s_headers).status_code
        == 200
    )
    assert client.get("/api/v1/service/metrics", headers=s_headers).status_code == 200

    # 5. Cross-merchant & stale world_epoch & invalid key rejected
    assert (
        client.get(
            "/api/v1/service/metrics",
            headers=_service_headers(merchant_id="OTHER_MERCHANT"),
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/service/metrics",
            headers=_service_headers(world_epoch="999"),
        ).status_code
        == 409
    )
    assert (
        client.get(
            "/api/v1/service/metrics",
            headers={**s_headers, "X-Commerce-Service-Key": "wrong-key"},
        ).status_code
        == 403
    )
