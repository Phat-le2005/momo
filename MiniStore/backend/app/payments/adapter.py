"""Provider-independent PaymentAdapter contract and MockPaymentAdapter implementation.

Prepared cleanly for Prompt 11 (`VNPaySandboxAdapter`) while supporting ONLY `LOCAL_MOCK`
in Prompt 10. Never connects to real payment endpoints and never fakes VNPAY transactions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import CommerceError, ErrorCode
from app.models.entities import Order, Payment, RefundRequest, RefundTransaction
from app.models.enums import (
    PaymentStatus,
    RefundRequestStatus,
    RefundTransactionStatus,
    can_transition_payment,
)
from app.services.events import record_commerce_event


@dataclass(frozen=True)
class PaymentObservation:
    payment_id: str
    order_id: str
    merchant_id: str
    provider: str
    provider_ref: str
    status: str
    amount_vnd: int
    refunded_amount_vnd: int
    refundable_remaining_vnd: int
    is_synced_to_order: bool
    confirmed_at: datetime | None
    observed_at: datetime


class PaymentAdapter(Protocol):
    """Provider-independent payment adapter (LOCAL_MOCK now, VNPAY_SANDBOX in Prompt 11)."""

    @property
    def provider_name(self) -> str: ...

    def create_payment(
        self,
        db: Session,
        *,
        order: Order,
        correlation_id: str,
    ) -> Payment: ...

    def query_payment_status(self, db: Session, *, payment_id: str) -> PaymentObservation: ...

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
    ) -> tuple[Payment, bool]: ...

    def request_refund(
        self,
        db: Session,
        *,
        refund_request: RefundRequest,
        idempotency_key: str,
    ) -> RefundTransaction: ...

    def query_refund_status(self, db: Session, *, refund_txn_id: str) -> RefundTransaction: ...

    def get_authoritative_observation(
        self, db: Session, *, payment_id: str
    ) -> PaymentObservation: ...


class MockPaymentAdapter:
    """Authoritative LOCAL_MOCK test payment adapter.

    Enforces:
    - Merchant, order, and integer VND amount validation on every notification.
    - Idempotent duplicate notification handling via `notification_id`.
    - Validated payment state machine transitions (rejecting invalid transitions with 409).
    - Strict refund accounting (partial & full refunds; blocks over-refund & duplicate keys).
    - Never marks Order as CONFIRMED inline — payment confirmation and order sync are separate
      durable stages (`Payment.is_synced_to_order = False` until sync worker processes it!).
    """

    @property
    def provider_name(self) -> str:
        return "LOCAL_MOCK"

    def create_payment(
        self,
        db: Session,
        *,
        order: Order,
        correlation_id: str,
    ) -> Payment:
        now = datetime.now(UTC)
        pay_id = f"PAY-{uuid.uuid4().hex[:10].upper()}"
        provider_ref = f"MOCKPAY-{now.strftime('%y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
        payment = Payment(
            id=pay_id,
            order_id=order.id,
            merchant_id=order.merchant_id,
            commerce_session_id=order.commerce_session_id,
            provider=self.provider_name,
            provider_ref=provider_ref,
            correlation_id=correlation_id,
            amount_vnd=order.total_vnd,
            refunded_amount_vnd=0,
            status=PaymentStatus.INITIATED.value,
            is_synced_to_order=False,
            provider_payload_json={"mode": "LOCAL_MOCK", "created_for_order": order.order_code},
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
        """Process a test payment notification idempotently. Returns `(payment, is_duplicate)`."""
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

        payment = db.scalar(
            select(Payment).where(Payment.provider_ref == provider_ref).with_for_update()
        )
        if payment is None:
            raise CommerceError(
                ErrorCode.NOT_FOUND,
                f"Payment with provider_ref '{provider_ref}' not found",
                status_code=404,
            )

        if payment.merchant_id != merchant_id or payment.order_id != order_id:
            raise CommerceError(
                ErrorCode.FORBIDDEN,
                "NOTIFICATION_SCOPE_MISMATCH: merchant_id or order_id does not match payment",
                status_code=403,
            )
        if payment.amount_vnd != amount_vnd:
            raise CommerceError(
                ErrorCode.CONFLICT,
                (
                    f"NOTIFICATION_AMOUNT_MISMATCH: expected {payment.amount_vnd} VND, "
                    f"got {amount_vnd}"
                ),
                status_code=409,
            )

        # Idempotent duplicate notification check
        if payment.last_notification_id == notification_id:
            return payment, True
        if payment.status == norm_outcome:
            payment.last_notification_id = notification_id
            db.commit()
            return payment, True

        curr = PaymentStatus(payment.status)
        target = PaymentStatus(norm_outcome)
        if not can_transition_payment(curr, target):
            raise CommerceError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"Invalid payment transition: {curr.value} -> {target.value}",
                status_code=409,
            )

        now = datetime.now(UTC)
        payment.status = target.value
        payment.last_notification_id = notification_id
        if target == PaymentStatus.SUCCEEDED:
            payment.confirmed_at = now
        payment.provider_payload_json = {
            **(payment.provider_payload_json or {}),
            "last_notification_id": notification_id,
            "last_outcome": target.value,
            **(raw_payload or {}),
        }
        payment.updated_at = now

        # Update order.payment_status observation; Order.status sync happens in async sync stage!
        order = db.scalar(select(Order).where(Order.id == payment.order_id).with_for_update())
        if order is not None:
            order.payment_status = target.value
            order.updated_at = now
            world_epoch = order.world_epoch
        else:
            world_epoch = 1

        ev_type = (
            "PAYMENT_SUCCEEDED"
            if target == PaymentStatus.SUCCEEDED
            else "PAYMENT_FAILED"
            if target == PaymentStatus.FAILED
            else "PAYMENT_UPDATED"
        )
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
                "provider": self.provider_name,
                "provider_ref": payment.provider_ref,
                "notification_id": notification_id,
                "previous_status": curr.value,
                "new_status": target.value,
                "amount_vnd": payment.amount_vnd,
            },
        )
        db.commit()
        db.refresh(payment)
        return payment, False

    def request_refund(
        self,
        db: Session,
        *,
        refund_request: RefundRequest,
        idempotency_key: str,
    ) -> RefundTransaction:
        """Execute an authorized mock refund with strict remaining-balance accounting."""
        payment = db.scalar(
            select(Payment).where(Payment.id == refund_request.payment_id).with_for_update()
        )
        if payment is None:
            raise CommerceError(ErrorCode.NOT_FOUND, "Payment not found", status_code=404)

        if payment.status not in (
            PaymentStatus.SUCCEEDED.value,
            PaymentStatus.PARTIALLY_REFUNDED.value,
        ):
            raise CommerceError(
                ErrorCode.CONFLICT,
                f"Cannot refund payment in status '{payment.status}'",
                status_code=409,
            )

        existing = db.scalar(
            select(RefundTransaction).where(
                RefundTransaction.payment_id == payment.id,
                RefundTransaction.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return existing

        # Compute authoritative in-flight + completed refund sum under lock
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

        order = db.scalar(select(Order).where(Order.id == payment.order_id).with_for_update())
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

    def query_payment_status(self, db: Session, *, payment_id: str) -> PaymentObservation:
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
        return PaymentObservation(
            payment_id=payment.id,
            order_id=payment.order_id,
            merchant_id=payment.merchant_id,
            provider=payment.provider,
            provider_ref=payment.provider_ref,
            status=payment.status,
            amount_vnd=payment.amount_vnd,
            refunded_amount_vnd=payment.refunded_amount_vnd,
            refundable_remaining_vnd=max(0, payment.amount_vnd - payment.refunded_amount_vnd),
            is_synced_to_order=payment.is_synced_to_order,
            confirmed_at=payment.confirmed_at,
            observed_at=datetime.now(UTC),
        )
