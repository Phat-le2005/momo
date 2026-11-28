"""Provider-independent PaymentAdapter contract and shared authoritative state domain service.

Supports BOTH `LOCAL_MOCK` and `VNPAY_SANDBOX` through a single shared transaction-safe
payment observation service (`apply_authoritative_payment_observation`) so locking order,
ProviderNotification inbox deduplication, late-payment reconciliation, and event emission
are guaranteed consistent across every provider.
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
    PaymentProvider,
    PaymentStatus,
    ReconciliationStatus,
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
    supports_refund: bool
    is_mock_only: bool


@dataclass(frozen=True)
class PaymentObservation:
    payment_id: str
    order_id: str
    merchant_id: str
    provider: str
    provider_environment: str
    provider_ref: str
    provider_txn_no: str | None
    provider_txn_date: str | None
    provider_response_code: str | None
    provider_transaction_status: str | None
    observation_source: str
    signature_verified: bool
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
    last_observed_at: datetime | None
    observed_at: datetime
    authoritative_provenance: dict[str, Any]
    redirect_url: str | None = None


class PaymentAdapter(Protocol):
    """Provider-independent payment adapter (`LOCAL_MOCK` and `VNPAY_SANDBOX`)."""

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
        client_ip: str = "127.0.0.1",
        return_url: str | None = None,
    ) -> Payment: ...

    def verify_provider_signature(
        self,
        *,
        payload: dict[str, Any],
        signature: str | None,
    ) -> bool: ...

    def query_payment_status(
        self,
        db: Session,
        *,
        payment_id: str,
    ) -> PaymentObservation: ...

    def request_refund(
        self,
        db: Session,
        *,
        refund_request: RefundRequest,
        idempotency_key: str,
        operator_id: str = "merchant_operator",
        client_ip: str = "127.0.0.1",
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


def ensure_no_active_payment_for_order(db: Session, *, order_id: str) -> None:
    """Prevent creating a blind second payment while an existing payment is active/unknown."""
    existing_active = db.scalar(
        select(Payment).where(
            Payment.order_id == order_id,
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


def build_payment_observation(db: Session, *, payment: Payment) -> PaymentObservation:
    """Construct a complete `PaymentObservation` including pending/unknown refund accounting."""
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
        provider_environment=payment.provider_environment,
        provider_ref=payment.provider_ref,
        provider_txn_no=payment.provider_txn_no,
        provider_txn_date=payment.provider_txn_date,
        provider_response_code=payment.provider_response_code,
        provider_transaction_status=payment.provider_transaction_status,
        observation_source=payment.observation_source,
        signature_verified=payment.signature_verified,
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
        last_observed_at=payment.last_observed_at,
        observed_at=datetime.now(UTC),
        authoritative_provenance={
            "provider": payment.provider,
            "provider_environment": payment.provider_environment,
            "observation_source": payment.observation_source,
            "signature_verified": payment.signature_verified,
            "is_mock_only": payment.provider == "LOCAL_MOCK",
        },
        redirect_url=payment.redirect_url,
    )


def apply_authoritative_payment_observation(
    db: Session,
    *,
    expected_provider: str,
    provider_ref: str,
    notification_id: str | None,
    merchant_id: str,
    order_id: str,
    amount_vnd: int,
    target_status: PaymentStatus,
    observation_source: str,
    signature_verified: bool,
    provider_environment: str,
    provider_txn_no: str | None = None,
    provider_txn_date: str | None = None,
    provider_pay_date: str | None = None,
    provider_response_code: str | None = None,
    provider_transaction_status: str | None = None,
    force_reconciliation: bool = False,
    reconciliation_note: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    expected_querydr_claim_token: str | None = None,
) -> tuple[Payment, bool]:
    """Shared atomic domain service for applying an authoritative payment state observation.

    Enforces:
    - Cross-provider isolation (`payment.provider == expected_provider`).
    - Canonical lock ordering: `Order -> Payment`.
    - Durable `provider_notifications` dedup & payload-hash conflict check (Section 5.5).
    - Validated state machine transitions (never regressing terminal states).
    - Late-payment reconciliation when `SUCCEEDED` arrives after cancellation/expiry.
    - Atomic `CommerceEvent` + `EventOutbox` recording in the same transaction.
    """
    clean_nid = notification_id.strip() if notification_id else None
    payload_hash = (
        compute_notification_payload_hash(
            provider=expected_provider,
            provider_ref=provider_ref,
            merchant_id=merchant_id,
            order_id=order_id,
            amount_vnd=amount_vnd,
            outcome=target_status.value,
        )
        if clean_nid
        else ""
    )

    # 1. Check durable provider_notifications inbox first when notification_id is supplied
    if clean_nid:
        existing_notif = db.scalar(
            select(ProviderNotification).where(
                ProviderNotification.provider == expected_provider,
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

    # 2. Lookup payment, then lock in canonical order: Order -> Payment
    pay_lookup = db.scalar(select(Payment).where(Payment.provider_ref == provider_ref))
    if pay_lookup is None:
        raise CommerceError(
            ErrorCode.NOT_FOUND,
            f"Payment with provider_ref '{provider_ref}' not found",
            status_code=404,
        )

    order = db.scalar(
        select(Order)
        .where(Order.id == pay_lookup.order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    payment = db.scalar(
        select(Payment)
        .where(Payment.id == pay_lookup.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if payment is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Payment not found", status_code=404)
    if (
        expected_querydr_claim_token is not None
        and payment.querydr_claim_token != expected_querydr_claim_token
    ):
        raise CommerceError(
            ErrorCode.CONFLICT,
            "STALE_QUERYDR_CLAIM_TOKEN: worker lost lease ownership before applying observation",
            status_code=409,
        )

    # Strictly block cross-provider mutations!
    if payment.provider != expected_provider:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            (
                f"CROSS_PROVIDER_MUTATION_BLOCKED: payment provider is '{payment.provider}', "
                f"cannot mutate via '{expected_provider}'"
            ),
            status_code=403,
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
            (f"NOTIFICATION_AMOUNT_MISMATCH: expected {payment.amount_vnd} VND, got {amount_vnd}"),
            status_code=409,
        )

    now = datetime.now(UTC)
    curr = PaymentStatus(payment.status)
    observed_status = target_status
    if expected_provider == PaymentProvider.VNPAY_SANDBOX.value and (
        curr == PaymentStatus.UNKNOWN and target_status == PaymentStatus.PENDING
    ):
        # Pending evidence does not resolve uncertainty, but the observation must be durable.
        target_status = curr
    terminal_expected = {
        PaymentStatus.SUCCEEDED: PaymentStatus.SUCCEEDED,
        PaymentStatus.PARTIALLY_REFUNDED: PaymentStatus.SUCCEEDED,
        PaymentStatus.REFUNDED: PaymentStatus.SUCCEEDED,
        PaymentStatus.FAILED: PaymentStatus.FAILED,
    }.get(curr)
    preserve_terminal = observation_source == "VNPAY_IPN" and terminal_expected is not None
    if preserve_terminal:
        if (
            target_status != terminal_expected
            or (payment.provider_txn_no and provider_txn_no != payment.provider_txn_no)
            or (payment.provider_pay_date and provider_pay_date != payment.provider_pay_date)
        ):
            force_reconciliation = True
            reconciliation_note = "Signed VNPAY IPN conflicts with terminal payment"
        target_status = curr

    # 3. Same-state replay path (Section 5.5: MUST validate payload_hash even on IntegrityError!)
    if curr == target_status:
        if clean_nid:
            notif = ProviderNotification(
                id=f"PNOT-{uuid.uuid4().hex[:12].upper()}",
                provider=expected_provider,
                notification_id=clean_nid,
                payment_id=payment.id,
                order_id=payment.order_id,
                merchant_id=payment.merchant_id,
                payload_hash=payload_hash,
                outcome=observed_status.value,
                response_json={"status": payment.status, "idempotent_same_state": True},
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
                        ProviderNotification.provider == expected_provider,
                        ProviderNotification.notification_id == clean_nid,
                    )
                )
                if dup is not None and dup.payload_hash != payload_hash:
                    raise CommerceError(
                        ErrorCode.CONFLICT,
                        f"NOTIFICATION_PAYLOAD_CONFLICT for '{clean_nid}'",
                        status_code=409,
                    ) from err
                return payment, True
            payment.last_notification_id = clean_nid
        payment.last_observed_at = now
        if provider_txn_no and not payment.provider_txn_no:
            payment.provider_txn_no = provider_txn_no
        if signature_verified and not preserve_terminal:
            payment.signature_verified = True
            payment.observation_source = observation_source
            payment.provider_environment = provider_environment
            payment.provider_response_code = provider_response_code
            payment.provider_transaction_status = provider_transaction_status
        if force_reconciliation and not payment.reconciliation_required:
            payment.reconciliation_required = True
            payment.reconciliation_note = reconciliation_note
            if order is not None:
                order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
                order.reconciliation_reason = reconciliation_note
        db.commit()
        db.refresh(payment)
        return payment, True

    if not can_transition_payment(curr, target_status):
        raise CommerceError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Invalid payment transition: {curr.value} -> {target_status.value}",
            status_code=409,
        )

    # 4. Persist durable notification inbox row inside transaction
    if clean_nid:
        notif = ProviderNotification(
            id=f"PNOT-{uuid.uuid4().hex[:12].upper()}",
            provider=expected_provider,
            notification_id=clean_nid,
            payment_id=payment.id,
            order_id=payment.order_id,
            merchant_id=payment.merchant_id,
            payload_hash=payload_hash,
            outcome=target_status.value,
            response_json={"previous_status": curr.value, "new_status": target_status.value},
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
                    ProviderNotification.provider == expected_provider,
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

    # 5. Apply authoritative observation to Payment
    payment.status = target_status.value
    payment.provider_environment = provider_environment
    payment.observation_source = observation_source
    payment.signature_verified = signature_verified
    payment.last_observed_at = now
    if clean_nid:
        payment.last_notification_id = clean_nid
    if provider_txn_no:
        payment.provider_txn_no = provider_txn_no
    # P1.4: NEVER overwrite immutable provider_create_date with vnp_PayDate!
    if provider_pay_date:
        payment.provider_pay_date = provider_pay_date
    if provider_txn_date and not payment.provider_txn_date:
        payment.provider_txn_date = provider_txn_date
    if provider_response_code is not None:
        payment.provider_response_code = provider_response_code
    if provider_transaction_status is not None:
        payment.provider_transaction_status = provider_transaction_status
    if target_status == PaymentStatus.SUCCEEDED:
        payment.confirmed_at = now
    payment.provider_payload_json = {
        **(payment.provider_payload_json or {}),
        "last_observation_source": observation_source,
        "last_outcome": target_status.value,
        **({"last_notification_id": clean_nid} if clean_nid else {}),
        **(raw_payload or {}),
    }
    payment.updated_at = now

    world_epoch = order.world_epoch if order is not None else 1
    needs_reconciliation = force_reconciliation
    recon_reason = reconciliation_note

    if order is not None:
        order.payment_status = target_status.value
        order.updated_at = now

        if target_status == PaymentStatus.SUCCEEDED:
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
                needs_reconciliation = True
                recon_reason = (
                    "Late SUCCEEDED payment arrived after order was cancelled or "
                    "inventory reservations expired/released. Order NOT resurrected; "
                    "manual reconciliation required."
                )
                payment.is_synced_to_order = True

        if needs_reconciliation:
            payment.reconciliation_required = True
            payment.reconciliation_note = recon_reason
            order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
            order.reconciliation_reason = recon_reason

    ev_type = (
        "PAYMENT_SUCCEEDED"
        if target_status == PaymentStatus.SUCCEEDED
        else "PAYMENT_FAILED"
        if target_status == PaymentStatus.FAILED
        else "PAYMENT_STATUS_UNKNOWN"
        if target_status == PaymentStatus.UNKNOWN
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
            "provider": expected_provider,
            "provider_environment": provider_environment,
            "provider_ref": payment.provider_ref,
            "provider_txn_no": payment.provider_txn_no,
            "observation_source": observation_source,
            "signature_verified": signature_verified,
            "previous_status": curr.value,
            "new_status": target_status.value,
            "amount_vnd": payment.amount_vnd,
            "reconciliation_required": needs_reconciliation,
        },
        provenance={
            "provider": expected_provider,
            "provider_environment": provider_environment,
            "observation_source": observation_source,
            "signature_verified": signature_verified,
        },
    )
    if needs_reconciliation:
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
                "provider": expected_provider,
                "provider_ref": payment.provider_ref,
                "amount_vnd": payment.amount_vnd,
                "order_status": order.status if order else None,
                "reason": recon_reason,
            },
        )

    db.commit()
    db.refresh(payment)
    return payment, False


# Backwards-compatible explicit re-export of MockPaymentAdapter
from app.payments.mock import MockPaymentAdapter as MockPaymentAdapter  # noqa: E402, PLC0414
