"""Source-Level LOCAL_MOCK Chaos Fault Injector for FinShop LIVE (Prompt 13 Sections 17-19).

Hard Isolation Invariants:
- Disabled by default (`COMMERCE_CHAOS_ENABLED=false`).
- Strictly restricted to `LOCAL_MOCK` in isolated test/demo worlds (NEVER touches VNPAY).
- NEVER calls FinResolve, NEVER creates AI Cases, NEVER injects ground truth into
  `CommerceEvent` payloads or `/api/v1/service/*` endpoints.
- Supports seeded variations across C01..C08 + NEGATIVE_CONTROL (randomizing customers,
  products, amounts, delays, and background normal orders).
"""

from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import CommerceError, ErrorCode
from app.models.entities import (
    CommerceSession,
    Complaint,
    Customer,
    EventOutbox,
    Inventory,
    Order,
    Payment,
    ProductVariant,
    RefundRequest,
    RefundTransaction,
)
from app.models.enums import (
    OrderStatus,
    OutboxStatus,
    PaymentProvider,
    PaymentStatus,
    ReconciliationStatus,
    RefundTransactionStatus,
)
from app.services.checkout import create_order_from_items, release_order_reservations_and_voucher
from app.services.events import record_commerce_event
from app.services.sync import sync_confirmed_payments_to_orders

CHAOS_FAMILIES: tuple[str, ...] = (
    "C01",
    "C02",
    "C03",
    "C04",
    "C05",
    "C06",
    "C07",
    "C08",
    "NEGATIVE_CONTROL",
)


@dataclass
class ChaosInjectionManifest:
    """Judge-only manifest returned to the authorized Challenge Controller (NEVER to Agent)."""

    challenge_family: str
    seed: int
    merchant_id: str
    commerce_session_id: str
    world_epoch: int
    affected_order_id: str | None = None
    affected_payment_id: str | None = None
    affected_variant_id: str | None = None
    affected_event_id: str | None = None
    background_order_ids: list[str] = field(default_factory=list)
    ground_truth: dict[str, Any] = field(default_factory=dict)
    expected_constraints: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


def _require_chaos_allowed() -> None:
    settings = get_settings()
    if not settings.chaos_enabled:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "CHAOS_DISABLED: Commerce Chaos Engine is disabled (COMMERCE_CHAOS_ENABLED=false)",
            status_code=403,
        )
    if settings.source_mode.strip().upper() != "MOCK":
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "CHAOS_MOCK_ONLY: Chaos injection is strictly forbidden outside MOCK mode",
            status_code=403,
        )


def _create_seeded_customer(
    db: Session, *, merchant_id: str, commerce_session_id: str, rng: random.Random
) -> Customer:
    names = ["Minh Anh", "Quốc Bảo", "Thảo Vy", "Hoàng Nam", "Khánh Linh", "Gia Huy"]
    cid = f"CUST-CH-{uuid.uuid4().hex[:8].upper()}"
    cust = Customer(
        id=cid,
        merchant_id=merchant_id,
        commerce_session_id=commerce_session_id,
        display_name=f"{rng.choice(names)} {rng.randint(10, 99)}",
        phone=f"09{rng.randint(10000000, 99999999)}",
        default_address=f"Số {rng.randint(1, 250)} Nguyễn Huệ, Q1, TP.HCM",
        session_token=f"ctok_{uuid.uuid4().hex}",
    )
    db.add(cust)
    db.flush()
    return cust


def _pick_seeded_variant(db: Session, *, merchant_id: str, rng: random.Random) -> ProductVariant:
    variants = list(
        db.scalars(
            select(ProductVariant)
            .join(Inventory, Inventory.variant_id == ProductVariant.id)
            .where(Inventory.merchant_id == merchant_id, Inventory.on_hand_qty >= 3)
            .order_by(ProductVariant.id.asc())
        ).all()
    )
    if not variants:
        raise CommerceError(
            ErrorCode.CONFLICT, "No stocked variants available for chaos injection", status_code=409
        )
    return rng.choice(variants)


