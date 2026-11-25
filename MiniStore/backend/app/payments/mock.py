"""Authoritative LOCAL_MOCK test payment adapter."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import CommerceError, ErrorCode
from app.models.entities import Order, Payment, RefundRequest, RefundTransaction
from app.models.enums import (
    ObservationProvenance,
    PaymentProvider,
    PaymentStatus,
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
from app.services.events import record_commerce_event


class MockPaymentAdapter:
    """Authoritative LOCAL_MOCK test payment adapter (strictly isolated from VNPAY)."""

    @property
    def provider_name(self) -> str:
        return PaymentProvider.LOCAL_MOCK.value

    @property
    def capabilities(self) -> PaymentCapabilities:
        return PaymentCapabilities(
            provider=PaymentProvider.LOCAL_MOCK.value,
            supports_redirect_checkout=False,
            supports_ipn_signature_verification=True,
            supports_querydr=True,
            supports_partial_refund=True,
            supports_refund=True,
            is_mock_only=True,
        )

    def verify_provider_signature(
        self,
        *,
        payload: dict[str, Any],
        signature: str | None,
    ) -> bool:
        _ = (payload, signature)
        return True

    def create_payment(
        self,
        db: Session,
        *,
        order: Order,
        correlation_id: str,
        client_ip: str = "127.0.0.1",
        return_url: str | None = None,
    ) -> Payment:
        _ = client_ip
        ensure_no_active_payment_for_order(db, order_id=order.id)

        now = datetime.now(UTC)
        pay_id = f"PAY-{uuid.uuid4().hex[:10].upper()}"
        provider_ref = f"MOCKPAY-{now.strftime('%y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
        payment = Payment(
            id=pay_id,
            order_id=order.id,
            merchant_id=order.merchant_id,
            commerce_session_id=order.commerce_session_id,
            provider=self.provider_name,
            provider_environment=ObservationProvenance.LOCAL_MOCK.value,
            provider_ref=provider_ref,
            observation_source="LOCAL_MOCK",
            signature_verified=True,
            last_observed_at=now,
            redirect_url=None,
            correlation_id=correlation_id,
            amount_vnd=order.total_vnd,
            refunded_amount_vnd=0,
            status=PaymentStatus.INITIATED.value,
            is_synced_to_order=False,
            reconciliation_required=False,
            reconciliation_note=None,
            provider_payload_json={
                "mode": "LOCAL_MOCK",
                "created_for_order": order.order_code,
                "return_url": return_url,
            },
            created_at=now,
            updated_at=now,
        )
        db.add(payment)
        db.flush()

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
                "provider_environment": ObservationProvenance.LOCAL_MOCK.value,
                "provider_ref": provider_ref,
                "amount_vnd": payment.amount_vnd,
            },
        )
        return payment

    def process_provider_notification(
        self,
        db: Session,
        *,
        provider_ref: str,
        notification_id: str,
        merchant_id: str,
        order_id: str,
        amount_vnd: int,
        outcome: str,
        raw_payload: dict[str, Any] | None = None,
    ) -> tuple[Payment, bool]:
        norm_outcome = outcome.strip().upper()
        if norm_outcome not in {
            PaymentStatus.PENDING.value,
            PaymentStatus.SUCCEEDED.value,
            PaymentStatus.FAILED.value,
            PaymentStatus.UNKNOWN.value,
        }:
            raise CommerceError(
                ErrorCode.BAD_REQUEST,
                f"Unsupported payment outcome '{outcome}'",
                status_code=400,
            )
        return apply_authoritative_payment_observation(
            db,
            expected_provider=self.provider_name,
            provider_ref=provider_ref,
            notification_id=notification_id,
            merchant_id=merchant_id,
            order_id=order_id,
            amount_vnd=amount_vnd,
            target_status=PaymentStatus(norm_outcome),
            observation_source="LOCAL_MOCK",
            signature_verified=True,
            provider_environment=ObservationProvenance.LOCAL_MOCK.value,
            raw_payload=raw_payload,
        )

    def request_refund(
        self,
        db: Session,
        *,
        refund_request: RefundRequest,
        idempotency_key: str,
        operator_id: str = "merchant_operator",
        client_ip: str = "127.0.0.1",
    ) -> RefundTransaction:
        _ = (operator_id, client_ip)
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
                    (
                        "REFUND_IDEMPOTENCY_CONFLICT: idempotency_key already used for a "
                        "different refund request or amount"
                    ),
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

        committed_sum = (
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
        remaining = payment.amount_vnd - int(committed_sum)
        if refund_request.requested_amount_vnd > remaining:
            raise CommerceError(
                ErrorCode.CONFLICT,
                f"REFUND_EXCEEDS_REMAINING: Yêu cầu hoàn {refund_request.requested_amount_vnd:,}đ "
                f"vượt quá số dư có thể hoàn ({remaining:,}đ).",
                status_code=409,
                details={
                    "requested_amount_vnd": refund_request.requested_amount_vnd,
                    "refundable_remaining_vnd": remaining,
                },
            )

        now = datetime.now(UTC)
        txn = RefundTransaction(
            id=f"RTXN-{uuid.uuid4().hex[:10].upper()}",
            refund_request_id=refund_request.id,
            payment_id=payment.id,
            merchant_id=payment.merchant_id,
            provider=self.provider_name,
            provider_refund_ref=f"MOCKREF-{uuid.uuid4().hex[:10].upper()}",
            idempotency_key=idempotency_key,
            amount_vnd=refund_request.requested_amount_vnd,
            status=RefundTransactionStatus.SUCCEEDED.value,
            provider_payload_json={"mode": "LOCAL_MOCK", "reason": refund_request.reason},
            created_at=now,
            updated_at=now,
        )
        db.add(txn)

        payment.refunded_amount_vnd += refund_request.requested_amount_vnd
        payment.status = (
            PaymentStatus.REFUNDED.value
            if payment.refunded_amount_vnd >= payment.amount_vnd
            else PaymentStatus.PARTIALLY_REFUNDED.value
        )
        payment.updated_at = now

        refund_request.status = RefundRequestStatus.COMPLETED.value
        refund_request.updated_at = now

        world_epoch = order.world_epoch if order is not None else 1
        if order is not None:
            order.payment_status = payment.status
            order.updated_at = now

        record_commerce_event(
            db,
            merchant_id=payment.merchant_id,
            commerce_session_id=payment.commerce_session_id,
            world_epoch=world_epoch,
            event_type="REFUND_COMPLETED",
            order_id=payment.order_id,
            payment_id=payment.id,
            correlation_id=payment.correlation_id,
            payload={
                "refund_request_id": refund_request.id,
                "refund_txn_id": txn.id,
                "provider_refund_ref": txn.provider_refund_ref,
                "amount_vnd": txn.amount_vnd,
                "payment_status": payment.status,
            },
        )
        db.commit()
        db.refresh(txn)
        return txn

    def query_payment_status(
        self,
        db: Session,
        *,
        payment_id: str,
        client_ip: str = "127.0.0.1",
    ) -> PaymentObservation:
        _ = client_ip
        return self.get_authoritative_observation(db, payment_id=payment_id)

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
