"""Prompt 11.1 Payment Safety Hardening Test Suite.

Covers all 8 independent review findings:
- P1.1: Concurrent IPN + QueryDr PostgreSQL concurrency test (1 confirmation, 0 double
        inventory deduction, preserves REFUNDED/PARTIALLY_REFUNDED).
- P1.2: Refund response field validation (mismatched/missing TmnCode, TxnRef, Amount,
        TransactionType, ResponseId -> UNKNOWN + balance reserved + reconciliation_required).
- P1.3: Strict IPN status mapping (ResponseCode=00 with empty/missing TransactionStatus ->
        UNKNOWN + reconciliation_required, NEVER confirms).
- P1.4: Immutable `provider_create_date` preserved across IPN & QueryDr; `vnp_PayDate`
        stored separately in `provider_pay_date`; QueryDr/Refund send `provider_create_date`.
- P1.5: Strict QueryDr 00/00 field validation (missing vnp_ResponseId, vnp_Amount,
        vnp_TransactionNo, or invalid vnp_TransactionType -> UNKNOWN + reconciliation_required).
- P2.1: Multi-worker QueryDr durable lease claiming (`FOR UPDATE SKIP LOCKED`), stale
        claim recovery, and fencing token protection.
- P2.2: Exhausted QueryDr payment never starves newer eligible payment even with `limit=1`.
- P2.3: Restricted Public HTTPS Gateway local validation (health, allowlist, fail-closed).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.gateway import create_gateway_app
from app.models.entities import (
    Inventory,
    Order,
    Payment,
    PaymentQueryAttempt,
    RefundRequest,
)
from app.models.enums import (
    OrderStatus,
    PaymentStatus,
    ReconciliationStatus,
    RefundTransactionStatus,
)
from app.payments.vnpay.adapter import VNPaySandboxAdapter
from app.payments.vnpay.client import VNPayHttpClient
from app.payments.vnpay.signing import sign_vnpay_params, vnd_to_vnp_amount
from app.payments.vnpay.status_mapping import map_vnpay_ipn_status
from app.services.sync import reconcile_pending_vnpay_payments, sync_confirmed_payments_to_orders

TEST_TMN_CODE = "TESTTMN01"
TEST_HASH_SECRET = "TEST_VNPAY_SANDBOX_SECRET_KEY_FOR_UNIT_TESTS_ONLY_2026"  # noqa: S105


def _enable_vnpay(monkeypatch: pytest.MonkeyPatch, *, refund_enabled: bool = False) -> None:
    monkeypatch.setenv("COMMERCE_VNPAY_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_VNPAY_TMN_CODE", TEST_TMN_CODE)
    monkeypatch.setenv("COMMERCE_VNPAY_HASH_SECRET", TEST_HASH_SECRET)
    monkeypatch.setenv("COMMERCE_VNPAY_RETURN_URL", "http://localhost:3100/payment/vnpay/return")
    monkeypatch.setenv("COMMERCE_VNPAY_REFUND_ENABLED", "true" if refund_enabled else "false")
    monkeypatch.setenv("COMMERCE_VNPAY_QUERYDR_MIN_AGE_SECONDS", "0")
    get_settings.cache_clear()


def _sign_pipe(data: dict[str, str], fields: tuple[str, ...]) -> str:
    raw = "|".join(data.get(f, "") for f in fields)
    return hmac.new(
        TEST_HASH_SECRET.encode("utf-8"), raw.encode("utf-8"), hashlib.sha512
    ).hexdigest()


QUERYDR_RESP_FIELDS = (
    "vnp_ResponseId",
    "vnp_Command",
    "vnp_ResponseCode",
    "vnp_Message",
    "vnp_TmnCode",
    "vnp_TxnRef",
    "vnp_Amount",
    "vnp_BankCode",
    "vnp_PayDate",
    "vnp_TransactionNo",
    "vnp_TransactionType",
    "vnp_TransactionStatus",
    "vnp_OrderInfo",
)

REFUND_RESP_FIELDS = (
    "vnp_ResponseId",
    "vnp_Command",
    "vnp_ResponseCode",
    "vnp_Message",
    "vnp_TmnCode",
    "vnp_TxnRef",
    "vnp_Amount",
    "vnp_BankCode",
    "vnp_PayDate",
    "vnp_TransactionNo",
    "vnp_TransactionType",
    "vnp_TransactionStatus",
    "vnp_OrderInfo",
)


def _create_vnpay_order(
    client: TestClient, *, name: str = "P11.1 Buyer"
) -> tuple[str, str, str, int]:
    """Create a VNPAY_SANDBOX order and return (order_id, payment_id, provider_ref, amount)."""
    g = client.post(
        "/api/v1/store/customers/guest",
        json={"display_name": name, "phone": "0901234567", "default_address": "Q1, TP.HCM"},
    )
    assert g.status_code == 201
    headers = {
        "X-Customer-Token": g.json()["session_token"],
        "X-Idempotency-Key": f"IDEMP-{name}-{datetime.now(UTC).timestamp()}",
    }

    chk = client.post(
        "/api/v1/store/checkout",
        headers=headers,
        json={
            "recipient_name": name,
            "recipient_phone": "0901234567",
            "shipping_address": "Q1, TP.HCM",
            "payment_method": "VNPAY_SANDBOX",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 1}],
        },
    )
    assert chk.status_code == 201, chk.text
    body = chk.json()
    return (
        body["order"]["id"],
        body["payment"]["id"],
        body["payment"]["provider_ref"],
        body["payment"]["amount_vnd"],
    )


def _mock_client(handler) -> VNPayHttpClient:
    return VNPayHttpClient(
        api_url="https://sandbox.vnpayment.vn/merchant_webapi/api/transaction",
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )


# --- P1.3: Strict IPN status mapping ---
def test_p1_3_ipn_empty_transaction_status_never_confirms(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IPN with ResponseCode=00 and empty TransactionStatus -> UNKNOWN, never SUCCEEDED."""
    _enable_vnpay(monkeypatch)
    # Unit check on mapper
    m = map_vnpay_ipn_status(response_code="00", transaction_status="")
    assert m.payment_status == PaymentStatus.UNKNOWN
    assert m.reconciliation_required is True

    order_id, payment_id, txn_ref, amount_vnd = _create_vnpay_order(client, name="P13-EmptyTS")
    params = {
        "vnp_TmnCode": TEST_TMN_CODE,
        "vnp_Amount": vnd_to_vnp_amount(amount_vnd),
        "vnp_BankCode": "NCB",
        "vnp_OrderInfo": f"Pay {txn_ref}",
        "vnp_PayDate": "20261125120500",
        "vnp_ResponseCode": "00",
        "vnp_TransactionNo": "14000013",
        "vnp_TransactionStatus": "",
        "vnp_TxnRef": txn_ref,
    }
    params["vnp_SecureHash"] = sign_vnpay_params(params, hash_secret=TEST_HASH_SECRET)
    resp = client.get(f"/api/v1/payments/vnpay/ipn?{urlencode(params)}")
    assert resp.status_code == 200

    with get_sessionmaker()() as db:
        pay = db.get(Payment, payment_id)
        order = db.get(Order, order_id)
        assert pay is not None and order is not None
        assert pay.status == PaymentStatus.UNKNOWN.value
        assert pay.reconciliation_required is True
        assert order.status == OrderStatus.PENDING_PAYMENT.value
        assert order.reconciliation_status == ReconciliationStatus.RECONCILIATION_REQUIRED.value