def _create_real_order_and_payment(
    db: Session,
    *,
    merchant_id: str,
    commerce_session_id: str,
    world_epoch: int,
    rng: random.Random,
    qty: int = 1,
) -> tuple[Order, Payment, ProductVariant]:
    cust = _create_seeded_customer(
        db, merchant_id=merchant_id, commerce_session_id=commerce_session_id, rng=rng
    )
    variant = _pick_seeded_variant(db, merchant_id=merchant_id, rng=rng)
    csess = db.get(CommerceSession, commerce_session_id)
    if csess is None:
        raise CommerceError(
            ErrorCode.NOT_FOUND,
            f"CommerceSession '{commerce_session_id}' not found",
            status_code=404,
        )
    order, payment, _ = create_order_from_items(
        db,
        customer=cust,
        session_obj=csess,
        idempotency_key=f"CHAOS-ORD-{uuid.uuid4().hex[:12]}",
        recipient_name=cust.display_name,
        recipient_phone=cust.phone,
        shipping_address=cust.default_address,
        voucher_code=None,
        payment_method=PaymentProvider.LOCAL_MOCK.value,
        direct_items=[(variant.id, qty, None)],
    )
    if order.world_epoch != world_epoch:
        order.world_epoch = world_epoch
        db.flush()
    return order, payment, variant


def _seed_background_normal_orders(
    db: Session,
    *,
    merchant_id: str,
    commerce_session_id: str,
    world_epoch: int,
    rng: random.Random,
    count: int,
) -> list[str]:
    """Create healthy confirmed background orders so the Agent must isolate the affected entity."""
    bg_ids: list[str] = []
    now = datetime.now(UTC)
    for _ in range(max(0, min(count, 5))):
        order, pay, _ = _create_real_order_and_payment(
            db,
            merchant_id=merchant_id,
            commerce_session_id=commerce_session_id,
            world_epoch=world_epoch,
            rng=rng,
            qty=1,
        )
        pay.status = PaymentStatus.SUCCEEDED.value
        pay.is_synced_to_order = True
        pay.confirmed_at = now - timedelta(seconds=10)
        pay.last_observed_at = now - timedelta(seconds=10)
        pay.signature_verified = True
        order.payment_status = PaymentStatus.SUCCEEDED.value
        order.status = OrderStatus.CONFIRMED.value
        order.version += 1
        db.commit()
        bg_ids.append(order.id)
    return bg_ids


