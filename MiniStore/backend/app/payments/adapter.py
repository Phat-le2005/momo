"""Provider-independent PaymentAdapter contract and hardened MockPaymentAdapter.

Prepared cleanly for Prompt 11 (`VNPaySandboxAdapter`) while supporting ONLY `LOCAL_MOCK`
in Prompt 10.1. Never connects to real payment endpoints and never fakes VNPAY transactions.

Hardening Guarantees (Prompt 10.1):
- Durable `provider_notifications` inbox enforces unique `(provider, notification_id)` with
  canonical `payload_hash` conflict detection (`409 CONFLICT` on conflicting payload reuse).
- Out-of-order notifications never regress confirmed payment state.
- Late `SUCCEEDED` payment arriving after order cancellation or reservation release marks
  explicit `reconciliation_required=True` / `Order.reconciliation_status=RECONCILIATION_REQUIRED`
  and emits `PAYMENT_RECONCILIATION_REQUIRED` (never resurrects order or auto-refunds).
- Refund idempotency is checked BEFORE payment status checks so replaying a full refund key
  after payment became `REFUNDED` returns the original `RefundTransaction` cleanly.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import CommerceError, ErrorCode
from app.models.entities import (
    InventoryReservation,
    Order,
    Payment,
    ProviderNotification,
    RefundRequest,
    RefundTransaction,
)
from app.models.enums import (
    OrderStatus,
    PaymentStatus,
    ReconciliationStatus,
    RefundRequestStatus,
    RefundTransactionStatus,
    ReservationStatus,
    can_transition_payment,
)
from app.services.events import record_commerce_event


@dataclass(frozen=True)
class PaymentCapabilities:
    provider: str
    supports_redirect_checkout: bool
    supports_ipn_signature_verification: bool
    supports_querydr: bool
    supports_partial_refund: bool
    is_mock_only: bool


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
    pending_refund_amount_vnd: int
    unknown_refund_amount_vnd: int
    refundable_remaining_vnd: int
    is_synced_to_order: bool
    reconciliation_required: bool
    reconciliation_note: str | None
    confirmed_at: datetime | None
    observed_at: datetime
    authoritative_provenance: dict[str, Any]
    redirect_url: str | None = None


class PaymentAdapter(Protocol):
    """Provider-independent payment adapter (LOCAL_MOCK now, VNPAY_SANDBOX in Prompt 11)."""

    @property
    def provider_name(self) -> str: ...

    @property
    def capabilities(self) -> PaymentCapabilities: ...

    def create_payment(
        self,
        db: Session,
        *,
        order: Order,
        correlation_id: str,
        return_url: str | None = None,
    ) -> Payment: ...

    def verify_provider_signature(
        self,
        *,
        payload: dict[str, Any],
        signature: str | None,
    ) -> bool: ...

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


def compute_notification_payload_hash(
    *,
    provider: str,
    provider_ref: str,
    merchant_id: str,
    order_id: str,
    amount_vnd: int,
    outcome: str,
) -> str:
    canonical = json.dumps(
        {
            "provider": provider,
            "provider_ref": provider_ref,
            "merchant_id": merchant_id,
            "order_id": order_id,
            "amount_vnd": amount_vnd,
            "outcome": outcome.strip().upper(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MockPaymentAdapter:
    """Authoritative LOCAL_MOCK test payment adapter."""

    @property
    def provider_name(self) -> str:
        return "LOCAL_MOCK"

    @property
    def capabilities(self) -> PaymentCapabilities:
        return PaymentCapabilities(
            provider="LOCAL_MOCK",
            supports_redirect_checkout=False,
            supports_ipn_signature_verification=True,
            supports_querydr=True,
            supports_partial_refund=True,
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
        return_url: str | None = None,
    ) -> Payment:
        # Block creating a blind second payment when an existing payment is already active/unknown
        existing_active = db.scalar(
            select(Payment).where(
                Payment.order_id == order.id,
                Payment.status.in_(
                    [
                        PaymentStatus.INITIATED.value,
                        PaymentStatus.PENDING.value,
                        PaymentStatus.UNKNOWN.value,
                        PaymentStatus.SUCCEEDED.value,
                        PaymentStatus.PARTIALLY_REFUNDED.value,
                    ]
                ),
            )
        )
        if existing_active is not None:
            raise CommerceError(
                ErrorCode.CONFLICT,
                (
                    f"Cannot create duplicate payment while payment {existing_active.id} "
                    f"is in status '{existing_active.status}'"
                ),
                status_code=409,
            )

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
        """Process a payment provider notification with durable inbox deduplication.

        Lock order: Order -> Payment (consistent with sync & expiry workers).
        Returns `(payment, is_duplicate)`.
        """
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

        clean_nid = notification_id.strip()
        payload_hash = compute_notification_payload_hash(
            provider=self.provider_name,
            provider_ref=provider_ref,
            merchant_id=merchant_id,
            order_id=order_id,
            amount_vnd=amount_vnd,
            outcome=norm_outcome,
        )

        # 1. Check durable provider_notifications inbox first
        existing_notif = db.scalar(
            select(ProviderNotification).where(
                ProviderNotification.provider == self.provider_name,
                ProviderNotification.notification_id == clean_nid,
            )
        )
        if existing_notif is not None:
            if existing_notif.payload_hash != payload_hash:
                raise CommerceError(
                    ErrorCode.CONFLICT,
                    (
                        f"NOTIFICATION_PAYLOAD_CONFLICT: notification_id '{clean_nid}' "
                        "was already processed with a different payload"
                    ),
                    status_code=409,
                )
            existing_pay = db.get(Payment, existing_notif.payment_id)
            if existing_pay is None:
                raise CommerceError(ErrorCode.NOT_FOUND, "Payment not found", status_code=404)
            return existing_pay, True

        # 2. Look up payment id & order id, then lock in canonical order: Order -> Payment
        pay_lookup = db.scalar(select(Payment).where(Payment.provider_ref == provider_ref))
        if pay_lookup is None:
            raise CommerceError(
                ErrorCode.NOT_FOUND,
                f"Payment with provider_ref '{provider_ref}' not found",
                status_code=404,
            )

        order = db.scalar(select(Order).where(Order.id == pay_lookup.order_id).with_for_update())
        payment = db.scalar(select(Payment).where(Payment.id == pay_lookup.id).with_for_update())
        if payment is None:
            raise CommerceError(ErrorCode.NOT_FOUND, "Payment not found", status_code=404)

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

        now = datetime.now(UTC)
        curr = PaymentStatus(payment.status)
        target = PaymentStatus(norm_outcome)

        # Out-of-order / idempotent protection: never regress confirmed or terminal payment state
        if curr == target:
            notif = ProviderNotification(
                id=f"PNOT-{uuid.uuid4().hex[:12].upper()}",
                provider=self.provider_name,
                notification_id=clean_nid,
                payment_id=payment.id,
                order_id=payment.order_id,
                merchant_id=payment.merchant_id,
                payload_hash=payload_hash,
                outcome=target.value,
                response_json={"status": payment.status, "idempotent_same_state": True},
                received_at=now,
                processed_at=now,
            )
            try:
                with db.begin_nested():
                    db.add(notif)
                    db.flush()
            except IntegrityError:
                db.rollback()
                return payment, True
            payment.last_notification_id = clean_nid
            db.commit()
            db.refresh(payment)
            return payment, True

        if not can_transition_payment(curr, target):
            raise CommerceError(
                ErrorCode.INVALID_STATE_TRANSITION,
                f"Invalid payment transition: {curr.value} -> {target.value}",
                status_code=409,
            )

        # Insert durable inbox row inside transaction (enforces unique (provider, notification_id))
        notif = ProviderNotification(
            id=f"PNOT-{uuid.uuid4().hex[:12].upper()}",
            provider=self.provider_name,
            notification_id=clean_nid,
            payment_id=payment.id,
            order_id=payment.order_id,
            merchant_id=payment.merchant_id,
            payload_hash=payload_hash,
            outcome=target.value,
            response_json={"previous_status": curr.value, "new_status": target.value},
            received_at=now,
            processed_at=now,
        )
        try:
            with db.begin_nested():
                db.add(notif)
                db.flush()
        except IntegrityError as err:
            dup = db.scalar(
                select(ProviderNotification).where(
                    ProviderNotification.provider == self.provider_name,
                    ProviderNotification.notification_id == clean_nid,
                )
            )
            if dup is not None:
                if dup.payload_hash != payload_hash:
                    raise CommerceError(
                        ErrorCode.CONFLICT,
                        f"NOTIFICATION_PAYLOAD_CONFLICT for '{clean_nid}'",
                        status_code=409,
                    ) from err
                return payment, True
            raise

        payment.status = target.value
        payment.last_notification_id = clean_nid
        if target == PaymentStatus.SUCCEEDED:
            payment.confirmed_at = now
        payment.provider_payload_json = {
            **(payment.provider_payload_json or {}),
            "last_notification_id": clean_nid,
            "last_outcome": target.value,
            **(raw_payload or {}),
        }
        payment.updated_at = now

        world_epoch = order.world_epoch if order is not None else 1
        late_reconciliation = False
        reconciliation_reason: str | None = None

        if order is not None:
            order.payment_status = target.value
            order.updated_at = now

            if target == PaymentStatus.SUCCEEDED:
                active_res_count = int(
                    db.scalar(
                        select(func.count(InventoryReservation.id)).where(
                            InventoryReservation.order_id == order.id,
                            InventoryReservation.status == ReservationStatus.ACTIVE.value,
                        )
                    )
                    or 0
                )
                if order.status == OrderStatus.CANCELLED.value or active_res_count == 0:
                    late_reconciliation = True
                    reconciliation_reason = (
                        "Late SUCCEEDED payment arrived after order was cancelled or "
                        "inventory reservations expired/released. Order NOT resurrected; "
                        "manual reconciliation required."
                    )
                    payment.reconciliation_required = True
                    payment.reconciliation_note = reconciliation_reason
                    payment.is_synced_to_order = True
                    order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
                    order.reconciliation_reason = reconciliation_reason

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
                "notification_id": clean_nid,
                "previous_status": curr.value,
                "new_status": target.value,
                "amount_vnd": payment.amount_vnd,
                "reconciliation_required": late_reconciliation,
            },
        )
        if late_reconciliation:
            record_commerce_event(
                db,
                merchant_id=payment.merchant_id,
                commerce_session_id=payment.commerce_session_id,
                world_epoch=world_epoch,
                event_type="PAYMENT_RECONCILIATION_REQUIRED",
                order_id=payment.order_id,
                payment_id=payment.id,
                correlation_id=payment.correlation_id,
                payload={
                    "provider": self.provider_name,
                    "provider_ref": payment.provider_ref,
                    "amount_vnd": payment.amount_vnd,
                    "order_status": order.status if order else None,
                    "reason": reconciliation_reason,
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
        """Execute an authorized mock refund with idempotency-first replay & balance accounting."""
        # Canonical lock order: Order -> Payment
        order = db.scalar(
            select(Order).where(Order.id == refund_request.order_id).with_for_update()
        )
        payment = db.scalar(
            select(Payment).where(Payment.id == refund_request.payment_id).with_for_update()
        )
        if payment is None:
            raise CommerceError(ErrorCode.NOT_FOUND, "Payment not found", status_code=404)

        # 1. Check idempotency_key replay FIRST (even if payment is now REFUNDED!)
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

        # 2. Validate payment eligibility
        if payment.status not in (
            PaymentStatus.SUCCEEDED.value,
            PaymentStatus.PARTIALLY_REFUNDED.value,
        ):
            raise CommerceError(
                ErrorCode.CONFLICT,
                f"Cannot refund payment in status '{payment.status}'",
                status_code=409,
            )

        # 3. Compute authoritative in-flight + completed refund sum under lock
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

        # Note: Refund NEVER automatically restocks inventory!
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

        pending_sum = int(
            db.scalar(
                select(func.coalesce(func.sum(RefundTransaction.amount_vnd), 0)).where(
                    RefundTransaction.payment_id == payment.id,
                    RefundTransaction.status == RefundTransactionStatus.PENDING.value,
                )
            )
            or 0
        )
        unknown_sum = int(
            db.scalar(
                select(func.coalesce(func.sum(RefundTransaction.amount_vnd), 0)).where(
                    RefundTransaction.payment_id == payment.id,
                    RefundTransaction.status == RefundTransactionStatus.UNKNOWN.value,
                )
            )
            or 0
        )
        refundable = max(
            0, payment.amount_vnd - payment.refunded_amount_vnd - pending_sum - unknown_sum
        )

        return PaymentObservation(
            payment_id=payment.id,
            order_id=payment.order_id,
            merchant_id=payment.merchant_id,
            provider=payment.provider,
            provider_ref=payment.provider_ref,
            status=payment.status,
            amount_vnd=payment.amount_vnd,
            refunded_amount_vnd=payment.refunded_amount_vnd,
            pending_refund_amount_vnd=pending_sum,
            unknown_refund_amount_vnd=unknown_sum,
            refundable_remaining_vnd=refundable,
            is_synced_to_order=payment.is_synced_to_order,
            reconciliation_required=payment.reconciliation_required,
            reconciliation_note=payment.reconciliation_note,
            confirmed_at=payment.confirmed_at,
            observed_at=datetime.now(UTC),
            authoritative_provenance={
                "source": "COMMERCE_LOCAL_MOCK_LEDGER",
                "provider": payment.provider,
                "is_mock_only": True,
            },
            redirect_url=None,
        )
