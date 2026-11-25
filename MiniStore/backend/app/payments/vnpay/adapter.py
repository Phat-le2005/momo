"""VNPAY Sandbox API v2.1.0 PaymentAdapter implementation (Pay, IPN, QueryDr, Refund).

Safety & Honesty Invariants:
- Disabled by default; fails closed (`503`/`400`) when VNPAY credentials are not configured.
- Never silently falls back to `LOCAL_MOCK`.
- ReturnURL NEVER confirms payments or orders; only verified IPN or verified QueryDr updates
  authoritative payment state.
- Distinguishes `VNPAY_SANDBOX` vs `TEST_FIXTURE` provenance cleanly.
- Refund API is capability-gated (`COMMERCE_VNPAY_REFUND_ENABLED=true`) and never fabricates
  `SUCCEEDED` when VNPAY returns pending (`05`/`06`) or uncertain outcomes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import CommerceError, ErrorCode
from app.models.entities import (
    Order,
    Payment,
    PaymentQueryAttempt,
    RefundRequest,
    RefundTransaction,
)
from app.models.enums import (
    ObservationProvenance,
    PaymentProvider,
    PaymentStatus,
    ReconciliationStatus,
    RefundRequestStatus,
    RefundTransactionStatus,
)
from app.payments.adapter import (
    PaymentCapabilities,
    PaymentObservation,
    apply_authoritative_payment_observation,
    build_payment_observation,
    ensure_no_active_payment_for_order,
)
from app.payments.vnpay.client import VNPayHttpClient
from app.payments.vnpay.signing import (
    build_vnpay_payment_url,
    format_vnpay_timestamp,
    sign_querydr_request,
    sign_refund_request,
    verify_querydr_response_signature,
    verify_refund_response_signature,
    verify_vnpay_params_signature,
    vnd_to_vnp_amount,
    vnp_amount_to_vnd,
)
from app.payments.vnpay.status_mapping import (
    IPN_RSP_ALREADY_CONFIRMED,
    IPN_RSP_INVALID_AMOUNT,
    IPN_RSP_INVALID_SIGNATURE,
    IPN_RSP_ORDER_NOT_FOUND,
    IPN_RSP_SUCCESS,
    IPN_RSP_UNKNOWN_ERROR,
    map_vnpay_ipn_status,
    map_vnpay_querydr_status,
    map_vnpay_refund_response,
)
from app.services.events import record_commerce_event


def _sanitize_vnpay_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip cryptographic hash fields before storing sanitized metadata in DB."""
    return {
        k: v
        for k, v in payload.items()
        if k not in {"vnp_SecureHash", "vnp_SecureHashType", "vnp_HashSecret"}
    }


