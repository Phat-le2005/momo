"""Prompt 11 Comprehensive Automated Test Suite — VNPAY Sandbox, P1 Safety & Public Gateway.

Covers Section 20:
- Provider configuration & production host rejection.
- Official VNPAY 2.1.0 HMAC-SHA512 signing, parameter encoding, integer VND x100 conversion.
- Payment creation, unique vnp_TxnRef, ReturnURL read-only invariant & ownership.
- Official IPN (`00`, `01`, `02`, `04`, `97`, `99`, duplicate/out-of-order, cross-provider).
- QueryDr reconciliation (signed response, invalid sig, API vs payment success, timeout,
  missing IPN recovery, bounded retries, conflicting provider observation).
- Capability-gated Refund API (disabled, excessive, idempotent replay, pending/unknown accounting).
- P1 Commerce safety fixes (no manual CONFIRMED, no generic CANCEL, evidence-based reconciliation,
  fully refunded pre-sync releases reservations once, outbox HTTP 429 Retry-After).
- Restricted Public HTTPS Gateway (allowlist, blocked internal/MOCK routes, raw IPN forwarding,
  fail-closed when disabled).

Uses deterministic test fixtures and mocked `httpx.MockTransport` — zero live traffic to VNPAY.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import CommerceSettings, get_settings
from app.db.session import get_sessionmaker
from app.gateway import create_gateway_app
from app.models.entities import (
    EventOutbox,
    Inventory,
    InventoryReservation,
    Order,
    Payment,
    PaymentQueryAttempt,
    RefundRequest,
)
from app.models.enums import (
    OrderStatus,
    OutboxFailureCategory,
    OutboxStatus,
    PaymentProvider,
    PaymentStatus,
    ReconciliationStatus,
    RefundTransactionStatus,
    ReservationStatus,
)
from app.payments.vnpay.adapter import VNPaySandboxAdapter
from app.payments.vnpay.client import VNPayHttpClient
from app.payments.vnpay.signing import (
    build_vnpay_payment_url,
    format_vnpay_timestamp,
    sign_querydr_request,
    sign_refund_request,
    sign_vnpay_params,
    verify_querydr_response_signature,
    verify_refund_response_signature,
    verify_vnpay_params_signature,
    vnd_to_vnp_amount,
    vnp_amount_to_vnd,
)
from app.services.events import dispatch_pending_outbox_events
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


def _guest(client: TestClient, name: str = "VNPAY Tester") -> dict[str, str]:
    r = client.post(
        "/api/v1/store/customers/guest",
        json={"display_name": name, "phone": "0901112222", "default_address": "Q1, TP.HCM"},
    )
    assert r.status_code == 201
    return {"X-Customer-Token": r.json()["session_token"]}


def _merchant(client: TestClient) -> dict[str, str]:
    s = get_settings()
    r = client.post(
        "/api/v1/merchant/auth/login",
        json={
            "username": s.test_merchant_username,
            "password": s.test_merchant_password.get_secret_value(),
        },
    )
    assert r.status_code == 200
    return {"X-Merchant-Token": r.json()["merchant_token"]}


def _sign_response_pipe(data: dict[str, str], secret: str) -> str:
    import hashlib
    import hmac

    fields = (
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
    if data.get("vnp_Command") == "querydr":
        fields += ("vnp_PromotionCode", "vnp_PromotionAmount")
    raw = "|".join(data.get(f, "") for f in fields)
    return hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha512).hexdigest()


def test_querydr_research_vectors_and_malformed_signatures() -> None:
    key = "VNPAY-RESEARCH-FAKE-KEY-DO-NOT-USE"
    body = {
        "vnp_ResponseId": "RESP0001",
        "vnp_Command": "querydr",
        "vnp_ResponseCode": "00",
        "vnp_Message": "Query success",
        "vnp_TmnCode": "DEMO0001",
        "vnp_TxnRef": "FS001",
        "vnp_Amount": 12_500_000,
        "vnp_BankCode": "NCB",
        "vnp_PayDate": "20261129103100",
        "vnp_TransactionNo": "12345678",
        "vnp_TransactionType": "01",
        "vnp_TransactionStatus": "00",
        "vnp_OrderInfo": "Truy van giao dich FS001",
    }
    empty_slots = (
        "feeb35da0213e790ad12861177d00c39c5f876b8f23cd7cd10d650cec605aa6ad0"
        "f3ead4606be1e99d7155ac7a1bef95bcf4ab58d95de4886debc54c263af0b5"
    )
    zero_promotion = (
        "12527ae3cb7769457e2d8a30aa883db43f3a115b8bfb5a00f5b9fd0fcab39b522"
        "e171ce1e08ff260cbce975083dfdc8f7708572d020595abe1838203f32aa5b5"
    )
    legacy = (
        "1d3f89211542473d6c9eb19683bed2789cf0005409e209b8566419bd4588e12f"
        "6e2f0ed90a4cc437bcc06e3f32df0c128dc104c3a25e23587c3957f238e9741f"
    )
    assert verify_querydr_response_signature(
        {**body, "vnp_SecureHash": empty_slots}, hash_secret=key
    )
    promo = {**body, "vnp_PromotionCode": "123", "vnp_PromotionAmount": 0}
    assert verify_querydr_response_signature(
        {**promo, "vnp_SecureHash": zero_promotion}, hash_secret=key
    )
    for invalid in (legacy, "đ" * 128, "", "f" * 10000):
        assert not verify_querydr_response_signature(
            {**body, "vnp_SecureHash": invalid}, hash_secret=key
        )


def test_pay_and_ipn_independent_canonical_vectors() -> None:
    from urllib.parse import parse_qsl

    key = "VNPAY-RESEARCH-FAKE-KEY-DO-NOT-USE"
    pay = (
        "vnp_Amount=12500000&vnp_Command=pay&vnp_CreateDate=20261129103000&vnp_CurrCode=VND"
        "&vnp_ExpireDate=20261129104500&vnp_IpAddr=127.0.0.1&vnp_Locale=vn"
        "&vnp_OrderInfo=Thanh+toan+don+hang+FS001&vnp_OrderType=other"
        "&vnp_ReturnUrl=https%3A%2F%2Fshop.example.test%2Fpayment%2Fvnpay%2Freturn"
        "&vnp_TmnCode=DEMO0001&vnp_TxnRef=FS001&vnp_Version=2.1.0"
    )
    expected = (
        "21c3718acbcd657acb7639c113b69567c65940bbbf0bffd53bb3bec6f05fb0ed8c"
        "46b7345233f24cebbf627e46653555f4a598d13fd70bbc8ec37d5b919edb78"
    )
    url, digest = build_vnpay_payment_url(
        "https://sandbox.vnpaytest.vn/paymentv2/vpcpay.html",
        dict(reversed(parse_qsl(pay))),
        hash_secret=key,
    )
    assert digest == expected
    assert url.split("?", 1)[1] == f"{pay}&vnp_SecureHash={expected}"
    ipn = dict(
        parse_qsl(
            "vnp_Amount=12500000&vnp_BankCode=NCB&vnp_BankTranNo=TEST123&vnp_CardType=ATM"
            "&vnp_OrderInfo=Thanh+toan+don+hang+FS001&vnp_PayDate=20261129103100"
            "&vnp_ResponseCode=00&vnp_TmnCode=DEMO0001&vnp_TransactionNo=12345678"
            "&vnp_TransactionStatus=00&vnp_TxnRef=FS001"
        )
    )
    ipn["vnp_SecureHash"] = (
        "ab05ead5566c8a55c95debce622242a4b464dbf4b6192eee28866ae438b8e4323"
        "da358e29b0cb0e0e1ab2330d972ab52026807df95bb0477b370bb3204750559"
    )
    assert verify_vnpay_params_signature({**ipn, "edge_tag": "ignored"}, hash_secret=key)
    assert not verify_vnpay_params_signature({**ipn, "vnp_Amount": "12500100"}, hash_secret=key)


# --- 1. Provider Configuration & Production URL Rejection ---
def test_provider_configuration_defaults_and_production_url_rejection(
    client: TestClient,
) -> None:
    """App starts without VNPAY creds; unconfigured VNPAY fails checkout; prod URLs rejected."""
    boot = client.get("/api/v1/store/bootstrap")
    assert boot.status_code == 200
    methods = {m["id"]: m for m in boot.json()["payment_methods"]}
    assert methods["LOCAL_MOCK"]["available"] is True
    assert methods["VNPAY_SANDBOX"]["available"] is False
    # Never expose VNPAY secret in bootstrap
    assert TEST_HASH_SECRET not in json.dumps(boot.json())

    # Checkout with unconfigured VNPAY fails cleanly with 503 (never falls back to LOCAL_MOCK)
    cust = _guest(client)
    res = client.post(
        "/api/v1/store/checkout",
        headers={**cust, "X-Idempotency-Key": "IDEMP-VNP-UNCONF-1"},
        json={
            "recipient_name": "Test",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "payment_method": "VNPAY_SANDBOX",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 1}],
        },
    )
    assert res.status_code == 503

    # Production VNPAY URL MUST be rejected at config validation
    with pytest.raises(ValueError, match="approved VNPAY Sandbox endpoint"):
        CommerceSettings(
            app_env="test",
            vnpay_payment_url="https://pay.vnpay.vn/vpcpay.html",
        )


# --- 2. Official VNPAY 2.1.0 Signing & Encoding Verification ---
def test_vnpay_210_signing_encoding_and_amount_conversion() -> None:
    """Verify HMAC-SHA512 signing, parameter sorting, GMT+7 timestamps, and VND x100."""
    assert vnd_to_vnp_amount(249000) == "24900000"
    assert vnp_amount_to_vnd("24900000") == 249000
    with pytest.raises(ValueError):
        vnp_amount_to_vnd("24900050")  # not a multiple of 100

    dt = datetime(2026, 11, 25, 6, 30, 0, tzinfo=UTC)
    assert format_vnpay_timestamp(dt) == "20261125133000"  # GMT+7

    params = {
        "vnp_Version": "2.1.0",
        "vnp_Command": "pay",
        "vnp_TmnCode": TEST_TMN_CODE,
        "vnp_Amount": "10000000",
        "vnp_CurrCode": "VND",
        "vnp_TxnRef": "FS261125TEST01",
        "vnp_OrderInfo": "Thanh toan don hang FS01",
        "vnp_OrderType": "other",
        "vnp_Locale": "vn",
        "vnp_ReturnUrl": "http://localhost:3100/payment/vnpay/return",
        "vnp_IpAddr": "127.0.0.1",
        "vnp_CreateDate": "20261125133000",
    }
    url, sig = build_vnpay_payment_url(
        "https://sandbox.vnpayment.vn/paymentv2/vpcpay.html",
        params,
        hash_secret=TEST_HASH_SECRET,
    )
    assert url.startswith("https://sandbox.vnpayment.vn/paymentv2/vpcpay.html?")
    assert f"vnp_SecureHash={sig}" in url
    assert verify_vnpay_params_signature(
        {**params, "vnp_SecureHash": sig}, hash_secret=TEST_HASH_SECRET
    )
    assert not verify_vnpay_params_signature(
        {**params, "vnp_Amount": "99999900", "vnp_SecureHash": sig},
        hash_secret=TEST_HASH_SECRET,
    )

    # QueryDr & Refund pipe signatures
    q_sig = sign_querydr_request(
        {
            "vnp_RequestId": "Q1",
            "vnp_Version": "2.1.0",
            "vnp_Command": "querydr",
            "vnp_TmnCode": TEST_TMN_CODE,
            "vnp_TxnRef": "FS01",
            "vnp_TransactionDate": "20261125133000",
            "vnp_CreateDate": "20261125133500",
            "vnp_IpAddr": "127.0.0.1",
            "vnp_OrderInfo": "Query",
        },
        hash_secret=TEST_HASH_SECRET,
    )
    assert len(q_sig) == 128

    r_sig = sign_refund_request(
        {
            "vnp_RequestId": "R1",
            "vnp_Version": "2.1.0",
            "vnp_Command": "refund",
            "vnp_TmnCode": TEST_TMN_CODE,
            "vnp_TransactionType": "02",
            "vnp_TxnRef": "FS01",
            "vnp_Amount": "10000000",
            "vnp_TransactionNo": "14400011",
            "vnp_TransactionDate": "20261125133000",
            "vnp_CreateBy": "merchant_owner",
            "vnp_CreateDate": "20261125134000",
            "vnp_IpAddr": "127.0.0.1",
            "vnp_OrderInfo": "Refund",
        },
        hash_secret=TEST_HASH_SECRET,
    )
    assert len(r_sig) == 128


# --- 3. VNPAY Payment Creation, ReturnURL Read-Only & IPN Protocol ---
def test_ipn_quota_admits_authenticated_callback_after_unsigned_traffic(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_vnpay(monkeypatch)
    monkeypatch.setenv("COMMERCE_RATE_LIMIT_PAYMENT_PER_MINUTE", "1")
    get_settings.cache_clear()
    path = "/api/v1/payments/vnpay/ipn"
    payload = {"vnp_TmnCode": TEST_TMN_CODE, "vnp_TxnRef": "SYNTHETIC_UNKNOWN"}
    for _ in range(3):
        assert client.get(path, params=payload).json()["RspCode"] == "97"

    payload["vnp_SecureHash"] = sign_vnpay_params(payload, hash_secret=TEST_HASH_SECRET)
    assert client.get(path, params=payload).json()["RspCode"] == "01"
    throttled = client.get(path, params=payload)
    assert throttled.status_code == 200
    assert throttled.json()["RspCode"] == "99"


def test_vnpay_checkout_return_url_and_ipn_lifecycle(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full VNPAY flow: checkout -> redirect_url -> ReturnURL (cannot confirm!) -> IPN -> sync."""
    _enable_vnpay(monkeypatch)
    cust1 = _guest(client, "VNPAY Buyer")
    cust2 = _guest(client, "Other Buyer")

    co = client.post(
        "/api/v1/store/checkout",
        headers={**cust1, "X-Idempotency-Key": "IDEMP-VNP-001"},
        json={
            "recipient_name": "VNPAY Buyer",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "payment_method": "VNPAY_SANDBOX",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 1}],
        },
    )
    assert co.status_code == 201
    order = co.json()["order"]
    pay = co.json()["payment"]
    assert pay["provider"] == PaymentProvider.VNPAY_SANDBOX.value
    assert pay["status"] == PaymentStatus.PENDING.value
    assert pay["redirect_url"].startswith("https://sandbox.vnpayment.vn/paymentv2/vpcpay.html?")
    qs = parse_qs(urlparse(pay["redirect_url"]).query)
    assert qs["vnp_Amount"][0] == str(order["total_vnd"] * 100)
    assert qs["vnp_TxnRef"][0] == pay["provider_ref"]

    # Cross-provider safety: mock-pay MUST be rejected (403) on VNPAY payment!
    assert (
        client.post(
            f"/api/v1/store/orders/{order['id']}/mock-pay",
            headers=cust1,
            json={"outcome": "SUCCEEDED"},
        ).status_code
        == 403
    )

    # ReturnURL with signed success params MUST NOT confirm payment or order!
    ret_params = {
        "vnp_TmnCode": TEST_TMN_CODE,
        "vnp_Amount": str(order["total_vnd"] * 100),
        "vnp_TxnRef": pay["provider_ref"],
        "vnp_ResponseCode": "00",
        "vnp_TransactionStatus": "00",
        "vnp_TransactionNo": "14499001",
        "vnp_PayDate": "20261125134500",
    }
    ret_params["vnp_SecureHash"] = sign_vnpay_params(ret_params, hash_secret=TEST_HASH_SECRET)

    # Other customer cannot inspect cust1's ReturnURL -> 404
    assert (
        client.get(
            f"/api/v1/store/payments/vnpay/return-verify?{urlencode(ret_params)}",
            headers=cust2,
        ).status_code
        == 404
    )

    ret_res = client.get(
        f"/api/v1/store/payments/vnpay/return-verify?{urlencode(ret_params)}",
        headers=cust1,
    )
    assert ret_res.status_code == 200
    assert ret_res.json()["return_signature_valid"] is True
    assert ret_res.json()["payment"]["status"] == PaymentStatus.PENDING.value
    assert ret_res.json()["order"]["status"] == OrderStatus.PENDING_PAYMENT.value

    # IPN with invalid signature -> RspCode 97
    bad_sig = {**ret_params, "vnp_SecureHash": "0" * 128}
    r97 = client.get(f"/api/v1/payments/vnpay/ipn?{urlencode(bad_sig)}")
    assert r97.status_code == 200
    assert r97.json()["RspCode"] == "97"

    # IPN with unknown TxnRef -> RspCode 01
    unk = {**ret_params, "vnp_TxnRef": "UNKNOWN_REF_999"}
    unk["vnp_SecureHash"] = sign_vnpay_params(unk, hash_secret=TEST_HASH_SECRET)
    r01 = client.get(f"/api/v1/payments/vnpay/ipn?{urlencode(unk)}")
    assert r01.json()["RspCode"] == "01"

    # IPN with wrong amount -> RspCode 04
    bad_amt = {**ret_params, "vnp_Amount": "100000"}
    bad_amt["vnp_SecureHash"] = sign_vnpay_params(bad_amt, hash_secret=TEST_HASH_SECRET)
    r04 = client.get(f"/api/v1/payments/vnpay/ipn?{urlencode(bad_amt)}")
    assert r04.json()["RspCode"] == "04"

    # Valid signed IPN -> RspCode 00
    r00 = client.get(f"/api/v1/payments/vnpay/ipn?{urlencode(ret_params)}")
    assert r00.status_code == 200
    assert r00.json()["RspCode"] == "00"

    # Duplicate IPN -> RspCode 02 (idempotent!)
    r02 = client.get(f"/api/v1/payments/vnpay/ipn?{urlencode(ret_params)}")
    assert r02.json()["RspCode"] == "02"

    # Out-of-order FAILED IPN after SUCCEEDED -> RspCode 02, does NOT regress payment!
    ooo = {**ret_params, "vnp_ResponseCode": "24", "vnp_TransactionStatus": "02"}
    ooo["vnp_SecureHash"] = sign_vnpay_params(ooo, hash_secret=TEST_HASH_SECRET)
    r_ooo = client.get(f"/api/v1/payments/vnpay/ipn?{urlencode(ooo)}")
    assert r_ooo.json()["RspCode"] == "02"

    # Sync worker confirms order
    with get_sessionmaker()() as db:
        assert sync_confirmed_payments_to_orders(db) == 1
        ord_row = db.get(Order, order["id"])
        pay_row = db.get(Payment, pay["id"])
        assert ord_row is not None and ord_row.status == OrderStatus.CONFIRMED.value
        assert pay_row is not None
        assert pay_row.status == PaymentStatus.SUCCEEDED.value
        assert pay_row.provider_txn_no == "14499001"
        assert pay_row.observation_source == "VNPAY_IPN"
        assert pay_row.signature_verified is True