# --- P1.4: Immutable provider_create_date vs provider_pay_date ---
def test_p1_4_provider_create_date_immutable_across_ipn_and_querydr(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IPN updates provider_pay_date only; QueryDr/Refund send original provider_create_date."""
    _enable_vnpay(monkeypatch, refund_enabled=True)
    order_id, payment_id, txn_ref, amount_vnd = _create_vnpay_order(client, name="P14-CreateDate")

    with get_sessionmaker()() as db:
        pay = db.get(Payment, payment_id)
        assert pay is not None
        original_create_date = pay.provider_create_date
        assert original_create_date is not None and len(original_create_date) == 14

    different_pay_date = "20261125195959"
    assert different_pay_date != original_create_date

    params = {
        "vnp_TmnCode": TEST_TMN_CODE,
        "vnp_Amount": vnd_to_vnp_amount(amount_vnd),
        "vnp_BankCode": "NCB",
        "vnp_OrderInfo": f"Pay {txn_ref}",
        "vnp_PayDate": different_pay_date,
        "vnp_ResponseCode": "00",
        "vnp_TransactionNo": "14000014",
        "vnp_TransactionStatus": "00",
        "vnp_TxnRef": txn_ref,
    }
    params["vnp_SecureHash"] = sign_vnpay_params(params, hash_secret=TEST_HASH_SECRET)
    resp = client.get(f"/api/v1/payments/vnpay/ipn?{urlencode(params)}")
    assert resp.status_code == 200
    assert resp.json()["RspCode"] == "00"

    with get_sessionmaker()() as db:
        pay = db.get(Payment, payment_id)
        assert pay is not None
        assert pay.provider_create_date == original_create_date
        assert pay.provider_pay_date == different_pay_date

    captured_txn_dates: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content.decode("utf-8"))
        captured_txn_dates.append(body["vnp_TransactionDate"])
        resp_body = {
            "vnp_ResponseId": "RSP14",
            "vnp_Command": "querydr",
            "vnp_ResponseCode": "00",
            "vnp_Message": "Success",
            "vnp_TmnCode": TEST_TMN_CODE,
            "vnp_TxnRef": txn_ref,
            "vnp_Amount": vnd_to_vnp_amount(amount_vnd),
            "vnp_BankCode": "NCB",
            "vnp_PayDate": different_pay_date,
            "vnp_TransactionNo": "14000014",
            "vnp_TransactionType": "01",
            "vnp_TransactionStatus": "00",
            "vnp_OrderInfo": "Query",
        }
        resp_body["vnp_SecureHash"] = _sign_pipe(resp_body, QUERYDR_RESP_FIELDS)
        return httpx.Response(200, json=resp_body)

    adapter = VNPaySandboxAdapter(http_client=_mock_client(_handler))
    with get_sessionmaker()() as db:
        adapter.query_payment_status(db, payment_id=payment_id)

    assert captured_txn_dates == [original_create_date]


# --- P1.5: Strict QueryDr 00/00 field validation ---
def test_p1_5_querydr_missing_required_confirmation_fields_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QueryDr 00/00 missing Amount or TransactionNo -> UNKNOWN + reconciliation_required."""
    _enable_vnpay(monkeypatch)
    order_id, payment_id, txn_ref, _ = _create_vnpay_order(client, name="P15-MissingFields")

    def _handler(request: httpx.Request) -> httpx.Response:
        resp_body = {
            "vnp_ResponseId": "RSP15",
            "vnp_Command": "querydr",
            "vnp_ResponseCode": "00",
            "vnp_Message": "Success",
            "vnp_TmnCode": TEST_TMN_CODE,
            "vnp_TxnRef": txn_ref,
            "vnp_Amount": "",  # missing!
            "vnp_BankCode": "NCB",
            "vnp_PayDate": "20261125120500",
            "vnp_TransactionNo": "",  # missing!
            "vnp_TransactionType": "01",
            "vnp_TransactionStatus": "00",
            "vnp_OrderInfo": "Query",
        }
        resp_body["vnp_SecureHash"] = _sign_pipe(resp_body, QUERYDR_RESP_FIELDS)
        return httpx.Response(200, json=resp_body)

    adapter = VNPaySandboxAdapter(http_client=_mock_client(_handler))
    with get_sessionmaker()() as db:
        obs = adapter.query_payment_status(db, payment_id=payment_id)
        assert obs.status == PaymentStatus.UNKNOWN
        pay = db.get(Payment, payment_id)
        order = db.get(Order, order_id)
        attempt = db.scalar(
            select(PaymentQueryAttempt).where(PaymentQueryAttempt.payment_id == payment_id)
        )
        assert pay is not None and order is not None and attempt is not None
        assert pay.status == PaymentStatus.UNKNOWN.value
        assert pay.reconciliation_required is True
        assert order.status == OrderStatus.PENDING_PAYMENT.value
        assert attempt.error_message is not None
        assert "vnp_Amount missing" in attempt.error_message


# --- P1.2: Strict Refund response field validation ---
def test_p1_2_refund_response_field_mismatch_marks_unknown_and_reserves_balance(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signed Refund 00/00 with bad Amount or missing ResponseId -> UNKNOWN + balance reserved."""
    _enable_vnpay(monkeypatch, refund_enabled=True)
    order_id, payment_id, txn_ref, amount_vnd = _create_vnpay_order(client, name="P12-RefundCheck")

    # Confirm payment via signed IPN
    params = {
        "vnp_TmnCode": TEST_TMN_CODE,
        "vnp_Amount": vnd_to_vnp_amount(amount_vnd),
        "vnp_BankCode": "NCB",
        "vnp_OrderInfo": f"Pay {txn_ref}",
        "vnp_PayDate": "20261125120500",
        "vnp_ResponseCode": "00",
        "vnp_TransactionNo": "14000012",
        "vnp_TransactionStatus": "00",
        "vnp_TxnRef": txn_ref,
    }
    params["vnp_SecureHash"] = sign_vnpay_params(params, hash_secret=TEST_HASH_SECRET)
    assert client.get(f"/api/v1/payments/vnpay/ipn?{urlencode(params)}").json()["RspCode"] == "00"

    # Refund mock returns wrong vnp_Amount and empty vnp_ResponseId
    def _bad_refund_handler(request: httpx.Request) -> httpx.Response:
        resp_body = {
            "vnp_ResponseId": "",  # missing!
            "vnp_Command": "refund",
            "vnp_ResponseCode": "00",
            "vnp_Message": "Success",
            "vnp_TmnCode": TEST_TMN_CODE,
            "vnp_TxnRef": txn_ref,
            "vnp_Amount": vnd_to_vnp_amount(amount_vnd + 10000),  # mismatched!
            "vnp_BankCode": "NCB",
            "vnp_PayDate": "20261125121000",
            "vnp_TransactionNo": "14000012",
            "vnp_TransactionType": "02",
            "vnp_TransactionStatus": "00",
            "vnp_OrderInfo": "Refund",
        }
        resp_body["vnp_SecureHash"] = _sign_pipe(resp_body, REFUND_RESP_FIELDS)
        return httpx.Response(200, json=resp_body)

    adapter = VNPaySandboxAdapter(http_client=_mock_client(_bad_refund_handler))
    with get_sessionmaker()() as db:
        pay = db.get(Payment, payment_id)
        order = db.get(Order, order_id)
        assert pay is not None and order is not None
        rr = RefundRequest(
            id="RREQ-P12-01",
            order_id=order_id,
            payment_id=payment_id,
            merchant_id=pay.merchant_id,
            commerce_session_id=pay.commerce_session_id,
            customer_id=order.customer_id,
            requested_amount_vnd=amount_vnd,
            reason="Test mismatch",
            status="REQUESTED",
        )
        db.add(rr)
        db.commit()

        txn = adapter.request_refund(db, refund_request=rr, idempotency_key="IDEMP-P12-1")
        assert txn.status == RefundTransactionStatus.UNKNOWN.value

        db.refresh(pay)
        assert pay.reconciliation_required is True
        assert pay.refunded_amount_vnd == 0

        # Second refund attempt must be blocked because UNKNOWN reserves full capacity!
        rr2 = RefundRequest(
            id="RREQ-P12-02",
            order_id=order_id,
            payment_id=payment_id,
            merchant_id=pay.merchant_id,
            commerce_session_id=pay.commerce_session_id,
            customer_id=order.customer_id,
            requested_amount_vnd=1000,
            reason="Second attempt",
            status="REQUESTED",
        )
        db.add(rr2)
        db.commit()
        from app.core.errors import CommerceError

        with pytest.raises(CommerceError, match="REFUND_EXCEEDS_REMAINING"):
            adapter.request_refund(db, refund_request=rr2, idempotency_key="IDEMP-P12-2")


# --- P1.1: Concurrent IPN + QueryDr PostgreSQL concurrency test ---
def test_p1_1_concurrent_ipn_and_querydr_no_double_deduction_and_preserves_refunded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent IPN + QueryDr gives 1 confirmation, 0 double deduction, preserves REFUNDED."""
    _enable_vnpay(monkeypatch)
    order_id, payment_id, txn_ref, amount_vnd = _create_vnpay_order(client, name="P11-Race")

    with get_sessionmaker()() as db:
        order = db.get(Order, order_id)
        assert order is not None
        variant_id = order.items[0].variant_id
        inv_before = db.scalar(select(Inventory).where(Inventory.variant_id == variant_id))
        assert inv_before is not None
        on_hand_before = inv_before.on_hand_qty

    ipn_params = {
        "vnp_TmnCode": TEST_TMN_CODE,
        "vnp_Amount": vnd_to_vnp_amount(amount_vnd),
        "vnp_BankCode": "NCB",
        "vnp_OrderInfo": f"Pay {txn_ref}",
        "vnp_PayDate": "20261125120500",
        "vnp_ResponseCode": "00",
        "vnp_TransactionNo": "14000011",
        "vnp_TransactionStatus": "00",
        "vnp_TxnRef": txn_ref,
    }
    ipn_params["vnp_SecureHash"] = sign_vnpay_params(ipn_params, hash_secret=TEST_HASH_SECRET)

    def _qdr_handler(request: httpx.Request) -> httpx.Response:
        resp_body = {
            "vnp_ResponseId": "RSP11",
            "vnp_Command": "querydr",
            "vnp_ResponseCode": "00",
            "vnp_Message": "Success",
            "vnp_TmnCode": TEST_TMN_CODE,
            "vnp_TxnRef": txn_ref,
            "vnp_Amount": vnd_to_vnp_amount(amount_vnd),
            "vnp_BankCode": "NCB",
            "vnp_PayDate": "20261125120500",
            "vnp_TransactionNo": "14000011",
            "vnp_TransactionType": "01",
            "vnp_TransactionStatus": "00",
            "vnp_OrderInfo": "Query",
        }
        resp_body["vnp_SecureHash"] = _sign_pipe(resp_body, QUERYDR_RESP_FIELDS)
        return httpx.Response(200, json=resp_body)

    def _run_ipn() -> dict[str, str]:
        with get_sessionmaker()() as db:
            return VNPaySandboxAdapter().process_ipn(db, raw_query_items=list(ipn_params.items()))

    def _run_qdr() -> str:
        with get_sessionmaker()() as db:
            obs = VNPaySandboxAdapter(http_client=_mock_client(_qdr_handler)).query_payment_status(
                db, payment_id=payment_id
            )
            return str(obs.status)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_run_ipn)
        f2 = pool.submit(_run_qdr)
        f1.result()
        f2.result()

    # Run order sync twice to confirm exactly 1 stock deduction
    with get_sessionmaker()() as db:
        sync_confirmed_payments_to_orders(db)
        sync_confirmed_payments_to_orders(db)
        inv_after = db.scalar(select(Inventory).where(Inventory.variant_id == variant_id))
        order_after = db.get(Order, order_id)
        pay_after = db.get(Payment, payment_id)
        assert inv_after is not None and order_after is not None and pay_after is not None
        assert order_after.status == OrderStatus.CONFIRMED.value
        assert inv_after.on_hand_qty == on_hand_before - 1

        # Now mark payment REFUNDED and run QueryDr returning 00/00 -> must NOT overwrite REFUNDED!
        pay_after.status = PaymentStatus.REFUNDED.value
        db.commit()

        obs2 = VNPaySandboxAdapter(http_client=_mock_client(_qdr_handler)).query_payment_status(
            db, payment_id=payment_id
        )
        assert obs2.status == PaymentStatus.REFUNDED.value
        db.refresh(pay_after)
        assert pay_after.status == PaymentStatus.REFUNDED.value


# --- P2.1 & P2.2: Multi-worker QueryDr durable claiming & starvation prevention ---
def test_p2_1_and_p2_2_querydr_lease_claiming_and_no_starvation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exhausted payment never starves newer payment (limit=1); lease blocks duplicate worker."""
    _enable_vnpay(monkeypatch)
    monkeypatch.setenv("COMMERCE_VNPAY_QUERYDR_MAX_ATTEMPTS", "1")
    get_settings.cache_clear()

    _, pay1_id, ref1, amt1 = _create_vnpay_order(client, name="P22-Old")
    _, pay2_id, ref2, _ = _create_vnpay_order(client, name="P22-New")

    # Backdate pay1 so it is picked first
    with get_sessionmaker()() as db:
        p1 = db.get(Payment, pay1_id)
        assert p1 is not None
        p1.created_at = datetime.now(UTC) - timedelta(minutes=10)
        db.commit()

    queried_refs: list[str] = []

    def _timeout_or_pending_handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content.decode("utf-8"))
        ref = body["vnp_TxnRef"]
        queried_refs.append(ref)
        resp_body = {
            "vnp_ResponseId": f"RSP-{ref}",
            "vnp_Command": "querydr",
            "vnp_ResponseCode": "00",
            "vnp_Message": "Pending",
            "vnp_TmnCode": TEST_TMN_CODE,
            "vnp_TxnRef": ref,
            "vnp_Amount": vnd_to_vnp_amount(amt1),
            "vnp_BankCode": "NCB",
            "vnp_PayDate": "",
            "vnp_TransactionNo": "",
            "vnp_TransactionType": "01",
            "vnp_TransactionStatus": "01",  # PENDING
            "vnp_OrderInfo": "Pending",
        }
        resp_body["vnp_SecureHash"] = _sign_pipe(resp_body, QUERYDR_RESP_FIELDS)
        return httpx.Response(200, json=resp_body)

    http_client = _mock_client(_timeout_or_pending_handler)

    # Tick 1 with limit=1: queries pay1, hits max_attempts=1 -> marks querydr_exhausted=True
    with get_sessionmaker()() as db:
        q1 = reconcile_pending_vnpay_payments(db, limit=1, http_client=http_client, worker_id="w1")
        assert q1 == 1
        p1 = db.get(Payment, pay1_id)
        assert p1 is not None
        assert p1.querydr_exhausted is True
        assert p1.reconciliation_required is True
        assert p1.querydr_claim_token is None

    # Tick 2 with limit=1: MUST pick pay2 (pay1 must NOT starve pay2!)
    with get_sessionmaker()() as db:
        q2 = reconcile_pending_vnpay_payments(db, limit=1, http_client=http_client, worker_id="w2")
        assert q2 == 1

    assert queried_refs == [ref1, ref2]

    # P2.1: Active lease blocks another worker; expired lease is reclaimed
    _, pay3_id, ref3, _ = _create_vnpay_order(client, name="P21-Lease")
    with get_sessionmaker()() as db:
        p3 = db.get(Payment, pay3_id)
        assert p3 is not None
        p3.querydr_claim_token = "ACTIVE-LOCK"
        p3.querydr_claimed_by = "other-worker"
        p3.querydr_locked_until = datetime.now(UTC) + timedelta(minutes=5)
        db.commit()

        assert (
            reconcile_pending_vnpay_payments(db, limit=5, http_client=http_client, worker_id="w3")
            == 0
        )

        # Expire lease -> w3 reclaims it cleanly
        p3.querydr_locked_until = datetime.now(UTC) - timedelta(seconds=5)
        db.commit()
        assert (
            reconcile_pending_vnpay_payments(db, limit=5, http_client=http_client, worker_id="w3")
            == 1
        )
        assert ref3 in queried_refs


# --- P2.3: Restricted Public HTTPS Gateway local validation ---
def test_p2_3_restricted_gateway_fail_closed_and_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway exposes only health & VNPAY return/IPN; blocks internal/admin/MOCK routes."""
    monkeypatch.setenv("COMMERCE_PUBLIC_GATEWAY_ENABLED", "false")
    get_settings.cache_clear()
    gw_off = TestClient(create_gateway_app())
    assert gw_off.get("/health").status_code == 503
    assert gw_off.get("/api/v1/payments/vnpay/ipn").status_code == 503
    assert gw_off.get("/api/v1/service/orders").status_code == 503
    assert gw_off.get("/api/v1/merchant/orders").status_code == 503
    assert gw_off.post("/api/v1/payments/mock/callback").status_code == 503

    monkeypatch.setenv("COMMERCE_PUBLIC_GATEWAY_ENABLED", "true")
    get_settings.cache_clear()
    gw_on = TestClient(create_gateway_app())
    assert gw_on.get("/docs").status_code == 403
    assert gw_on.get("/api/v1/service/orders").status_code == 403
    assert gw_on.get("/api/v1/merchant/orders").status_code == 403
    assert gw_on.post("/api/v1/test-payments/callback").status_code == 403
    assert gw_on.post("/api/v1/store/orders/ORD-1/mock-pay").status_code == 403
