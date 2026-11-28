"""Regression checks for intentionally degraded VNPAY Sandbox reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_prompt11_1_payment_safety import (
    QUERYDR_RESP_FIELDS,
    TEST_HASH_SECRET,
    TEST_TMN_CODE,
    _create_vnpay_order,
    _enable_vnpay,
    _mock_client,
    _sign_pipe,
)

from app.core.config import get_settings
from app.core.errors import CommerceError
from app.db.session import get_sessionmaker
from app.models.entities import InventoryReservation, Order, Payment, PaymentQueryAttempt
from app.models.enums import OrderStatus, PaymentStatus, ReconciliationStatus, ReservationStatus
from app.payments.vnpay.adapter import VNPaySandboxAdapter
from app.payments.vnpay.signing import sign_vnpay_params, vnd_to_vnp_amount
from app.services.checkout import cancel_order
from app.services.sync import expire_stale_reservations, reconcile_pending_vnpay_payments


def _signed_ipn(
    txn_ref: str, amount_vnd: int, *, response: str = "00", status: str = "00"
) -> dict[str, str]:
    params = {
        "vnp_TmnCode": TEST_TMN_CODE,
        "vnp_TxnRef": txn_ref,
        "vnp_Amount": vnd_to_vnp_amount(amount_vnd),
        "vnp_TransactionNo": "1234567",
        "vnp_PayDate": "20261125120500",
        "vnp_ResponseCode": response,
        "vnp_TransactionStatus": status,
    }
    return {**params, "vnp_SecureHash": sign_vnpay_params(params, hash_secret=TEST_HASH_SECRET)}


def test_querydr_disabled_leaves_checkout_and_signed_ipn_available(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_vnpay(monkeypatch)
    monkeypatch.setenv("COMMERCE_VNPAY_QUERYDR_ENABLED", "false")
    get_settings.cache_clear()
    _, payment_id, txn_ref, amount_vnd = _create_vnpay_order(client)

    def unexpected_http(_request: httpx.Request) -> httpx.Response:
        pytest.fail("Disabled QueryDR attempted provider HTTP")

    transport = _mock_client(unexpected_http)
    adapter = VNPaySandboxAdapter(http_client=transport)
    assert adapter.capabilities.supports_redirect_checkout
    assert not adapter.capabilities.supports_querydr
    with get_sessionmaker()() as db:
        assert reconcile_pending_vnpay_payments(db, http_client=transport) == 0
        with pytest.raises(CommerceError, match="VNPAY_QUERYDR_UNAVAILABLE") as error:
            adapter.query_payment_status(db, payment_id=payment_id)
        assert error.value.status_code == 503
        assert db.get(Payment, payment_id).querydr_claim_token is None
        assert (
            db.scalar(
                select(PaymentQueryAttempt).where(PaymentQueryAttempt.payment_id == payment_id)
            )
            is None
        )

    response = client.get("/api/v1/payments/vnpay/ipn", params=_signed_ipn(txn_ref, amount_vnd))
    assert response.json()["RspCode"] == "00"
    with get_sessionmaker()() as db:
        assert db.get(Payment, payment_id).status == PaymentStatus.SUCCEEDED.value


def test_expired_vnpay_without_ipn_requires_manual_reconciliation_and_never_resurrects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_vnpay(monkeypatch)
    monkeypatch.setenv("COMMERCE_VNPAY_QUERYDR_ENABLED", "false")
    get_settings.cache_clear()
    order_id, payment_id, txn_ref, amount_vnd = _create_vnpay_order(client)
    with get_sessionmaker()() as db:
        reservation = db.scalar(
            select(InventoryReservation).where(InventoryReservation.order_id == order_id)
        )
        assert reservation is not None
        reservation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        assert expire_stale_reservations(db) == 1
        order = db.get(Order, order_id)
        payment = db.get(Payment, payment_id)
        assert order is not None and payment is not None
        assert order.status == OrderStatus.CANCELLED.value
        assert order.reconciliation_status == ReconciliationStatus.RECONCILIATION_REQUIRED.value
        assert payment.status == PaymentStatus.PENDING.value
        assert payment.reconciliation_required and not payment.signature_verified
        assert reservation.status == ReservationStatus.EXPIRED.value

    response = client.get("/api/v1/payments/vnpay/ipn", params=_signed_ipn(txn_ref, amount_vnd))
    assert response.json()["RspCode"] == "00"
    with get_sessionmaker()() as db:
        order = db.get(Order, order_id)
        payment = db.get(Payment, payment_id)
        reservation = db.scalar(
            select(InventoryReservation).where(InventoryReservation.order_id == order_id)
        )
        assert order is not None and payment is not None and reservation is not None
        assert order.status == OrderStatus.CANCELLED.value
        assert payment.status == PaymentStatus.SUCCEEDED.value
        assert payment.reconciliation_required and payment.signature_verified
        assert reservation.status == ReservationStatus.EXPIRED.value


def test_incomplete_signed_querydr_does_not_reopen_failed_payment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_vnpay(monkeypatch)
    order_id, payment_id, txn_ref, amount_vnd = _create_vnpay_order(client)
    callback = client.get(
        "/api/v1/payments/vnpay/ipn",
        params=_signed_ipn(txn_ref, amount_vnd, response="24", status="02"),
    )
    assert callback.json()["RspCode"] == "00"
    with get_sessionmaker()() as db:
        order = db.get(Order, order_id)
        assert order is not None
        cancel_order(db, order=order, actor="TEST_FIXTURE")

    def signed_incomplete(_request: httpx.Request) -> httpx.Response:
        payload = {
            "vnp_TmnCode": TEST_TMN_CODE,
            "vnp_TxnRef": txn_ref,
            "vnp_Command": "querydr",
            "vnp_ResponseCode": "00",
            "vnp_TransactionStatus": "00",
            "vnp_TransactionType": "01",
            "vnp_TransactionNo": "1234567",
            "vnp_Amount": vnd_to_vnp_amount(amount_vnd),
        }
        payload["vnp_SecureHash"] = _sign_pipe(payload, QUERYDR_RESP_FIELDS)
        return httpx.Response(200, json=payload)

    adapter = VNPaySandboxAdapter(http_client=_mock_client(signed_incomplete))
    with get_sessionmaker()() as db:
        adapter.query_payment_status(db, payment_id=payment_id)
        payment = db.get(Payment, payment_id)
        order = db.get(Order, order_id)
        attempt = db.scalar(
            select(PaymentQueryAttempt).where(PaymentQueryAttempt.payment_id == payment_id)
        )
        assert payment is not None and order is not None and attempt is not None
        assert payment.status == order.payment_status == PaymentStatus.FAILED.value
        assert payment.observation_source == "VNPAY_IPN"
        assert order.status == OrderStatus.CANCELLED.value
        assert payment.reconciliation_required
        assert attempt.error_message and "vnp_ResponseId missing" in attempt.error_message


def test_malformed_querydr_codes_consume_retry_budget(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_vnpay(monkeypatch)
    monkeypatch.setenv("COMMERCE_VNPAY_QUERYDR_MAX_ATTEMPTS", "1")
    get_settings.cache_clear()
    _, payment_id, _, _ = _create_vnpay_order(client)
    calls = 0

    def malformed(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"vnp_ResponseCode": "9" * 17, "vnp_TransactionStatus": "0" * 17},
        )

    transport = _mock_client(malformed)
    with get_sessionmaker()() as db:
        assert reconcile_pending_vnpay_payments(db, http_client=transport) == 1
        attempt = db.scalar(
            select(PaymentQueryAttempt).where(PaymentQueryAttempt.payment_id == payment_id)
        )
        payment = db.get(Payment, payment_id)
        assert payment is not None and attempt is not None
        assert payment.querydr_exhausted and payment.reconciliation_required
        assert payment.status == PaymentStatus.UNKNOWN.value
        assert attempt.error_message == "VNPAY QueryDR response status fields malformed"
        assert (
            attempt.provider_response_code is None and attempt.provider_transaction_status is None
        )
        assert not attempt.signature_verified
        assert reconcile_pending_vnpay_payments(db, http_client=transport) == 0
    assert calls == 1