# --- 4. QueryDr Reconciliation (Signed, Invalid Sig, Timeout, Missing IPN Recovery) ---
def test_vnpay_querydr_reconciliation_and_timeout_handling(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QueryDr recovers missing IPN, rejects bad signatures, preserves UNKNOWN on timeout."""
    _enable_vnpay(monkeypatch)
    monkeypatch.setenv("COMMERCE_VNPAY_QUERYDR_SERVER_IP", "192.0.2.15")
    get_settings.cache_clear()
    cust = _guest(client, "QueryDr User")

    co = client.post(
        "/api/v1/store/checkout",
        headers={**cust, "X-Idempotency-Key": "IDEMP-QDR-001"},
        json={
            "recipient_name": "QueryDr User",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "payment_method": "VNPAY_SANDBOX",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 1}],
        },
    )
    order = co.json()["order"]
    pay = co.json()["payment"]

    # 1. Network timeout -> marks UNKNOWN (does NOT mark FAILED!)
    def _timeout_handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Simulated VNPAY timeout")

    timeout_client = VNPayHttpClient(transport=httpx.MockTransport(_timeout_handler))
    with get_sessionmaker()() as db:
        obs = VNPaySandboxAdapter(http_client=timeout_client).query_payment_status(
            db, payment_id=pay["id"]
        )
        assert obs.status == PaymentStatus.UNKNOWN.value

    # 2. Invalid response signature -> rejected, does NOT mark SUCCEEDED!
    def _bad_sig_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "vnp_ResponseId": "RESP-BAD",
                "vnp_Command": "querydr",
                "vnp_ResponseCode": "00",
                "vnp_Message": "Success",
                "vnp_TmnCode": TEST_TMN_CODE,
                "vnp_TxnRef": pay["provider_ref"],
                "vnp_Amount": str(order["total_vnd"] * 100),
                "vnp_BankCode": "NCB",
                "vnp_PayDate": "20261125135000",
                "vnp_TransactionNo": "14499002",
                "vnp_TransactionType": "01",
                "vnp_TransactionStatus": "00",
                "vnp_OrderInfo": "Query",
                "vnp_SecureHash": "deadbeef",
            },
        )

    bad_sig_client = VNPayHttpClient(transport=httpx.MockTransport(_bad_sig_handler))
    with get_sessionmaker()() as db:
        obs_bad = VNPaySandboxAdapter(http_client=bad_sig_client).query_payment_status(
            db, payment_id=pay["id"]
        )
        assert obs_bad.status == PaymentStatus.UNKNOWN.value

    def _unsigned_error_handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        assert payload["vnp_IpAddr"] == "192.0.2.15"
        return httpx.Response(
            200, json={"vnp_ResponseCode": "99", "vnp_Message": "Unexpected error"}
        )

    unsigned_client = VNPayHttpClient(transport=httpx.MockTransport(_unsigned_error_handler))
    with get_sessionmaker()() as db:
        observation = VNPaySandboxAdapter(http_client=unsigned_client).query_payment_status(
            db, payment_id=pay["id"]
        )
        assert observation.status == PaymentStatus.UNKNOWN.value

    def _signed_query_response(transaction_status: str) -> httpx.Response:
        body = {
            "vnp_ResponseId": "RESP-OK-1",
            "vnp_Command": "querydr",
            "vnp_ResponseCode": "00",
            "vnp_Message": "Success",
            "vnp_TmnCode": TEST_TMN_CODE,
            "vnp_TxnRef": pay["provider_ref"],
            "vnp_Amount": str(order["total_vnd"] * 100),
            "vnp_BankCode": "NCB",
            "vnp_PayDate": "20261125135100",
            "vnp_TransactionNo": "14499002",
            "vnp_TransactionType": "01",
            "vnp_TransactionStatus": transaction_status,
            "vnp_OrderInfo": "Query",
        }
        body["vnp_SecureHash"] = _sign_response_pipe(body, TEST_HASH_SECRET)
        assert verify_querydr_response_signature(body, hash_secret=TEST_HASH_SECRET)
        return httpx.Response(200, json=body)

    pending_client = VNPayHttpClient(
        transport=httpx.MockTransport(lambda _: _signed_query_response("01"))
    )
    with get_sessionmaker()() as db:
        observation = VNPaySandboxAdapter(http_client=pending_client).query_payment_status(
            db, payment_id=pay["id"]
        )
        assert observation.status == PaymentStatus.UNKNOWN.value

    ok_client = VNPayHttpClient(
        transport=httpx.MockTransport(lambda _: _signed_query_response("00"))
    )
    with get_sessionmaker()() as db:
        obs_ok = VNPaySandboxAdapter(http_client=ok_client).query_payment_status(
            db, payment_id=pay["id"]
        )
        assert obs_ok.status == PaymentStatus.SUCCEEDED.value
        assert obs_ok.observation_source == "VNPAY_QUERYDR"
        assert obs_ok.signature_verified is True

        attempts = list(
            db.scalars(
                select(PaymentQueryAttempt).where(PaymentQueryAttempt.payment_id == pay["id"])
            ).all()
        )
        assert len(attempts) == 5
        errors = {attempt.error_message for attempt in attempts}
        assert "VNPAY QueryDR response signature missing" in errors
        assert "VNPAY QueryDR response signature invalid" in errors
        assert any(
            attempt.signature_verified and attempt.mapped_payment_status == PaymentStatus.PENDING
            for attempt in attempts
        )

        # Completed payment is NOT queried again by background reconciler
        assert reconcile_pending_vnpay_payments(db, http_client=ok_client) == 0

    # An unsigned query error cannot undermine payment evidence already verified.
    with get_sessionmaker()() as db:
        observation = VNPaySandboxAdapter(http_client=unsigned_client).query_payment_status(
            db, payment_id=pay["id"]
        )
        assert observation.status == PaymentStatus.SUCCEEDED.value
        assert observation.signature_verified is True


# --- 5. Capability-Gated VNPAY Refund API ---
def test_vnpay_refund_capability_gate_and_pending_accounting(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VNPAY refund disabled by default; when enabled, pending/unknown reserves capacity."""
    _enable_vnpay(monkeypatch, refund_enabled=False)
    cust = _guest(client, "Refund VNPAY User")

    co = client.post(
        "/api/v1/store/checkout",
        headers={**cust, "X-Idempotency-Key": "IDEMP-VNPREF-1"},
        json={
            "recipient_name": "Refund VNPAY User",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "payment_method": "VNPAY_SANDBOX",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 1}],
        },
    )
    order = co.json()["order"]
    pay = co.json()["payment"]

    # Mark payment SUCCEEDED via signed IPN
    ipn = {
        "vnp_TmnCode": TEST_TMN_CODE,
        "vnp_Amount": str(order["total_vnd"] * 100),
        "vnp_TxnRef": pay["provider_ref"],
        "vnp_ResponseCode": "00",
        "vnp_TransactionStatus": "00",
        "vnp_TransactionNo": "14499099",
        "vnp_PayDate": "20261125135500",
    }
    ipn["vnp_SecureHash"] = sign_vnpay_params(ipn, hash_secret=TEST_HASH_SECRET)
    assert client.get(f"/api/v1/payments/vnpay/ipn?{urlencode(ipn)}").json()["RspCode"] == "00"

    rreq_res = client.post(
        f"/api/v1/store/orders/{order['id']}/refund-requests",
        headers=cust,
        json={"requested_amount_vnd": order["total_vnd"], "reason": "Đổi ý"},
    )
    assert rreq_res.status_code == 201
    rreq_id = rreq_res.json()["id"]

    # 1. Refund disabled -> 403
    m_hdr = _merchant(client)
    dec_disabled = client.post(
        f"/api/v1/merchant/refunds/{rreq_id}/decision",
        headers=m_hdr,
        json={"decision": "APPROVE"},
    )
    assert dec_disabled.status_code == 403

    # 2. Enable refund and mock VNPAY TransactionStatus=05 (PENDING — never fake SUCCEEDED!)
    _enable_vnpay(monkeypatch, refund_enabled=True)

    def _refund_pending_handler(_req: httpx.Request) -> httpx.Response:
        body = {
            "vnp_ResponseId": "RRESP-1",
            "vnp_Command": "refund",
            "vnp_ResponseCode": "00",
            "vnp_Message": "Refund accepted",
            "vnp_TmnCode": TEST_TMN_CODE,
            "vnp_TxnRef": pay["provider_ref"],
            "vnp_Amount": str(order["total_vnd"] * 100),
            "vnp_BankCode": "NCB",
            "vnp_PayDate": "20261125135500",
            "vnp_TransactionNo": "14499099",
            "vnp_TransactionType": "02",
            "vnp_TransactionStatus": "05",
            "vnp_OrderInfo": "Refund",
        }
        body["vnp_SecureHash"] = _sign_response_pipe(body, TEST_HASH_SECRET)
        assert verify_refund_response_signature(body, hash_secret=TEST_HASH_SECRET)
        return httpx.Response(200, json=body)

    ref_client = VNPayHttpClient(transport=httpx.MockTransport(_refund_pending_handler))
    with get_sessionmaker()() as db:
        rreq = db.get(RefundRequest, rreq_id)
        assert rreq is not None
        adapter = VNPaySandboxAdapter(http_client=ref_client)
        txn = adapter.request_refund(db, refund_request=rreq, idempotency_key="IDEMP-VNP-REF-1")
        assert txn.status == RefundTransactionStatus.PENDING.value

        # Idempotent replay returns same txn
        txn2 = adapter.request_refund(db, refund_request=rreq, idempotency_key="IDEMP-VNP-REF-1")
        assert txn2.id == txn.id

        # Pending refund reserves full balance -> refundable_remaining_vnd == 0!
        obs = adapter.get_authoritative_observation(db, payment_id=pay["id"])
        assert obs.pending_refund_amount_vnd == order["total_vnd"]
        assert obs.refundable_remaining_vnd == 0


