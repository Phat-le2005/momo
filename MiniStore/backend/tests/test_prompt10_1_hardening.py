"""Prompt 10.1 Hardening Regression Suite — Auth, Payments, Inventory, Outbox, Service Read."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.auth import MOCK_PROVIDER_SECRET_HEADER
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.entities import (
    Customer,
    EventOutbox,
    Inventory,
    LiveProduct,
    MerchantSession,
    Order,
    Payment,
    RefundRequest,
    ServiceCredential,
)
from app.models.enums import (
    OrderStatus,
    OutboxFailureCategory,
    OutboxStatus,
    PaymentStatus,
    ReconciliationStatus,
    RefundRequestStatus,
)
from app.payments.adapter import MockPaymentAdapter
from app.services.events import dispatch_pending_outbox_events
from app.services.seed import (
    DEFAULT_LIVE_SESSION_ID,
    DEFAULT_MERCHANT_ID,
    DEFAULT_SESSION_ID,
)
from app.services.sync import expire_stale_reservations, sync_confirmed_payments_to_orders
from app.worker import run_business_tick


def _guest(client: TestClient, name: str = "Hardening User") -> dict[str, str]:
    r = client.post(
        "/api/v1/store/customers/guest",
        json={"display_name": name, "phone": "0901112222", "default_address": "Quận 1, TP.HCM"},
    )
    assert r.status_code == 201
    return {"X-Customer-Token": r.json()["session_token"]}


def _mock_secret_headers() -> dict[str, str]:
    return {MOCK_PROVIDER_SECRET_HEADER: get_settings().mock_provider_secret.get_secret_value()}


def _service_headers(world_epoch: str | None = "1") -> dict[str, str]:
    h = {
        "X-Commerce-Service-Key": get_settings().service_api_key.get_secret_value(),
        "X-Merchant-Id": DEFAULT_MERCHANT_ID,
        "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
    }
    if world_epoch is not None:
        h["X-World-Epoch"] = world_epoch
    return h


def test_merchant_login_logout_revocation_and_customer_logout(client: TestClient) -> None:
    """Verify password-protected merchant login, session revocation, and customer logout."""
    settings = get_settings()

    # Wrong password -> 401
    bad = client.post(
        "/api/v1/merchant/auth/login",
        json={"username": settings.test_merchant_username, "password": "WrongPassword!"},
    )
    assert bad.status_code == 401

    # Valid login -> 200 with revocable session
    ok = client.post(
        "/api/v1/merchant/auth/login",
        json={
            "username": settings.test_merchant_username,
            "password": settings.test_merchant_password.get_secret_value(),
        },
    )
    assert ok.status_code == 200
    tok = ok.json()["merchant_token"]
    m_hdr = {"X-Merchant-Token": tok}

    assert client.get("/api/v1/merchant/overview", headers=m_hdr).status_code == 200

    # Logout revokes session -> subsequent calls rejected with 401
    lo = client.post("/api/v1/merchant/auth/logout", headers=m_hdr)
    assert lo.status_code == 200
    assert lo.json()["revoked"] is True
    assert client.get("/api/v1/merchant/overview", headers=m_hdr).status_code == 401

    # Expired merchant session rejected with 401
    ok2 = client.post(
        "/api/v1/merchant/auth/login",
        json={
            "username": settings.test_merchant_username,
            "password": settings.test_merchant_password.get_secret_value(),
        },
    )
    tok2 = ok2.json()["merchant_token"]
    sess_id = ok2.json()["session_id"]
    with get_sessionmaker()() as db:
        ms = db.get(MerchantSession, sess_id)
        assert ms is not None
        ms.expires_at = datetime.now(UTC) - timedelta(seconds=10)
        db.commit()
    assert (
        client.get("/api/v1/merchant/overview", headers={"X-Merchant-Token": tok2}).status_code
        == 401
    )

    # Customer logout revokes token -> 401
    cust = _guest(client, "Logout Test")
    assert client.get("/api/v1/store/customers/me", headers=cust).status_code == 200
    assert client.post("/api/v1/store/customers/logout", headers=cust).status_code == 200
    assert client.get("/api/v1/store/customers/me", headers=cust).status_code == 401

    # Expired customer token -> 401
    cust_exp = _guest(client, "Expired Guest")
    with get_sessionmaker()() as db:
        c_row = db.scalar(
            select(Customer).where(Customer.session_token == cust_exp["X-Customer-Token"])
        )
        assert c_row is not None
        c_row.token_expires_at = datetime.now(UTC) - timedelta(seconds=5)
        db.commit()
    assert client.get("/api/v1/store/customers/me", headers=cust_exp).status_code == 401


def test_mock_payment_boundary_and_customer_mock_pay_isolation(client: TestClient) -> None:
    """Direct mock/notify rejects anonymous calls; customer mock-pay enforces order ownership."""
    cust1 = _guest(client, "Owner")
    cust2 = _guest(client, "Other")

    co = client.post(
        "/api/v1/store/checkout",
        headers={**cust1, "X-Idempotency-Key": "IDEMP-PAY-BND-1"},
        json={
            "recipient_name": "Owner",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 1}],
        },
    )
    assert co.status_code == 201
    order = co.json()["order"]
    payment = co.json()["payment"]

    # Anonymous direct provider callback MUST be rejected with 403
    anon = client.post(
        "/api/v1/test-payments/mock/notify",
        json={
            "provider_ref": payment["provider_ref"],
            "notification_id": "NOTIF-ANON-1",
            "merchant_id": DEFAULT_MERCHANT_ID,
            "order_id": order["id"],
            "amount_vnd": payment["amount_vnd"],
            "outcome": "SUCCEEDED",
        },
    )
    assert anon.status_code == 403

    # Other customer cannot trigger mock-pay on cust1's order -> 404
    other_pay = client.post(
        f"/api/v1/store/orders/{order['id']}/mock-pay",
        headers=cust2,
        json={"outcome": "SUCCEEDED", "auto_sync_order": True},
    )
    assert other_pay.status_code == 404

    # Owning customer CAN trigger mock-pay -> 200 & synced
    own_pay = client.post(
        f"/api/v1/store/orders/{order['id']}/mock-pay",
        headers=cust1,
        json={"outcome": "SUCCEEDED", "auto_sync_order": True},
    )
    assert own_pay.status_code == 200
    assert own_pay.json()["payment"]["status"] == PaymentStatus.SUCCEEDED.value
    assert own_pay.json()["orders_synced"] == 1


def test_notification_inbox_payload_conflict_and_late_payment_reconciliation(
    client: TestClient,
) -> None:
    """Durable provider_notifications rejects payload conflict (409); late payment after cancel
    marks RECONCILIATION_REQUIRED without resurrecting order or deducting released inventory."""
    cust = _guest(client, "Late Pay User")
    co = client.post(
        "/api/v1/store/checkout",
        headers={**cust, "X-Idempotency-Key": "IDEMP-LATE-1"},
        json={
            "recipient_name": "Late Pay User",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 2}],
        },
    )
    assert co.status_code == 201
    order = co.json()["order"]
    payment = co.json()["payment"]

    # Send PENDING notification first
    n1 = client.post(
        "/api/v1/test-payments/mock/notify",
        headers=_mock_secret_headers(),
        json={
            "provider_ref": payment["provider_ref"],
            "notification_id": "NOTIF-CONFLICT-1",
            "merchant_id": DEFAULT_MERCHANT_ID,
            "order_id": order["id"],
            "amount_vnd": payment["amount_vnd"],
            "outcome": "PENDING",
            "auto_sync_order": False,
        },
    )
    assert n1.status_code == 200

    # Reusing SAME notification_id with DIFFERENT outcome -> 409 CONFLICT
    n_conflict = client.post(
        "/api/v1/test-payments/mock/notify",
        headers=_mock_secret_headers(),
        json={
            "provider_ref": payment["provider_ref"],
            "notification_id": "NOTIF-CONFLICT-1",
            "merchant_id": DEFAULT_MERCHANT_ID,
            "order_id": order["id"],
            "amount_vnd": payment["amount_vnd"],
            "outcome": "SUCCEEDED",
            "auto_sync_order": False,
        },
    )
    assert n_conflict.status_code == 409

    # Customer cancels unpaid order -> releases inventory
    cancel_r = client.post(f"/api/v1/store/orders/{order['id']}/cancel", headers=cust)
    assert cancel_r.status_code == 200
    assert cancel_r.json()["status"] == OrderStatus.CANCELLED.value

    with get_sessionmaker()() as db:
        inv_before = db.scalar(select(Inventory).where(Inventory.variant_id == "VAR-TECH-03-15M"))
        assert inv_before is not None
        on_hand_before = inv_before.on_hand_qty
        assert inv_before.reserved_qty == 0

    # Late SUCCEEDED payment arrives AFTER cancellation -> MUST NOT resurrect order!
    late = client.post(
        "/api/v1/test-payments/mock/notify",
        headers=_mock_secret_headers(),
        json={
            "provider_ref": payment["provider_ref"],
            "notification_id": "NOTIF-LATE-SUCCEEDED",
            "merchant_id": DEFAULT_MERCHANT_ID,
            "order_id": order["id"],
            "amount_vnd": payment["amount_vnd"],
            "outcome": "SUCCEEDED",
            "auto_sync_order": True,
        },
    )
    assert late.status_code == 200
    assert late.json()["payment"]["reconciliation_required"] is True

    with get_sessionmaker()() as db:
        ord_after = db.get(Order, order["id"])
        inv_after = db.scalar(select(Inventory).where(Inventory.variant_id == "VAR-TECH-03-15M"))
        assert ord_after is not None
        assert ord_after.status == OrderStatus.CANCELLED.value
        assert ord_after.reconciliation_status == ReconciliationStatus.RECONCILIATION_REQUIRED.value
        assert inv_after is not None and inv_after.on_hand_qty == on_hand_before


def test_fully_refunded_before_sync_does_not_confirm_order(client: TestClient) -> None:
    """If payment becomes REFUNDED before sync runs, order MUST NOT be confirmed."""
    cust = _guest(client, "Pre-Sync Refund")
    co = client.post(
        "/api/v1/store/checkout",
        headers={**cust, "X-Idempotency-Key": "IDEMP-PRESYNC-REF-1"},
        json={
            "recipient_name": "Pre-Sync Refund",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 1}],
        },
    )
    order_id = co.json()["order"]["id"]
    pay_id = co.json()["payment"]["id"]

    with get_sessionmaker()() as db:
        pay = db.get(Payment, pay_id)
        assert pay is not None
        pay.status = PaymentStatus.REFUNDED.value
        pay.refunded_amount_vnd = pay.amount_vnd
        pay.is_synced_to_order = False
        db.commit()

        synced = sync_confirmed_payments_to_orders(db)
        assert synced == 1

        ord_row = db.get(Order, order_id)
        pay_row = db.get(Payment, pay_id)
        assert ord_row is not None
        assert ord_row.status == OrderStatus.PENDING_PAYMENT.value
        assert ord_row.reconciliation_status == ReconciliationStatus.RECONCILIATION_REQUIRED.value
        assert pay_row is not None and pay_row.reconciliation_required is True


def test_checkout_fingerprint_conflict_and_flash_sale_quota_restoration(
    client: TestClient,
) -> None:
    """Reusing checkout idempotency_key with different payload -> 409;
    cancelling unpaid Flash Sale order restores flash_sale_reserved_qty cleanly."""
    cust = _guest(client, "Flash User")
    co1 = client.post(
        "/api/v1/store/checkout",
        headers={**cust, "X-Idempotency-Key": "IDEMP-FP-1"},
        json={
            "recipient_name": "Flash User",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "direct_items": [
                {
                    "variant_id": "VAR-TECH-01-WHT",
                    "quantity": 1,
                    "live_session_id": DEFAULT_LIVE_SESSION_ID,
                }
            ],
        },
    )
    assert co1.status_code == 201
    order_id = co1.json()["order"]["id"]

    # Reusing SAME idempotency_key with DIFFERENT quantity -> 409 CONFLICT
    co_conflict = client.post(
        "/api/v1/store/checkout",
        headers={**cust, "X-Idempotency-Key": "IDEMP-FP-1"},
        json={
            "recipient_name": "Flash User",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "direct_items": [
                {
                    "variant_id": "VAR-TECH-01-WHT",
                    "quantity": 2,
                    "live_session_id": DEFAULT_LIVE_SESSION_ID,
                }
            ],
        },
    )
    assert co_conflict.status_code == 409

    # Check flash_sale_reserved_qty == 1 and flash_sale_sold_qty == 0
    with get_sessionmaker()() as db:
        lp = db.get(LiveProduct, "LP-01")
        assert lp is not None
        assert lp.flash_sale_reserved_qty == 1
        assert lp.flash_sale_sold_qty == 0

    # Cancel unpaid order -> flash_sale_reserved_qty MUST return to 0!
    assert client.post(f"/api/v1/store/orders/{order_id}/cancel", headers=cust).status_code == 200
    with get_sessionmaker()() as db:
        lp2 = db.get(LiveProduct, "LP-01")
        assert lp2 is not None
        assert lp2.flash_sale_reserved_qty == 0
        assert lp2.flash_sale_sold_qty == 0


def test_refund_idempotency_replay_after_full_refund(client: TestClient) -> None:
    """Replaying a full refund key after payment became REFUNDED returns original txn."""
    cust = _guest(client, "Full Refund User")
    co = client.post(
        "/api/v1/store/checkout",
        headers={**cust, "X-Idempotency-Key": "IDEMP-FULLREF-1"},
        json={
            "recipient_name": "Full Refund User",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 1}],
        },
    )
    order = co.json()["order"]
    payment = co.json()["payment"]

    assert (
        client.post(
            f"/api/v1/store/orders/{order['id']}/mock-pay",
            headers=cust,
            json={"outcome": "SUCCEEDED", "auto_sync_order": True},
        ).status_code
        == 200
    )

    with get_sessionmaker()() as db:
        inv_before = db.scalar(select(Inventory).where(Inventory.variant_id == "VAR-TECH-03-15M"))
        assert inv_before is not None
        on_hand_before = inv_before.on_hand_qty

        rreq = RefundRequest(
            id="RREQ-FULL-01",
            order_id=order["id"],
            payment_id=payment["id"],
            merchant_id=DEFAULT_MERCHANT_ID,
            commerce_session_id=DEFAULT_SESSION_ID,
            customer_id=order["customer_id"],
            requested_amount_vnd=payment["amount_vnd"],
            reason="Full refund test",
            status=RefundRequestStatus.REQUESTED.value,
        )
        db.add(rreq)
        db.commit()

        adapter = MockPaymentAdapter()
        t1 = adapter.request_refund(db, refund_request=rreq, idempotency_key="IDEMP-REF-FULL-1")
        # Replay with same idempotency_key even though payment is now REFUNDED -> returns t1!
        t2 = adapter.request_refund(db, refund_request=rreq, idempotency_key="IDEMP-REF-FULL-1")
        assert t2.id == t1.id

        # Refund MUST NOT restock inventory
        inv_after = db.scalar(select(Inventory).where(Inventory.variant_id == "VAR-TECH-03-15M"))
        assert inv_after is not None and inv_after.on_hand_qty == on_hand_before


def test_outbox_auth_enforcement_failure_classification_and_worker_separation(
    client: TestClient,
    monkeypatch: object,
) -> None:
    """Outbox requires BOTH session & service tokens; 401 -> DEAD_LETTER; 409 stale world ->
    REVIEW_REQUIRED; core business worker operates independently when FinResolve is offline."""
    cust = _guest(client, "Outbox User")
    co = client.post(
        "/api/v1/store/checkout",
        headers={**cust, "X-Idempotency-Key": "IDEMP-OUTBOX-1"},
        json={
            "recipient_name": "Outbox User",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 1}],
        },
    )
    assert co.status_code == 201
    order = co.json()["order"]

    # 1. Without FINRESOLVE_SERVICE_TOKEN, dispatch returns 0 and leaves events PENDING
    mp = monkeypatch
    assert hasattr(mp, "setenv")
    mp.setenv("COMMERCE_FINRESOLVE_API_BASE_URL", "http://127.0.0.1:8000")
    mp.setenv("COMMERCE_FINRESOLVE_SESSION_TOKEN", "sess-tok")
    mp.setenv("COMMERCE_FINRESOLVE_SERVICE_TOKEN", "")
    get_settings.cache_clear()

    with get_sessionmaker()() as db:
        assert dispatch_pending_outbox_events(db) == 0

    # 2. With BOTH tokens configured, mock HTTP 401 -> DEAD_LETTER (PERMANENT_AUTH)
    mp.setenv("COMMERCE_FINRESOLVE_SERVICE_TOKEN", "svc-tok")
    get_settings.cache_clear()

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized service token"

    with patch("app.services.events.httpx.Client") as mock_client_cls:
        mock_client_cls.return_value.__enter__.return_value.post.return_value = mock_resp
        with get_sessionmaker()() as db:
            delivered = dispatch_pending_outbox_events(db, limit=1)
            assert delivered == 0
            dead = db.scalar(
                select(EventOutbox).where(EventOutbox.status == OutboxStatus.DEAD_LETTER.value)
            )
            assert dead is not None
            assert dead.failure_category == OutboxFailureCategory.PERMANENT_AUTH.value

    # 3. Core business worker still syncs payments even when FinResolve is unreachable
    assert (
        client.post(
            f"/api/v1/store/orders/{order['id']}/mock-pay",
            headers=cust,
            json={"outcome": "SUCCEEDED", "auto_sync_order": False},
        ).status_code
        == 200
    )
    biz = run_business_tick()
    assert biz["synced_orders"] == 1


def test_service_read_cursor_pagination_and_required_world_epoch(client: TestClient) -> None:
    """Service read requires X-World-Epoch (400 if missing) and supports stable cursor paging."""
    # Missing X-World-Epoch -> 400
    r_missing = client.get("/api/v1/service/events", headers=_service_headers(world_epoch=None))
    assert r_missing.status_code == 400

    cust = _guest(client, "Cursor User")
    client.post(
        "/api/v1/store/checkout",
        headers={**cust, "X-Idempotency-Key": "IDEMP-CUR-1"},
        json={
            "recipient_name": "Cursor User",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 1}],
        },
    )

    p1 = client.get("/api/v1/service/events?limit=2", headers=_service_headers("1"))
    assert p1.status_code == 200
    b1 = p1.json()
    assert len(b1["items"]) == 2
    assert b1["has_more"] is True
    assert b1["next_cursor"] is not None

    p2 = client.get(
        f"/api/v1/service/events?limit=2&after_id={b1['next_cursor']}",
        headers=_service_headers("1"),
    )
    assert p2.status_code == 200
    b2 = p2.json()
    assert len(b2["items"]) >= 1
    ids1 = {e["id"] for e in b1["items"]}
    ids2 = {e["id"] for e in b2["items"]}
    assert ids1.isdisjoint(ids2)

    # Revoked service credential -> 403
    with get_sessionmaker()() as db:
        cred = db.scalar(select(ServiceCredential))
        assert cred is not None
        cred.revoked_at = datetime.now(UTC)
        db.commit()
    assert client.get("/api/v1/service/metrics", headers=_service_headers("1")).status_code == 403


def test_unknown_payment_blocks_cancel_and_marks_reconciliation_on_expiry(
    client: TestClient,
) -> None:
    """UNKNOWN payment blocks blind cancellation and triggers RECONCILIATION_REQUIRED on expiry."""
    cust = _guest(client, "Unknown Pay User")
    co = client.post(
        "/api/v1/store/checkout",
        headers={**cust, "X-Idempotency-Key": "IDEMP-UNK-1"},
        json={
            "recipient_name": "Unknown Pay User",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 1}],
        },
    )
    order_id = co.json()["order"]["id"]
    assert (
        client.post(
            f"/api/v1/store/orders/{order_id}/mock-pay",
            headers=cust,
            json={"outcome": "UNKNOWN", "auto_sync_order": False},
        ).status_code
        == 200
    )

    # Customer cannot blindly cancel while payment is UNKNOWN -> 409
    assert client.post(f"/api/v1/store/orders/{order_id}/cancel", headers=cust).status_code == 409

    # Force reservation expiry -> cancels order AND marks RECONCILIATION_REQUIRED
    with get_sessionmaker()() as db:
        from app.models.entities import InventoryReservation

        res = db.scalar(
            select(InventoryReservation).where(InventoryReservation.order_id == order_id)
        )
        assert res is not None
        res.expires_at = datetime.now(UTC) - timedelta(seconds=10)
        db.commit()
        assert expire_stale_reservations(db) == 1
        ord_row = db.get(Order, order_id)
        assert ord_row is not None
        assert ord_row.status == OrderStatus.CANCELLED.value
        assert ord_row.reconciliation_status == ReconciliationStatus.RECONCILIATION_REQUIRED.value