class VNPaySandboxAdapter:
    """Official VNPAY Sandbox API v2.1.0 adapter."""

    def __init__(self, *, http_client: VNPayHttpClient | None = None) -> None:
        self._http_client = http_client

    @property
    def provider_name(self) -> str:
        return PaymentProvider.VNPAY_SANDBOX.value

    @property
    def capabilities(self) -> PaymentCapabilities:
        settings = get_settings()
        return PaymentCapabilities(
            provider=PaymentProvider.VNPAY_SANDBOX.value,
            supports_redirect_checkout=settings.is_vnpay_configured,
            supports_ipn_signature_verification=True,
            supports_querydr=settings.is_vnpay_configured,
            supports_partial_refund=settings.is_vnpay_configured and settings.vnpay_refund_enabled,
            supports_refund=settings.is_vnpay_configured and settings.vnpay_refund_enabled,
            is_mock_only=False,
        )

    def _require_configured(self) -> tuple[str, str, str, str, str]:
        settings = get_settings()
        if not settings.is_vnpay_configured:
            raise CommerceError(
                ErrorCode.BAD_REQUEST,
                (
                    "VNPAY_NOT_CONFIGURED: Cổng VNPAY Sandbox chưa được kích hoạt hoặc thiếu "
                    "thông tin cấu hình merchant (COMMERCE_VNPAY_TMN_CODE / HASH_SECRET)."
                ),
                status_code=503,
            )
        return (
            settings.vnpay_tmn_code.strip(),
            settings.vnpay_hash_secret.get_secret_value().strip(),
            settings.vnpay_version.strip(),
            settings.vnpay_payment_url.strip(),
            settings.vnpay_return_url.strip(),
        )

    def _get_client(self) -> VNPayHttpClient:
        return self._http_client or VNPayHttpClient()

    def _provenance_env(self) -> str:
        if self._http_client is not None or get_settings().app_env.strip().lower() == "test":
            return ObservationProvenance.TEST_FIXTURE.value
        return ObservationProvenance.VNPAY_SANDBOX.value

    def verify_provider_signature(
        self,
        *,
        payload: dict[str, Any],
        signature: str | None = None,
    ) -> bool:
        secret = get_settings().vnpay_hash_secret.get_secret_value().strip()
        if not secret:
            return False
        params = dict(payload)
        if signature is not None:
            params["vnp_SecureHash"] = signature
        return verify_vnpay_params_signature(params, hash_secret=secret)

    def create_payment(
        self,
        db: Session,
        *,
        order: Order,
        correlation_id: str,
        client_ip: str = "127.0.0.1",
        return_url: str | None = None,
    ) -> Payment:
        """Create persisted VNPAY Payment attempt and signed VNPAY 2.1.0 redirect URL."""
        tmn_code, hash_secret, version, payment_url, default_return_url = self._require_configured()
        ensure_no_active_payment_for_order(db, order_id=order.id)

        settings = get_settings()
        now = datetime.now(UTC)
        expire_dt = now + timedelta(minutes=settings.vnpay_payment_expiry_minutes)
        create_date = format_vnpay_timestamp(now)
        expire_date = format_vnpay_timestamp(expire_dt)

        pay_id = f"PAY-{uuid.uuid4().hex[:10].upper()}"
        # Unique merchant transaction reference (<= 100 alphanumeric chars per VNPAY spec)
        txn_ref = f"FS{now.strftime('%y%m%d%H%M%S')}{uuid.uuid4().hex[:8].upper()}"
        order_info = f"Thanh toan don hang {order.order_code}"
        eff_return_url = (return_url or default_return_url).strip()
        safe_ip = (client_ip or "127.0.0.1").split(",")[0].strip()[:45] or "127.0.0.1"

        vnp_params: dict[str, Any] = {
            "vnp_Version": version,
            "vnp_Command": "pay",
            "vnp_TmnCode": tmn_code,
            "vnp_Amount": vnd_to_vnp_amount(order.total_vnd),
            "vnp_CurrCode": "VND",
            "vnp_TxnRef": txn_ref,
            "vnp_OrderInfo": order_info,
            "vnp_OrderType": "other",
            "vnp_Locale": "vn",
            "vnp_ReturnUrl": eff_return_url,
            "vnp_IpAddr": safe_ip,
            "vnp_CreateDate": create_date,
            "vnp_ExpireDate": expire_date,
        }
        redirect_url, _ = build_vnpay_payment_url(
            payment_url,
            vnp_params,
            hash_secret=hash_secret,
        )

        prov_env = self._provenance_env()
        payment = Payment(
            id=pay_id,
            order_id=order.id,
            merchant_id=order.merchant_id,
            commerce_session_id=order.commerce_session_id,
            provider=self.provider_name,
            provider_environment=prov_env,
            provider_ref=txn_ref,
            provider_txn_no=None,
            provider_txn_date=create_date,
            observation_source="INTERNAL_LEDGER",
            signature_verified=False,
            last_observed_at=now,
            redirect_url=redirect_url,
            correlation_id=correlation_id,
            amount_vnd=order.total_vnd,
            refunded_amount_vnd=0,
            status=PaymentStatus.PENDING.value,
            is_synced_to_order=False,
            reconciliation_required=False,
            reconciliation_note=None,
            provider_payload_json={
                "vnp_Version": version,
                "vnp_TmnCode": tmn_code,
                "vnp_TxnRef": txn_ref,
                "vnp_CreateDate": create_date,
                "vnp_ExpireDate": expire_date,
            },
            created_at=now,
            updated_at=now,
        )
        db.add(payment)
        db.flush()

        order.payment_status = PaymentStatus.PENDING.value
        order.payment_method = self.provider_name

        record_commerce_event(
            db,
            merchant_id=order.merchant_id,
            commerce_session_id=order.commerce_session_id,
            world_epoch=order.world_epoch,
            event_type="PAYMENT_INITIATED",
            order_id=order.id,
            payment_id=payment.id,
            correlation_id=correlation_id,
            payload={
                "provider": self.provider_name,
                "provider_environment": prov_env,
                "provider_ref": txn_ref,
                "amount_vnd": payment.amount_vnd,
                "vnp_CreateDate": create_date,
                "vnp_ExpireDate": expire_date,
            },
        )
        record_commerce_event(
            db,
            merchant_id=order.merchant_id,
            commerce_session_id=order.commerce_session_id,
            world_epoch=order.world_epoch,
            event_type="PAYMENT_REDIRECT_CREATED",
            order_id=order.id,
            payment_id=payment.id,
            correlation_id=correlation_id,
            payload={
                "provider": self.provider_name,
                "provider_ref": txn_ref,
                "amount_vnd": payment.amount_vnd,
            },
        )
        return payment

    def process_ipn(
        self,
        db: Session,
        *,
        raw_query_items: list[tuple[str, str]],
        provenance_override: str | None = None,
    ) -> dict[str, str]:
        """Process official VNPAY `GET /api/v1/payments/vnpay/ipn` callback.

        Returns official VNPAY JSON acknowledgment `{"RspCode": "...", "Message": "..."}`.
        Fails closed when VNPAY credentials are unavailable.
        """
        settings = get_settings()
        if not settings.is_vnpay_configured:
            return {"RspCode": IPN_RSP_UNKNOWN_ERROR[0], "Message": "VNPAY Sandbox not configured"}

        # 1. Reject ambiguous duplicate query parameters
        params: dict[str, str] = {}
        for k, v in raw_query_items:
            if k in params:
                return {
                    "RspCode": IPN_RSP_INVALID_SIGNATURE[0],
                    "Message": "Duplicate query parameter rejected",
                }
            params[k] = v

        # 2. Verify HMAC-SHA512 signature BEFORE touching any payment state
        hash_secret = settings.vnpay_hash_secret.get_secret_value().strip()
        if not verify_vnpay_params_signature(params, hash_secret=hash_secret):
            return {
                "RspCode": IPN_RSP_INVALID_SIGNATURE[0],
                "Message": IPN_RSP_INVALID_SIGNATURE[1],
            }

        # 3. Validate merchant TmnCode
        tmn_code = params.get("vnp_TmnCode", "").strip()
        if tmn_code != settings.vnpay_tmn_code.strip():
            return {"RspCode": IPN_RSP_INVALID_SIGNATURE[0], "Message": "Invalid TmnCode"}

        txn_ref = params.get("vnp_TxnRef", "").strip()
        if not txn_ref:
            return {"RspCode": IPN_RSP_ORDER_NOT_FOUND[0], "Message": IPN_RSP_ORDER_NOT_FOUND[1]}

        # 4. Find persisted VNPAY payment attempt
        payment = db.scalar(select(Payment).where(Payment.provider_ref == txn_ref))
        if payment is None or payment.provider != self.provider_name:
            return {"RspCode": IPN_RSP_ORDER_NOT_FOUND[0], "Message": IPN_RSP_ORDER_NOT_FOUND[1]}

        # 5. Parse & validate exact VND amount
        try:
            amount_vnd = vnp_amount_to_vnd(params.get("vnp_Amount", ""))
        except ValueError:
            return {"RspCode": IPN_RSP_INVALID_AMOUNT[0], "Message": IPN_RSP_INVALID_AMOUNT[1]}
        if amount_vnd != payment.amount_vnd:
            return {"RspCode": IPN_RSP_INVALID_AMOUNT[0], "Message": IPN_RSP_INVALID_AMOUNT[1]}

        # 6. Map VNPAY ResponseCode + TransactionStatus
        response_code = params.get("vnp_ResponseCode", "").strip()
        txn_status = params.get("vnp_TransactionStatus", "").strip()
        txn_no = params.get("vnp_TransactionNo", "").strip() or None
        pay_date = params.get("vnp_PayDate", "").strip() or payment.provider_txn_date
        mapping = map_vnpay_ipn_status(response_code=response_code, transaction_status=txn_status)

        # Deterministic notification identity bound to provider txn & status
        notif_id = f"VNPAY-IPN-{txn_ref}-{txn_no or 'NOTXN'}-{response_code}-{txn_status or 'NA'}"
        prov_env = provenance_override or self._provenance_env()

        # Check already-confirmed terminal state (VNPAY RspCode 02)
        if payment.status in (
            PaymentStatus.SUCCEEDED.value,
            PaymentStatus.PARTIALLY_REFUNDED.value,
            PaymentStatus.REFUNDED.value,
        ):
            if mapping.payment_status == PaymentStatus.SUCCEEDED:
                return {
                    "RspCode": IPN_RSP_ALREADY_CONFIRMED[0],
                    "Message": IPN_RSP_ALREADY_CONFIRMED[1],
                }
            # Out-of-order failure arriving after SUCCEEDED -> do NOT regress; return 02
            return {
                "RspCode": IPN_RSP_ALREADY_CONFIRMED[0],
                "Message": IPN_RSP_ALREADY_CONFIRMED[1],
            }

        try:
            updated_pay, is_dup = apply_authoritative_payment_observation(
                db,
                expected_provider=self.provider_name,
                provider_ref=txn_ref,
                notification_id=notif_id,
                merchant_id=payment.merchant_id,
                order_id=payment.order_id,
                amount_vnd=amount_vnd,
                target_status=mapping.payment_status,
                observation_source="VNPAY_IPN",
                signature_verified=True,
                provider_environment=prov_env,
                provider_txn_no=txn_no,
                provider_txn_date=pay_date,
                provider_response_code=response_code,
                provider_transaction_status=txn_status or None,
                force_reconciliation=mapping.reconciliation_required,
                reconciliation_note=mapping.reason,
                raw_payload=_sanitize_vnpay_payload(params),
            )
            _ = updated_pay
            if is_dup:
                return {
                    "RspCode": IPN_RSP_ALREADY_CONFIRMED[0],
                    "Message": IPN_RSP_ALREADY_CONFIRMED[1],
                }
            return {"RspCode": IPN_RSP_SUCCESS[0], "Message": IPN_RSP_SUCCESS[1]}
        except CommerceError as exc:
            if exc.code == ErrorCode.INVALID_STATE_TRANSITION:
                return {
                    "RspCode": IPN_RSP_ALREADY_CONFIRMED[0],
                    "Message": IPN_RSP_ALREADY_CONFIRMED[1],
                }
            return {"RspCode": IPN_RSP_UNKNOWN_ERROR[0], "Message": IPN_RSP_UNKNOWN_ERROR[1]}
        except Exception:  # noqa: BLE001
            db.rollback()
            return {"RspCode": IPN_RSP_UNKNOWN_ERROR[0], "Message": IPN_RSP_UNKNOWN_ERROR[1]}

    def query_payment_status(
        self,
        db: Session,
        *,
        payment_id: str,
        client_ip: str = "127.0.0.1",
    ) -> PaymentObservation:
        """Execute VNPAY 2.1.0 `querydr` API, persist durable attempt history, and reconcile."""
        tmn_code, hash_secret, version, _, _ = self._require_configured()
        payment = db.get(Payment, payment_id)
        if payment is None:
            raise CommerceError(ErrorCode.NOT_FOUND, "Payment not found", status_code=404)
        if payment.provider != self.provider_name:
            raise CommerceError(
                ErrorCode.FORBIDDEN,
                f"Cannot run VNPAY QueryDr on provider '{payment.provider}'",
                status_code=403,
            )

        now = datetime.now(UTC)
        req_id = f"QDR{now.strftime('%y%m%d%H%M%S')}{uuid.uuid4().hex[:8].upper()}"
        create_date = format_vnpay_timestamp(now)
        txn_date = payment.provider_txn_date or format_vnpay_timestamp(payment.created_at)
        order_info = f"Truy van giao dich {payment.provider_ref}"
        safe_ip = (client_ip or "127.0.0.1").split(",")[0].strip()[:45] or "127.0.0.1"

        req_payload: dict[str, Any] = {
            "vnp_RequestId": req_id,
            "vnp_Version": version,
            "vnp_Command": "querydr",
            "vnp_TmnCode": tmn_code,
            "vnp_TxnRef": payment.provider_ref,
            "vnp_OrderInfo": order_info,
            "vnp_TransactionDate": txn_date,
            "vnp_CreateDate": create_date,
            "vnp_IpAddr": safe_ip,
        }
        req_payload["vnp_SecureHash"] = sign_querydr_request(req_payload, hash_secret=hash_secret)

        prov_env = self._provenance_env()
        http_status: int | None = None
        resp_data: dict[str, Any] = {}
        err_msg: str | None = None
        sig_ok = False
        mapped_status_str: str | None = None

        try:
            http_status, resp_data = self._get_client().post_transaction(req_payload)
        except httpx.TimeoutException as exc:
            err_msg = f"VNPAY QueryDr timeout: {exc}"
        except Exception as exc:  # noqa: BLE001
            err_msg = f"VNPAY QueryDr transport error: {str(exc)[:300]}"

        resp_code = str(resp_data.get("vnp_ResponseCode", "") or "").strip() or None
        txn_status = str(resp_data.get("vnp_TransactionStatus", "") or "").strip() or None

        if err_msg is None and http_status == 200:
            sig_ok = verify_querydr_response_signature(resp_data, hash_secret=hash_secret)
            if not sig_ok:
                err_msg = "VNPAY QueryDr response signature verification FAILED"

        # Always persist durable PaymentQueryAttempt history!
        attempt = PaymentQueryAttempt(
            id=f"QATT-{uuid.uuid4().hex[:10].upper()}",
            payment_id=payment.id,
            order_id=payment.order_id,
            merchant_id=payment.merchant_id,
            provider=self.provider_name,
            request_id=req_id,
            command="querydr",
            http_status=http_status,
            provider_response_code=resp_code,
            provider_transaction_status=txn_status,
            signature_verified=sig_ok,
            mapped_payment_status=None,
            observation_provenance=prov_env,
            error_message=err_msg,
            response_json=_sanitize_vnpay_payload(resp_data),
            created_at=now,
        )
        db.add(attempt)
        db.flush()

        # Network timeout / error -> DO NOT convert into payment failure; mark UNKNOWN if needed
        if err_msg is not None or not sig_ok:
            if payment.status in (PaymentStatus.INITIATED.value, PaymentStatus.PENDING.value):
                payment.status = PaymentStatus.UNKNOWN.value
                payment.last_observed_at = now
                payment.updated_at = now
                ord_row = db.get(Order, payment.order_id)
                if ord_row is not None:
                    ord_row.payment_status = PaymentStatus.UNKNOWN.value
                    ord_row.updated_at = now
                record_commerce_event(
                    db,
                    merchant_id=payment.merchant_id,
                    commerce_session_id=payment.commerce_session_id,
                    world_epoch=ord_row.world_epoch if ord_row else 1,
                    event_type="PAYMENT_STATUS_UNKNOWN",
                    order_id=payment.order_id,
                    payment_id=payment.id,
                    correlation_id=payment.correlation_id,
                    payload={"querydr_request_id": req_id, "error": err_msg},
                )
            db.commit()
            return build_payment_observation(db, payment=payment)

        # Validate TmnCode, TxnRef, and Amount (when returned)
        if (
            str(resp_data.get("vnp_TmnCode", "")).strip() != tmn_code
            or str(resp_data.get("vnp_TxnRef", "")).strip() != payment.provider_ref
        ):
            attempt.error_message = "VNPAY QueryDr response TmnCode/TxnRef mismatch"
            db.commit()
            raise CommerceError(
                ErrorCode.CONFLICT,
                "VNPAY QueryDr response identity mismatch",
                status_code=409,
            )

        resp_amount_raw = str(resp_data.get("vnp_Amount", "") or "").strip()
        if resp_amount_raw:
            try:
                resp_amount_vnd = vnp_amount_to_vnd(resp_amount_raw)
            except ValueError as err:
                attempt.error_message = f"Invalid QueryDr vnp_Amount: {resp_amount_raw}"
                db.commit()
                raise CommerceError(ErrorCode.CONFLICT, str(err), status_code=409) from err
            if resp_amount_vnd != payment.amount_vnd:
                attempt.error_message = (
                    f"QueryDr amount mismatch: expected {payment.amount_vnd}, got {resp_amount_vnd}"
                )
                db.commit()
                raise CommerceError(
                    ErrorCode.CONFLICT,
                    attempt.error_message,
                    status_code=409,
                )

        mapping = map_vnpay_querydr_status(
            response_code=resp_code or "",
            transaction_status=txn_status,
        )
        mapped_status_str = mapping.payment_status.value
        attempt.mapped_payment_status = mapped_status_str

        ord_row = db.get(Order, payment.order_id)
        record_commerce_event(
            db,
            merchant_id=payment.merchant_id,
            commerce_session_id=payment.commerce_session_id,
            world_epoch=ord_row.world_epoch if ord_row else 1,
            event_type="PAYMENT_QUERY_COMPLETED",
            order_id=payment.order_id,
            payment_id=payment.id,
            correlation_id=payment.correlation_id,
            payload={
                "request_id": req_id,
                "vnp_ResponseCode": resp_code,
                "vnp_TransactionStatus": txn_status,
                "mapped_status": mapped_status_str,
                "signature_verified": True,
                "provenance": prov_env,
            },
        )
        db.commit()

        # Apply authoritative observation if not already in a conflicting terminal state
        if (
            payment.status
            in (
                PaymentStatus.SUCCEEDED.value,
                PaymentStatus.PARTIALLY_REFUNDED.value,
                PaymentStatus.REFUNDED.value,
            )
            and mapping.payment_status != PaymentStatus.SUCCEEDED
        ):
            # Conflicting provider observation on succeeded payment -> flag reconciliation!
            payment.reconciliation_required = True
            payment.reconciliation_note = (
                f"Conflicting QueryDr observation ({mapped_status_str}) on payment in status "
                f"{payment.status}"
            )
            if ord_row is not None:
                ord_row.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
                ord_row.reconciliation_reason = payment.reconciliation_note
            db.commit()
            return build_payment_observation(db, payment=payment)

        updated_pay, _ = apply_authoritative_payment_observation(
            db,
            expected_provider=self.provider_name,
            provider_ref=payment.provider_ref,
            notification_id=None,
            merchant_id=payment.merchant_id,
            order_id=payment.order_id,
            amount_vnd=payment.amount_vnd,
            target_status=mapping.payment_status,
            observation_source="VNPAY_QUERYDR",
            signature_verified=True,
            provider_environment=prov_env,
            provider_txn_no=str(resp_data.get("vnp_TransactionNo", "") or "").strip() or None,
            provider_txn_date=str(resp_data.get("vnp_PayDate", "") or "").strip()
            or payment.provider_txn_date,
            provider_response_code=resp_code,
            provider_transaction_status=txn_status,
            force_reconciliation=mapping.reconciliation_required,
            reconciliation_note=mapping.reason,
            raw_payload=_sanitize_vnpay_payload(resp_data),
        )
        return build_payment_observation(db, payment=updated_pay)

    def request_refund(
        self,
        db: Session,
        *,
        refund_request: RefundRequest,
        idempotency_key: str,
        operator_id: str = "merchant_operator",
        client_ip: str = "127.0.0.1",
    ) -> RefundTransaction:
        """Capability-gated VNPAY 2.1.0 Refund API with cumulative capacity reservation."""
        settings = get_settings()
        if not settings.vnpay_refund_enabled:
            raise CommerceError(
                ErrorCode.FORBIDDEN,
                (
                    "VNPAY_REFUND_DISABLED: Hoàn tiền tự động qua VNPAY Sandbox đang tắt "
                    "(COMMERCE_VNPAY_REFUND_ENABLED=false) cho đến khi tài khoản được cấp quyền."
                ),
                status_code=403,
            )
        tmn_code, hash_secret, version, _, _ = self._require_configured()

        order = db.scalar(
            select(Order).where(Order.id == refund_request.order_id).with_for_update()
        )
        payment = db.scalar(
            select(Payment).where(Payment.id == refund_request.payment_id).with_for_update()
        )
        if payment is None:
            raise CommerceError(ErrorCode.NOT_FOUND, "Payment not found", status_code=404)
        if payment.provider != self.provider_name:
            raise CommerceError(
                ErrorCode.FORBIDDEN,
                f"CROSS_PROVIDER_REFUND_BLOCKED: payment is '{payment.provider}'",
                status_code=403,
            )

        # 1. Idempotency check FIRST (before status checks or external HTTP calls!)
        existing = db.scalar(
            select(RefundTransaction).where(
                RefundTransaction.payment_id == payment.id,
                RefundTransaction.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if (
                existing.amount_vnd != refund_request.requested_amount_vnd
                or existing.refund_request_id != refund_request.id
            ):
                raise CommerceError(
                    ErrorCode.CONFLICT,
                    "REFUND_IDEMPOTENCY_CONFLICT: idempotency_key reused with different payload",
                    status_code=409,
                )
            return existing

        if payment.status not in (
            PaymentStatus.SUCCEEDED.value,
            PaymentStatus.PARTIALLY_REFUNDED.value,
        ):
            raise CommerceError(
                ErrorCode.CONFLICT,
                f"Cannot refund payment in status '{payment.status}'",
                status_code=409,
            )
        if not payment.provider_txn_date:
            raise CommerceError(
                ErrorCode.CONFLICT,
                "Missing original VNPAY provider_txn_date required for refund",
                status_code=409,
            )

        # 2. Enforce cumulative refundable capacity (reserving SUCCEEDED + PENDING + UNKNOWN)
        committed_sum = int(
            db.scalar(
                select(func.coalesce(func.sum(RefundTransaction.amount_vnd), 0)).where(
                    RefundTransaction.payment_id == payment.id,
                    RefundTransaction.status.in_(
                        [
                            RefundTransactionStatus.SUCCEEDED.value,
                            RefundTransactionStatus.PENDING.value,
                            RefundTransactionStatus.UNKNOWN.value,
                        ]
                    ),
                )
            )
            or 0
        )
        remaining = payment.amount_vnd - committed_sum
        if refund_request.requested_amount_vnd > remaining:
            raise CommerceError(
                ErrorCode.CONFLICT,
                f"REFUND_EXCEEDS_REMAINING: Requested {refund_request.requested_amount_vnd:,}đ "
                f"exceeds remaining {remaining:,}đ",
                status_code=409,
            )

        now = datetime.now(UTC)
        req_id = f"RFD{now.strftime('%y%m%d%H%M%S')}{uuid.uuid4().hex[:8].upper()}"
        create_date = format_vnpay_timestamp(now)
        txn_type = "02" if refund_request.requested_amount_vnd == payment.amount_vnd else "03"
        safe_ip = (client_ip or "127.0.0.1").split(",")[0].strip()[:45] or "127.0.0.1"
        order_info = f"Hoan tien don hang {order.order_code if order else payment.order_id}"

        # 3. Persist PENDING RefundTransaction BEFORE external call to reserve balance atomically
        txn = RefundTransaction(
            id=f"RTXN-{uuid.uuid4().hex[:10].upper()}",
            refund_request_id=refund_request.id,
            payment_id=payment.id,
            merchant_id=payment.merchant_id,
            provider=self.provider_name,
            provider_refund_ref=req_id,
            provider_request_id=req_id,
            idempotency_key=idempotency_key,
            amount_vnd=refund_request.requested_amount_vnd,
            status=RefundTransactionStatus.PENDING.value,
            provider_payload_json={"vnp_TransactionType": txn_type},
            created_at=now,
            updated_at=now,
        )
        db.add(txn)
        refund_request.status = RefundRequestStatus.APPROVED.value
        refund_request.updated_at = now
        db.commit()

        req_payload: dict[str, Any] = {
            "vnp_RequestId": req_id,
            "vnp_Version": version,
            "vnp_Command": "refund",
            "vnp_TmnCode": tmn_code,
            "vnp_TransactionType": txn_type,
            "vnp_TxnRef": payment.provider_ref,
            "vnp_Amount": vnd_to_vnp_amount(refund_request.requested_amount_vnd),
            "vnp_OrderInfo": order_info,
            "vnp_TransactionNo": payment.provider_txn_no or "0",
            "vnp_TransactionDate": payment.provider_txn_date,
            "vnp_CreateBy": operator_id[:64],
            "vnp_CreateDate": create_date,
            "vnp_IpAddr": safe_ip,
        }
        req_payload["vnp_SecureHash"] = sign_refund_request(req_payload, hash_secret=hash_secret)

        http_status: int | None = None
        resp_data: dict[str, Any] = {}
        err_msg: str | None = None
        try:
            http_status, resp_data = self._get_client().post_transaction(req_payload)
        except Exception as exc:  # noqa: BLE001
            err_msg = f"VNPAY Refund transport/timeout error: {str(exc)[:300]}"

        # 4. Finalize RefundTransaction conservatively (NEVER fabricate SUCCEEDED!)
        locked_txn = db.scalar(
            select(RefundTransaction).where(RefundTransaction.id == txn.id).with_for_update()
        )
        assert locked_txn is not None  # noqa: S101
        txn = locked_txn
        locked_pay = db.scalar(select(Payment).where(Payment.id == payment.id).with_for_update())
        assert locked_pay is not None  # noqa: S101
        payment = locked_pay
        order = db.scalar(select(Order).where(Order.id == payment.order_id).with_for_update())
        world_epoch = order.world_epoch if order is not None else 1

        now2 = datetime.now(UTC)
        if err_msg is not None or http_status != 200:
            txn.status = RefundTransactionStatus.UNKNOWN.value
            txn.provider_payload_json = {
                **(txn.provider_payload_json or {}),
                "error": err_msg or f"HTTP {http_status}",
            }
            txn.updated_at = now2
            payment.reconciliation_required = True
            payment.reconciliation_note = (
                "VNPAY Refund outcome UNKNOWN; balance reserved for review"
            )
            if order is not None:
                order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
                order.reconciliation_reason = payment.reconciliation_note
            record_commerce_event(
                db,
                merchant_id=payment.merchant_id,
                commerce_session_id=payment.commerce_session_id,
                world_epoch=world_epoch,
                event_type="REFUND_STATUS_UNKNOWN",
                order_id=payment.order_id,
                payment_id=payment.id,
                correlation_id=payment.correlation_id,
                payload={"refund_txn_id": txn.id, "error": err_msg},
            )
            db.commit()
            db.refresh(txn)
            return txn

        sig_ok = verify_refund_response_signature(resp_data, hash_secret=hash_secret)
        resp_code = str(resp_data.get("vnp_ResponseCode", "") or "").strip()
        txn_status = str(resp_data.get("vnp_TransactionStatus", "") or "").strip()
        txn.provider_response_code = resp_code or None
        txn.provider_transaction_status = txn_status or None
        txn.provider_payload_json = _sanitize_vnpay_payload(resp_data)
        txn.updated_at = now2

        if not sig_ok:
            txn.status = RefundTransactionStatus.UNKNOWN.value
            payment.reconciliation_required = True
            payment.reconciliation_note = "VNPAY Refund response signature invalid; marked UNKNOWN"
            if order is not None:
                order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
                order.reconciliation_reason = payment.reconciliation_note
            record_commerce_event(
                db,
                merchant_id=payment.merchant_id,
                commerce_session_id=payment.commerce_session_id,
                world_epoch=world_epoch,
                event_type="REFUND_STATUS_UNKNOWN",
                order_id=payment.order_id,
                payment_id=payment.id,
                correlation_id=payment.correlation_id,
                payload={"refund_txn_id": txn.id, "reason": "invalid_response_signature"},
            )
            db.commit()
            db.refresh(txn)
            return txn

        rmap = map_vnpay_refund_response(response_code=resp_code, transaction_status=txn_status)
        txn.status = rmap.refund_status.value

        if rmap.refund_status == RefundTransactionStatus.SUCCEEDED:
            payment.refunded_amount_vnd += txn.amount_vnd
            payment.status = (
                PaymentStatus.REFUNDED.value
                if payment.refunded_amount_vnd >= payment.amount_vnd
                else PaymentStatus.PARTIALLY_REFUNDED.value
            )
            payment.updated_at = now2
            refund_request.status = RefundRequestStatus.COMPLETED.value
            if order is not None:
                order.payment_status = payment.status
                order.updated_at = now2
            ev_type = "REFUND_COMPLETED"
        elif rmap.refund_status == RefundTransactionStatus.PENDING:
            ev_type = "REFUND_PENDING"
        elif rmap.refund_status == RefundTransactionStatus.FAILED:
            refund_request.status = RefundRequestStatus.FAILED.value
            ev_type = "REFUND_STATUS_UNKNOWN"
        else:
            payment.reconciliation_required = True
            payment.reconciliation_note = rmap.reason
            if order is not None:
                order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
                order.reconciliation_reason = rmap.reason
            ev_type = "REFUND_STATUS_UNKNOWN"

        record_commerce_event(
            db,
            merchant_id=payment.merchant_id,
            commerce_session_id=payment.commerce_session_id,
            world_epoch=world_epoch,
            event_type=ev_type,
            order_id=payment.order_id,
            payment_id=payment.id,
            correlation_id=payment.correlation_id,
            payload={
                "refund_request_id": refund_request.id,
                "refund_txn_id": txn.id,
                "vnp_ResponseCode": resp_code,
                "vnp_TransactionStatus": txn_status,
                "status": txn.status,
                "amount_vnd": txn.amount_vnd,
            },
        )
        db.commit()
        db.refresh(txn)
        return txn

    def query_refund_status(self, db: Session, *, refund_txn_id: str) -> RefundTransaction:
        txn = db.get(RefundTransaction, refund_txn_id)
        if txn is None:
            raise CommerceError(ErrorCode.NOT_FOUND, "RefundTransaction not found", status_code=404)
        return txn

    def get_authoritative_observation(self, db: Session, *, payment_id: str) -> PaymentObservation:
        payment = db.get(Payment, payment_id)
        if payment is None:
            raise CommerceError(ErrorCode.NOT_FOUND, "Payment not found", status_code=404)
        return build_payment_observation(db, payment=payment)