# --- 6. P1 Commerce Safety Fixes (Confirm, Cancel, Reconciliation, Pre-Sync Refund) ---
def test_p1_commerce_safety_fixes_and_outbox_429(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Section 5.1, 5.2, 5.3 and Section 19 HTTP 429 Retry-After."""
    cust = _guest(client, "Safety Tester")
    m_hdr = _merchant(client)

    co = client.post(
        "/api/v1/store/checkout",
        headers={**cust, "X-Idempotency-Key": "IDEMP-P1-1"},
        json={
            "recipient_name": "Safety Tester",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 2}],
        },
    )
    order_id = co.json()["order"]["id"]
    pay_id = co.json()["payment"]["id"]

    # 5.1: Merchant CANNOT manually mark unpaid order CONFIRMED -> 409
    assert (
        client.patch(
            f"/api/v1/merchant/orders/{order_id}/status",
            headers=m_hdr,
            json={"status": "CONFIRMED"},
        ).status_code
        == 409
    )
    # 5.1: Merchant CANNOT bypass cancellation workflow via generic status endpoint -> 409
    assert (
        client.patch(
            f"/api/v1/merchant/orders/{order_id}/status",
            headers=m_hdr,
            json={"status": "CANCELLED"},
        ).status_code
        == 409
    )

    # 5.3: Fully refunded before sync -> NEVER confirms, releases reservations once, cancels order
    with get_sessionmaker()() as db:
        pay = db.get(Payment, pay_id)
        assert pay is not None
        pay.status = PaymentStatus.REFUNDED.value
        pay.refunded_amount_vnd = pay.amount_vnd
        pay.is_synced_to_order = False
        db.commit()

        assert sync_confirmed_payments_to_orders(db) == 1
        ord_row = db.get(Order, order_id)
        inv = db.scalar(select(Inventory).where(Inventory.variant_id == "VAR-TECH-03-15M"))
        res = db.scalar(
            select(InventoryReservation).where(InventoryReservation.order_id == order_id)
        )
        assert ord_row is not None
        assert ord_row.status == OrderStatus.CANCELLED.value
        assert ord_row.reconciliation_status == ReconciliationStatus.RECONCILIATION_REQUIRED.value
        assert inv is not None and inv.reserved_qty == 0
        assert res is not None and res.status == ReservationStatus.RELEASED.value

    # 5.2: Evidence-based reconciliation: resolve succeeds ONLY when postconditions are met
    # Here order=CANCELLED, payment=REFUNDED, active_reservations=0 -> postconditions satisfied!
    r_ack = client.post(
        f"/api/v1/merchant/orders/{order_id}/reconciliation/acknowledge",
        headers=m_hdr,
        json={"resolution_note": "Investigating pre-sync refund"},
    )
    assert r_ack.status_code == 200
    r_ok = client.post(
        f"/api/v1/merchant/orders/{order_id}/reconciliation/resolve",
        headers=m_hdr,
        json={"resolution_note": "Verified refunded & reservations released"},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["reconciliation_status"] == ReconciliationStatus.RESOLVED.value

    # 5.2 negative: CANCELLED order with SUCCEEDED (unrefunded) payment CANNOT resolve!
    co2 = client.post(
        "/api/v1/store/checkout",
        headers={**cust, "X-Idempotency-Key": "IDEMP-P1-2"},
        json={
            "recipient_name": "Safety Tester",
            "recipient_phone": "0901112222",
            "shipping_address": "TP.HCM",
            "direct_items": [{"variant_id": "VAR-TECH-03-15M", "quantity": 1}],
        },
    )
    ord2_id = co2.json()["order"]["id"]
    pay2_id = co2.json()["payment"]["id"]
    assert client.post(f"/api/v1/store/orders/{ord2_id}/cancel", headers=cust).status_code == 200
    with get_sessionmaker()() as db:
        p2 = db.get(Payment, pay2_id)
        o2 = db.get(Order, ord2_id)
        assert p2 is not None and o2 is not None
        p2.status = PaymentStatus.SUCCEEDED.value
        p2.reconciliation_required = True
        o2.payment_status = PaymentStatus.SUCCEEDED.value
        o2.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
        db.commit()

    r_reject = client.post(
        f"/api/v1/merchant/orders/{ord2_id}/reconciliation/resolve",
        headers=m_hdr,
        json={"resolution_note": "Just trying to close with a note"},
    )
    assert r_reject.status_code == 409

    # Section 19: Outbox HTTP 429 uses bounded retry honoring Retry-After (NOT DEAD_LETTER!)
    monkeypatch.setenv("COMMERCE_FINRESOLVE_API_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("COMMERCE_FINRESOLVE_SESSION_TOKEN", "sess-tok")
    monkeypatch.setenv("COMMERCE_FINRESOLVE_SERVICE_TOKEN", "svc-tok")
    get_settings.cache_clear()

    mock_429 = MagicMock()
    mock_429.status_code = 429
    mock_429.text = "Rate limited"
    mock_429.headers = {"Retry-After": "42"}

    with patch("app.services.events.httpx.Client") as mock_cli:
        mock_cli.return_value.__enter__.return_value.post.return_value = mock_429
        with get_sessionmaker()() as db:
            assert dispatch_pending_outbox_events(db, limit=1) == 0
            row = db.scalar(
                select(EventOutbox).where(
                    EventOutbox.failure_category == OutboxFailureCategory.TRANSIENT.value
                )
            )
            assert row is not None
            assert row.status == OutboxStatus.PENDING.value
            assert row.next_attempt_at > datetime.now(UTC) + timedelta(seconds=30)


# --- 7. Restricted Public HTTPS Gateway Security ---
def test_restricted_public_gateway_security(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway fails closed when disabled; blocks internal/MOCK routes; preserves raw IPN query."""
    captured_urls: list[str] = []

    def _api_upstream(req: httpx.Request) -> httpx.Response:
        captured_urls.append(str(req.url))
        return httpx.Response(200, json={"RspCode": "00", "Message": "Confirm Success"})

    def _web_upstream(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>FinShop LIVE</html>")

    gw = create_gateway_app(
        api_transport=httpx.MockTransport(_api_upstream),
        web_transport=httpx.MockTransport(_web_upstream),
    )
    gw_client = TestClient(gw)

    # 1. Disabled by default -> 503 fail closed
    monkeypatch.setenv("COMMERCE_PUBLIC_GATEWAY_ENABLED", "false")
    get_settings.cache_clear()
    assert gw_client.get("/api/v1/payments/vnpay/ipn?vnp_TxnRef=ABC").status_code == 503

    # 2. Enabled -> allowlisted routes work, sensitive routes strictly 403
    monkeypatch.setenv("COMMERCE_PUBLIC_GATEWAY_ENABLED", "true")
    get_settings.cache_clear()

    for blocked in (
        "/docs",
        "/openapi.json",
        "/merchant",
        "/api/v1/merchant/overview",
        "/api/v1/service/metrics",
        "/api/v1/test-payments/mock/notify",
        "/api/v1/store/orders/ORD-1/mock-pay",
    ):
        assert gw_client.get(blocked).status_code == 403, f"Expected 403 for {blocked}"
        assert gw_client.post(blocked).status_code == 403, f"Expected 403 for POST {blocked}"

    # Raw IPN query string preserved verbatim!
    raw_ipn_qs = "vnp_Amount=10000000&vnp_OrderInfo=Thanh+toan+don+hang&vnp_SecureHash=abc123"
    r_ipn = gw_client.get(f"/api/v1/payments/vnpay/ipn?{raw_ipn_qs}")
    assert r_ipn.status_code == 200
    assert r_ipn.json()["RspCode"] == "00"
    assert captured_urls[-1].endswith(f"/api/v1/payments/vnpay/ipn?{raw_ipn_qs}")

    # Storefront root & ReturnURL allowed
    assert gw_client.get("/").status_code == 200
    assert gw_client.get("/payment/vnpay/return?vnp_TxnRef=FS1").status_code == 200