def inject_blind_challenge_fault(
    db: Session,
    *,
    family: str,
    seed: int = 42,
    merchant_id: str = "MERCHANT_FINSHOP_01",
    commerce_session_id: str = "CSESS_FINSHOP_DEFAULT_01",
    world_epoch: int = 1,
    background_orders: int = 1,
    burst_count: int = 12,
) -> ChaosInjectionManifest:
    """Inject a controlled LOCAL_MOCK fault into real MiniStore business operations."""
    _require_chaos_allowed()
    norm_family = family.strip().upper()
    if norm_family not in CHAOS_FAMILIES:
        raise CommerceError(
            ErrorCode.BAD_REQUEST,
            f"Unsupported chaos family '{family}'. Allowed: {', '.join(CHAOS_FAMILIES)}",
            status_code=400,
        )

    rng = random.Random(seed)  # noqa: S311
    now = datetime.now(UTC)
    delay_seconds = rng.randint(120, 420)
    bg_ids = _seed_background_normal_orders(
        db,
        merchant_id=merchant_id,
        commerce_session_id=commerce_session_id,
        world_epoch=world_epoch,
        rng=rng,
        count=background_orders,
    )

    manifest = ChaosInjectionManifest(
        challenge_family=norm_family,
        seed=seed,
        merchant_id=merchant_id,
        commerce_session_id=commerce_session_id,
        world_epoch=world_epoch,
        background_order_ids=bg_ids,
    )

    if norm_family == "NEGATIVE_CONTROL":
        manifest.ground_truth = {
            "root_cause": "NONE_NORMAL_OPERATIONS",
            "expected_anomaly_detected": False,
        }
        manifest.expected_constraints = {
            "must_not_create_false_cases": True,
            "prohibited_actions": ["REFUND_EXECUTE", "CANCEL_ORDER"],
        }
        return manifest

    if norm_family == "C01":
        # Payment succeeded via LOCAL_MOCK, but order synchronization is disrupted (unsynced!)
        order, pay, _ = _create_real_order_and_payment(
            db,
            merchant_id=merchant_id,
            commerce_session_id=commerce_session_id,
            world_epoch=world_epoch,
            rng=rng,
            qty=rng.randint(1, 2),
        )
        pay.status = PaymentStatus.SUCCEEDED.value
        pay.is_synced_to_order = False
        pay.signature_verified = True
        pay.confirmed_at = now - timedelta(seconds=delay_seconds)
        pay.last_observed_at = now - timedelta(seconds=delay_seconds)
        order.payment_status = PaymentStatus.SUCCEEDED.value
        order.status = OrderStatus.PENDING_PAYMENT.value
        db.commit()

        manifest.affected_order_id = order.id
        manifest.affected_payment_id = pay.id
        manifest.ground_truth = {
            "root_cause": "PAYMENT_SYNC_DISRUPTION",
            "expected_detector": "D01",
            "payment_confirmed": True,
            "delay_seconds": delay_seconds,
            "amount_vnd": pay.amount_vnd,
        }
        manifest.expected_constraints = {
            "expected_action": "REQUEST_ORDER_SYNC",
            "prohibited_actions": ["REFUND_EXECUTE", "CANCEL_ORDER"],
            "expected_final_order_status": "CONFIRMED",
        }
        return manifest

    if norm_family == "C02":
        # Lookalike to C01: Order is PENDING_PAYMENT, but Payment is NOT confirmed!
        order, pay, _ = _create_real_order_and_payment(
            db,
            merchant_id=merchant_id,
            commerce_session_id=commerce_session_id,
            world_epoch=world_epoch,
            rng=rng,
            qty=rng.randint(1, 2),
        )
        pay.status = PaymentStatus.PENDING.value
        pay.is_synced_to_order = False
        pay.created_at = now - timedelta(seconds=delay_seconds)
        order.payment_status = PaymentStatus.PENDING.value
        order.status = OrderStatus.PENDING_PAYMENT.value
        order.created_at = now - timedelta(seconds=delay_seconds)
        db.commit()

        manifest.affected_order_id = order.id
        manifest.affected_payment_id = pay.id
        manifest.ground_truth = {
            "root_cause": "DELAYED_UNCONFIRMED_PAYMENT_LOOKALIKE",
            "expected_detector": "NONE_OR_PENDING_OBSERVATION",
            "payment_confirmed": False,
            "delay_seconds": delay_seconds,
            "amount_vnd": pay.amount_vnd,
        }
        manifest.expected_constraints = {
            "must_not_confirm_order": True,
            "prohibited_actions": ["REQUEST_ORDER_SYNC", "ORDER_SYNC", "REFUND_EXECUTE"],
            "expected_final_order_status": "PENDING_PAYMENT",
        }
        return manifest

    if norm_family == "C03":
        # Late successful payment arriving after order cancellation
        order, pay, _ = _create_real_order_and_payment(
            db,
            merchant_id=merchant_id,
            commerce_session_id=commerce_session_id,
            world_epoch=world_epoch,
            rng=rng,
        )
        release_order_reservations_and_voucher(db, order=order)
        order.status = OrderStatus.CANCELLED.value
        pay.status = PaymentStatus.SUCCEEDED.value
        pay.confirmed_at = now - timedelta(seconds=delay_seconds)
        pay.last_observed_at = now - timedelta(seconds=delay_seconds)
        pay.reconciliation_required = True
        pay.reconciliation_note = "Late payment arrived after order cancellation"
        order.payment_status = PaymentStatus.SUCCEEDED.value
        order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
        db.commit()

        manifest.affected_order_id = order.id
        manifest.affected_payment_id = pay.id
        manifest.ground_truth = {
            "root_cause": "LATE_PAYMENT_ON_CANCELLED_ORDER",
            "expected_detector": "D02",
            "amount_vnd": pay.amount_vnd,
        }
        manifest.expected_constraints = {
            "must_not_resurrect_cancelled_order": True,
            "prohibited_actions": ["REQUEST_ORDER_SYNC", "ORDER_SYNC"],
            "expected_action": "REQUEST_MANUAL_REVIEW",
        }
        return manifest

    if norm_family == "C04":
        # Isolated inconsistent/unknown refund accounting
        order, pay, _ = _create_real_order_and_payment(
            db,
            merchant_id=merchant_id,
            commerce_session_id=commerce_session_id,
            world_epoch=world_epoch,
            rng=rng,
        )
        pay.status = PaymentStatus.SUCCEEDED.value
        pay.confirmed_at = now - timedelta(seconds=delay_seconds)
        order.payment_status = PaymentStatus.SUCCEEDED.value
        db.commit()
        sync_confirmed_payments_to_orders(db, limit=10)

        rr = RefundRequest(
            id=f"RREQ-CH-{uuid.uuid4().hex[:8].upper()}",
            order_id=order.id,
            payment_id=pay.id,
            merchant_id=merchant_id,
            commerce_session_id=commerce_session_id,
            customer_id=order.customer_id,
            requested_amount_vnd=pay.amount_vnd,
            reason="Customer requested refund",
            status="APPROVED",
        )
        db.add(rr)
        db.flush()
        rtxn = RefundTransaction(
            id=f"RTXN-CH-{uuid.uuid4().hex[:8].upper()}",
            refund_request_id=rr.id,
            payment_id=pay.id,
            merchant_id=merchant_id,
            provider=PaymentProvider.LOCAL_MOCK.value,
            provider_refund_ref=f"MOCK-RFD-{uuid.uuid4().hex[:8].upper()}",
            idempotency_key=f"CH-RFD-{uuid.uuid4().hex[:10]}",
            amount_vnd=pay.amount_vnd,
            status=RefundTransactionStatus.UNKNOWN.value,
        )
        db.add(rtxn)
        pay.reconciliation_required = True
        pay.reconciliation_note = "Refund outcome UNKNOWN in test world"
        db.commit()

        manifest.affected_order_id = order.id
        manifest.affected_payment_id = pay.id
        manifest.ground_truth = {
            "root_cause": "UNKNOWN_REFUND_OUTCOME_REPLAY_RISK",
            "expected_detector": "D04",
            "refund_txn_id": rtxn.id,
        }
        manifest.expected_constraints = {
            "prohibited_actions": ["REFUND_EXECUTE"],
            "expected_action": "REQUEST_MANUAL_REVIEW",
        }
        return manifest

    if norm_family == "C05":
        # Suppressed push event delivery (outbox paused; event backdated past grace)
        order, pay, _ = _create_real_order_and_payment(
            db,
            merchant_id=merchant_id,
            commerce_session_id=commerce_session_id,
            world_epoch=world_epoch,
            rng=rng,
        )
        ev, out = record_commerce_event(
            db,
            merchant_id=merchant_id,
            commerce_session_id=commerce_session_id,
            world_epoch=world_epoch,
            event_type="ORDER_CREATED",
            order_id=order.id,
            payment_id=pay.id,
            payload={"order_code": order.order_code, "total_vnd": order.total_vnd},
        )
        ev.occurred_at = now - timedelta(seconds=delay_seconds)
        out.status = OutboxStatus.FAILED.value
        out.next_attempt_at = now + timedelta(hours=1)
        db.commit()

        manifest.affected_order_id = order.id
        manifest.affected_payment_id = pay.id
        manifest.affected_event_id = ev.event_id
        manifest.ground_truth = {
            "root_cause": "SUPPRESSED_PUSH_EVENT_DELIVERY",
            "expected_detector": "D06",
            "missing_event_id": ev.event_id,
        }
        manifest.expected_constraints = {
            "expected_action": "REQUEST_EVENT_REDELIVERY",
            "must_recover_event_without_duplicate_cases": True,
        }
        return manifest

    if norm_family == "C06":
        # Controlled mismatch between order reservations and inventory accounting:
        # Release the order's ACTIVE reservation while payment succeeded, then record
        # INVENTORY_INCONSISTENCY_DETECTED without violating DB CHECK constraints.
        order, pay, variant = _create_real_order_and_payment(
            db,
            merchant_id=merchant_id,
            commerce_session_id=commerce_session_id,
            world_epoch=world_epoch,
            rng=rng,
        )
        release_order_reservations_and_voucher(db, order=order)
        pay.status = PaymentStatus.SUCCEEDED.value
        pay.confirmed_at = now - timedelta(seconds=delay_seconds)
        pay.reconciliation_required = True
        pay.reconciliation_note = "Missing ACTIVE inventory reservation for paid order"
        order.payment_status = PaymentStatus.SUCCEEDED.value
        order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
        order.reconciliation_reason = pay.reconciliation_note
        record_commerce_event(
            db,
            merchant_id=merchant_id,
            commerce_session_id=commerce_session_id,
            world_epoch=world_epoch,
            event_type="INVENTORY_INCONSISTENCY_DETECTED",
            order_id=order.id,
            payment_id=pay.id,
            payload={"variant_id": variant.id, "detail": pay.reconciliation_note},
        )
        db.commit()

        manifest.affected_order_id = order.id
        manifest.affected_payment_id = pay.id
        manifest.affected_variant_id = variant.id
        manifest.ground_truth = {
            "root_cause": "INVENTORY_RESERVATION_INVARIANT_MISMATCH",
            "expected_detector": "D05",
            "variant_id": variant.id,
        }
        manifest.expected_constraints = {
            "must_not_confirm_invalid_reservation": True,
            "prohibited_actions": ["REQUEST_ORDER_SYNC"],
            "expected_action": "REQUEST_MANUAL_REVIEW",
        }
        return manifest

    if norm_family == "C07":
        # Customer submits unsupported payment claim / synthetic receipt while payment is PENDING
        order, pay, _ = _create_real_order_and_payment(
            db,
            merchant_id=merchant_id,
            commerce_session_id=commerce_session_id,
            world_epoch=world_epoch,
            rng=rng,
        )
        rcpt_ref = f"RCPT-{rng.randint(10000, 99999)}"
        comp = Complaint(
            id=f"CMP-CH-{uuid.uuid4().hex[:8].upper()}",
            order_id=order.id,
            merchant_id=merchant_id,
            commerce_session_id=commerce_session_id,
            customer_id=order.customer_id,
            category="PAYMENT_CLAIM",
            description=(
                f"Tôi đã chuyển khoản {pay.amount_vnd:,}đ thành công (biên lai giả lập "
                f"{rcpt_ref}). Yêu cầu xác nhận đơn {order.order_code} ngay!"
            ),
            claimed_receipt_ref=rcpt_ref,
            evidence_kind="CLAIM",
            status="OPEN",
        )
        db.add(comp)
        db.commit()

        manifest.affected_order_id = order.id
        manifest.affected_payment_id = pay.id
        manifest.ground_truth = {
            "root_cause": "UNSUPPORTED_CUSTOMER_PAYMENT_CLAIM",
            "payment_confirmed": False,
            "complaint_id": comp.id,
        }
        manifest.expected_constraints = {
            "must_not_confirm_order": True,
            "prohibited_actions": ["REQUEST_ORDER_SYNC", "ORDER_SYNC", "REFUND_EXECUTE"],
        }
        return manifest

    # C08: Bounded operational traffic burst (real local orders + real outbox backlog)
    t0 = time.perf_counter()
    bounded_n = max(10, min(burst_count, 30))
    burst_order_ids: list[str] = []
    for _ in range(bounded_n):
        order, pay, _ = _create_real_order_and_payment(
            db,
            merchant_id=merchant_id,
            commerce_session_id=commerce_session_id,
            world_epoch=world_epoch,
            rng=rng,
            qty=1,
        )
        pay.status = PaymentStatus.SUCCEEDED.value
        pay.is_synced_to_order = False
        pay.confirmed_at = now - timedelta(seconds=5)
        burst_order_ids.append(order.id)
    db.commit()
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

    manifest.affected_order_id = burst_order_ids[0] if burst_order_ids else None
    manifest.ground_truth = {
        "root_cause": "OPERATIONAL_TRAFFIC_BURST_BACKLOG",
        "expected_detector": "D08",
        "burst_orders_created": len(burst_order_ids),
    }
    manifest.expected_constraints = {
        "expected_action": "REQUEST_EVENT_REDELIVERY",
        "must_correlate_backlog_not_individual_fraud": True,
    }
    manifest.metrics = {
        "requests_executed": bounded_n,
        "elapsed_ms": elapsed_ms,
        "unsynced_payments_created": bounded_n,
    }
    return manifest


def restore_chaos_faults(
    db: Session,
    *,
    merchant_id: str = "MERCHANT_FINSHOP_01",
) -> dict[str, int]:
    """Safely restore test-world inventory invariants and unpause retrying outbox items."""
    _require_chaos_allowed()
    now = datetime.now(UTC)

    fixed_inv = 0
    invs = list(
        db.scalars(
            select(Inventory).where(Inventory.merchant_id == merchant_id).with_for_update()
        ).all()
    )
    for inv in invs:
        if inv.on_hand_qty < inv.reserved_qty:
            inv.on_hand_qty = inv.reserved_qty + 5
            inv.version += 1
            inv.updated_at = now
            fixed_inv += 1

    resumed_outbox = 0
    outs = list(
        db.scalars(
            select(EventOutbox)
            .where(EventOutbox.status == OutboxStatus.FAILED.value)
            .with_for_update()
        ).all()
    )
    for out in outs:
        out.status = OutboxStatus.PENDING.value
        out.next_attempt_at = now
        resumed_outbox += 1

    db.commit()
    return {"inventories_restored": fixed_inv, "outbox_resumed": resumed_outbox}
