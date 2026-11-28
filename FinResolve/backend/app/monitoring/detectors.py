"""Deterministic Commerce Anomaly Detectors D01..D08 (Prompt 12 Section 11).

All detectors are pure, deterministic, independently unit-testable functions that operate
strictly on authoritative HTTP observations from MiniStore and local FinResolve inbox state.
They NEVER call LLMs, NEVER mutate Commerce state, and NEVER fabricate anomalies on normal
operations (respecting grace periods, legitimate partial refunds, and unpaid abandonments).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.connectors.commerce import CommerceMetricsSnapshot


@dataclass(frozen=True)
class DetectedAnomaly:
    """Canonical anomaly candidate emitted by a deterministic detector."""

    detector_code: str
    anomaly_type: str
    fingerprint: str
    severity: str
    affected_order_id: str | None
    affected_payment_id: str | None
    affected_entities: dict[str, Any]
    evidence_refs: list[dict[str, Any]]
    rationale: str
    case_type: str = "PAYMENT_DISPUTE"
    recommended_action_hint: str = "MANUAL_REVIEW"


@dataclass
class CommerceSnapshot:
    """Point-in-time authoritative Commerce snapshot fetched via HTTP connector."""

    merchant_id: str
    commerce_session_id: str
    observed_at: datetime
    orders: list[dict[str, Any]] = field(default_factory=list)
    payments: list[dict[str, Any]] = field(default_factory=list)
    refunds: list[dict[str, Any]] = field(default_factory=list)
    inventory: list[dict[str, Any]] = field(default_factory=list)
    reservations_by_order: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    metrics: CommerceMetricsSnapshot | None = None
    ingested_event_ids: set[str] = field(default_factory=set)
    remote_events: list[dict[str, Any]] = field(default_factory=list)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _age_seconds(ts: datetime | None, now: datetime) -> float:
    if ts is None:
        return 0.0
    return max(0.0, (now - ts).total_seconds())


# --- D01: Payment Confirmed Without Order Sync ---
def detect_d01_payment_sync_mismatch(
    snapshot: CommerceSnapshot,
    *,
    grace_seconds: int = 30,
) -> list[DetectedAnomaly]:
    orders_by_id = {str(o.get("id")): o for o in snapshot.orders if o.get("id")}
    anomalies: list[DetectedAnomaly] = []

    for pay in snapshot.payments:
        pay_id = str(pay.get("payment_id") or "")
        order_id = str(pay.get("order_id") or "")
        if not pay_id or not order_id:
            continue
        pay_status = str(pay.get("status") or "").upper()
        is_synced = bool(pay.get("is_synced_to_order", False))
        if pay_status != "SUCCEEDED" or is_synced:
            continue

        order = orders_by_id.get(order_id)
        if order is None:
            continue
        order_status = str(order.get("status") or "").upper()
        if order_status != "PENDING_PAYMENT":
            continue

        confirmed_at = _parse_iso(pay.get("confirmed_at")) or _parse_iso(
            pay.get("last_observed_at")
        )
        age_s = _age_seconds(confirmed_at, snapshot.observed_at)
        if age_s < float(grace_seconds):
            # Within normal async sync grace window — NOT an anomaly!
            continue

        anomalies.append(
            DetectedAnomaly(
                detector_code="D01",
                anomaly_type="PAYMENT_SYNC_MISMATCH",
                fingerprint=f"D01:{snapshot.merchant_id}:{order_id}:{pay_id}",
                severity="HIGH",
                affected_order_id=order_id,
                affected_payment_id=pay_id,
                affected_entities={"order_id": order_id, "payment_id": pay_id},
                evidence_refs=[
                    {
                        "source": "MINI_STORE_COMMERCE_HTTP",
                        "endpoint": f"/api/v1/service/payments/{pay_id}",
                        "payment_status": pay_status,
                        "provider": pay.get("provider"),
                        "provider_environment": pay.get("provider_environment"),
                        "observation_source": pay.get("observation_source"),
                        "signature_verified": pay.get("signature_verified"),
                        "is_synced_to_order": is_synced,
                        "age_seconds": round(age_s, 1),
                    },
                    {
                        "source": "MINI_STORE_COMMERCE_HTTP",
                        "endpoint": f"/api/v1/service/orders/{order_id}",
                        "order_status": order_status,
                    },
                ],
                rationale=(
                    f"Payment {pay_id} is SUCCEEDED and unsynced for {age_s:.0f}s "
                    f"(grace={grace_seconds}s) while Order {order_id} remains PENDING_PAYMENT."
                ),
                case_type="PAYMENT_SYNC_MISMATCH",
                recommended_action_hint=(
                    "REVIEW_EXTERNAL_PAYMENT_SYNC"
                    if pay.get("provider") == "VNPAY_SANDBOX"
                    else "SYNC_ORDER_FROM_CONFIRMED_PAYMENT"
                ),
            )
        )
    return anomalies


# --- D02: Late Payment On Cancelled Order (Never Resurrects!) ---
def detect_d02_late_payment_on_cancelled_order(
    snapshot: CommerceSnapshot,
) -> list[DetectedAnomaly]:
    orders_by_id = {str(o.get("id")): o for o in snapshot.orders if o.get("id")}
    anomalies: list[DetectedAnomaly] = []

    for pay in snapshot.payments:
        pay_id = str(pay.get("payment_id") or "")
        order_id = str(pay.get("order_id") or "")
        pay_status = str(pay.get("status") or "").upper()
        if pay_status != "SUCCEEDED" or not order_id:
            continue
        order = orders_by_id.get(order_id)
        if order is None:
            continue
        if str(order.get("status") or "").upper() != "CANCELLED":
            continue

        anomalies.append(
            DetectedAnomaly(
                detector_code="D02",
                anomaly_type="LATE_PAYMENT_ON_CANCELLED_ORDER",
                fingerprint=f"D02:{snapshot.merchant_id}:{order_id}:{pay_id}",
                severity="CRITICAL",
                affected_order_id=order_id,
                affected_payment_id=pay_id,
                affected_entities={"order_id": order_id, "payment_id": pay_id},
                evidence_refs=[
                    {
                        "source": "MINI_STORE_COMMERCE_HTTP",
                        "endpoint": f"/api/v1/service/payments/{pay_id}",
                        "payment_status": pay_status,
                        "provider": pay.get("provider"),
                        "provider_environment": pay.get("provider_environment"),
                        "observation_source": pay.get("observation_source"),
                        "signature_verified": pay.get("signature_verified"),
                        "amount_vnd": pay.get("amount_vnd"),
                    },
                    {
                        "source": "MINI_STORE_COMMERCE_HTTP",
                        "endpoint": f"/api/v1/service/orders/{order_id}",
                        "order_status": "CANCELLED",
                        "reconciliation_status": order.get("reconciliation_status"),
                    },
                ],
                rationale=(
                    f"Payment {pay_id} succeeded on CANCELLED Order {order_id}. "
                    "Order MUST NOT be resurrected automatically; requires reconciliation review."
                ),
                case_type="PAYMENT_DISPUTE",
                recommended_action_hint=(
                    "REVIEW_EXTERNAL_LATE_PAYMENT"
                    if pay.get("provider") == "VNPAY_SANDBOX"
                    else "HUMAN_APPROVAL_REFUND_OR_RECONCILE"
                ),
            )
        )
    return anomalies


# --- D03: Stalled Order In Confirmed / Processing ---
def detect_d03_stalled_order(
    snapshot: CommerceSnapshot,
    *,
    stall_seconds: int = 1800,
) -> list[DetectedAnomaly]:
    anomalies: list[DetectedAnomaly] = []
    for order in snapshot.orders:
        order_id = str(order.get("id") or "")
        status = str(order.get("status") or "").upper()
        # Only flag paid/confirmed/processing orders; NEVER flag unpaid PENDING_PAYMENT abandonments
        if status not in ("CONFIRMED", "PROCESSING"):
            continue
        updated_at = _parse_iso(order.get("updated_at")) or _parse_iso(order.get("created_at"))
        age_s = _age_seconds(updated_at, snapshot.observed_at)
        if age_s < float(stall_seconds):
            continue

        anomalies.append(
            DetectedAnomaly(
                detector_code="D03",
                anomaly_type="ORDER_STALLED",
                fingerprint=f"D03:{snapshot.merchant_id}:{order_id}:{status}",
                severity="MEDIUM",
                affected_order_id=order_id,
                affected_payment_id=None,
                affected_entities={"order_id": order_id, "order_status": status},
                evidence_refs=[
                    {
                        "source": "MINI_STORE_COMMERCE_HTTP",
                        "endpoint": f"/api/v1/service/orders/{order_id}",
                        "status": status,
                        "stalled_seconds": round(age_s, 1),
                    }
                ],
                rationale=(
                    f"Order {order_id} has been stalled in {status} for {age_s:.0f}s "
                    f"(threshold={stall_seconds}s)."
                ),
                case_type="ORDER_STALLED",
                recommended_action_hint="MERCHANT_FULFILLMENT_REVIEW",
            )
        )
    return anomalies


# --- D04: Refund Anomaly (Excessive / Conflicting Refund Accounting) ---
def detect_d04_refund_anomaly(snapshot: CommerceSnapshot) -> list[DetectedAnomaly]:
    refunds_by_pay: dict[str, list[dict[str, Any]]] = {}
    for rf in snapshot.refunds:
        pid = str(rf.get("payment_id") or "")
        if pid:
            refunds_by_pay.setdefault(pid, []).append(rf)

    anomalies: list[DetectedAnomaly] = []
    for pay in snapshot.payments:
        pay_id = str(pay.get("payment_id") or "")
        order_id = str(pay.get("order_id") or "")
        amount_vnd = int(pay.get("amount_vnd") or 0)
        refunded = int(pay.get("refunded_amount_vnd") or 0)
        pending = int(pay.get("pending_refund_amount_vnd") or 0)
        unknown = int(pay.get("unknown_refund_amount_vnd") or 0)
        committed = refunded + pending + unknown

        pay_refunds = refunds_by_pay.get(pay_id, [])
        active_refunds = [
            r
            for r in pay_refunds
            if str(r.get("status") or "").upper() in ("SUCCEEDED", "PENDING", "UNKNOWN")
        ]
        unknown_refunds = [
            r for r in pay_refunds if str(r.get("status") or "").upper() == "UNKNOWN"
        ]

        # Legitimate equal partial refunds whose sum <= amount_vnd are NOT flagged!
        is_excessive = committed > amount_vnd and amount_vnd > 0
        has_unknown_refund = unknown > 0 or len(unknown_refunds) > 0
        if not is_excessive and not has_unknown_refund:
            continue

        reason_parts: list[str] = []
        if is_excessive:
            reason_parts.append(
                f"committed refund total {committed:,}đ exceeds payment amount {amount_vnd:,}đ"
            )
        if has_unknown_refund:
            reason_parts.append(
                f"unresolved UNKNOWN refund amount {unknown:,}đ ({len(unknown_refunds)} txn)"
            )

        anomalies.append(
            DetectedAnomaly(
                detector_code="D04",
                anomaly_type="REFUND_ANOMALY",
                fingerprint=f"D04:{snapshot.merchant_id}:{pay_id}",
                severity="CRITICAL" if is_excessive else "HIGH",
                affected_order_id=order_id or None,
                affected_payment_id=pay_id,
                affected_entities={
                    "order_id": order_id,
                    "payment_id": pay_id,
                    "committed_refund_vnd": committed,
                    "payment_amount_vnd": amount_vnd,
                },
                evidence_refs=[
                    {
                        "source": "MINI_STORE_COMMERCE_HTTP",
                        "endpoint": f"/api/v1/service/payments/{pay_id}/refunds",
                        "refunded_amount_vnd": refunded,
                        "pending_refund_amount_vnd": pending,
                        "unknown_refund_amount_vnd": unknown,
                        "active_refund_count": len(active_refunds),
                    }
                ],
                rationale=f"Payment {pay_id} refund anomaly: " + "; ".join(reason_parts) + ".",
                case_type="REFUND_ANOMALY",
                recommended_action_hint="FINANCE_RECONCILIATION_REVIEW",
            )
        )
    return anomalies


# --- D05: Inventory Inconsistency ---
def detect_d05_inventory_inconsistency(snapshot: CommerceSnapshot) -> list[DetectedAnomaly]:
    anomalies: list[DetectedAnomaly] = []

    for inv in snapshot.inventory:
        variant_id = str(inv.get("variant_id") or "")
        on_hand = int(inv.get("on_hand_qty") or 0)
        reserved = int(inv.get("reserved_qty") or 0)
        if on_hand < 0 or reserved < 0 or reserved > on_hand:
            anomalies.append(
                DetectedAnomaly(
                    detector_code="D05",
                    anomaly_type="INVENTORY_INCONSISTENCY",
                    fingerprint=f"D05:{snapshot.merchant_id}:INV:{variant_id}",
                    severity="HIGH",
                    affected_order_id=None,
                    affected_payment_id=None,
                    affected_entities={
                        "variant_id": variant_id,
                        "sku": inv.get("sku"),
                        "scope": inv.get("scope", "MERCHANT_SHARED"),
                    },
                    evidence_refs=[
                        {
                            "source": "MINI_STORE_COMMERCE_HTTP",
                            "endpoint": "/api/v1/service/inventory",
                            "variant_id": variant_id,
                            "on_hand_qty": on_hand,
                            "reserved_qty": reserved,
                            "scope": inv.get("scope", "MERCHANT_SHARED"),
                        }
                    ],
                    rationale=(
                        f"Merchant-shared inventory invariant broken for variant {variant_id}: "
                        f"on_hand={on_hand}, reserved={reserved}."
                    ),
                    case_type="INVENTORY_ANOMALY",
                    recommended_action_hint="INVENTORY_AUDIT_REQUIRED",
                )
            )

    # Also detect explicit INVENTORY_INCONSISTENCY_DETECTED events
    for ev in snapshot.remote_events:
        if str(ev.get("event_type") or "").upper() == "INVENTORY_INCONSISTENCY_DETECTED":
            order_id = ev.get("order_id")
            ev_id = str(ev.get("event_id") or ev.get("id") or "")
            anomalies.append(
                DetectedAnomaly(
                    detector_code="D05",
                    anomaly_type="INVENTORY_INCONSISTENCY",
                    fingerprint=f"D05:{snapshot.merchant_id}:EVT:{order_id or ev_id}",
                    severity="HIGH",
                    affected_order_id=str(order_id) if order_id else None,
                    affected_payment_id=str(ev.get("payment_id")) if ev.get("payment_id") else None,
                    affected_entities={"order_id": order_id, "event_id": ev_id},
                    evidence_refs=[
                        {
                            "source": "MINI_STORE_COMMERCE_HTTP",
                            "endpoint": "/api/v1/service/events",
                            "event_id": ev_id,
                            "event_type": "INVENTORY_INCONSISTENCY_DETECTED",
                        }
                    ],
                    rationale=(
                        f"Commerce emitted INVENTORY_INCONSISTENCY_DETECTED ({ev_id}) "
                        f"for order {order_id or 'N/A'}."
                    ),
                    case_type="INVENTORY_ANOMALY",
                    recommended_action_hint="INVENTORY_AUDIT_REQUIRED",
                )
            )
    return anomalies


# Events expected to arrive via push outbox (MiniStore FINRESOLVE_SUPPORTED_EVENT_MAP)
PUSH_EXPECTED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "ORDER_CREATED",
        "ORDER_UPDATED",
        "PAYMENT_INITIATED",
        "INVENTORY_UPDATED",
    }
)


# --- D06: Missing / Delayed Event Delivery ---
def detect_d06_missing_event_delivery(
    snapshot: CommerceSnapshot,
    *,
    grace_seconds: int = 30,
) -> list[DetectedAnomaly]:
    anomalies: list[DetectedAnomaly] = []
    for ev in snapshot.remote_events:
        ev_id = str(ev.get("event_id") or "")
        ev_type = str(ev.get("event_type") or "").strip().upper()
        # Only flag push-expected event types; never classify pull-only events as missing push!
        if not ev_id or ev_type not in PUSH_EXPECTED_EVENT_TYPES:
            continue
        if ev_id in snapshot.ingested_event_ids:
            continue
        occurred = _parse_iso(ev.get("occurred_at"))
        age_s = _age_seconds(occurred, snapshot.observed_at)
        if age_s < float(grace_seconds):
            continue
        anomalies.append(
            DetectedAnomaly(
                detector_code="D06",
                anomaly_type="MISSING_EVENT_DELIVERY",
                fingerprint=f"D06:{snapshot.merchant_id}:{ev_id}",
                severity="MEDIUM",
                affected_order_id=str(ev.get("order_id")) if ev.get("order_id") else None,
                affected_payment_id=str(ev.get("payment_id")) if ev.get("payment_id") else None,
                affected_entities={"event_id": ev_id, "event_type": ev_type},
                evidence_refs=[
                    {
                        "source": "MINI_STORE_COMMERCE_HTTP",
                        "endpoint": "/api/v1/service/events",
                        "event_id": ev_id,
                        "event_type": ev_type,
                        "age_seconds": round(age_s, 1),
                    }
                ],
                rationale=(
                    f"Push-expected Commerce event {ev_id} ({ev_type}) was not in FinResolve "
                    f"inbox after {age_s:.0f}s; recovered via pull reconciliation."
                ),
                case_type="OPERATIONAL_ANOMALY",
                recommended_action_hint="EVENT_DELIVERY_HEALTH_CHECK",
            )
        )
    return anomalies


# --- D07: Financial Evidence Conflict / Reconciliation Required ---
def detect_d07_financial_evidence_conflict(snapshot: CommerceSnapshot) -> list[DetectedAnomaly]:
    anomalies: list[DetectedAnomaly] = []
    seen_orders: set[str] = set()

    for pay in snapshot.payments:
        pay_id = str(pay.get("payment_id") or "")
        order_id = str(pay.get("order_id") or "")
        recon_req = bool(pay.get("reconciliation_required", False))
        pay_status = str(pay.get("status") or "").upper()
        if not recon_req and pay_status != "UNKNOWN":
            continue

        # Distinguish UNKNOWN provider outcome from confirmed conflict
        is_unknown_only = pay_status == "UNKNOWN" and not recon_req
        note = str(pay.get("reconciliation_note") or "")
        seen_orders.add(order_id)
        anomalies.append(
            DetectedAnomaly(
                detector_code="D07",
                anomaly_type=(
                    "PAYMENT_PROVIDER_OUTCOME_UNKNOWN"
                    if is_unknown_only
                    else "FINANCIAL_EVIDENCE_CONFLICT"
                ),
                fingerprint=f"D07:{snapshot.merchant_id}:{pay_id}",
                severity="MEDIUM" if is_unknown_only else "HIGH",
                affected_order_id=order_id or None,
                affected_payment_id=pay_id,
                affected_entities={
                    "order_id": order_id,
                    "payment_id": pay_id,
                    "provider": pay.get("provider"),
                    "provider_environment": pay.get("provider_environment"),
                },
                evidence_refs=[
                    {
                        "source": "MINI_STORE_COMMERCE_HTTP",
                        "endpoint": f"/api/v1/service/payments/{pay_id}",
                        "status": pay_status,
                        "reconciliation_required": recon_req,
                        "provider_environment": pay.get("provider_environment"),
                        "observation_source": pay.get("observation_source"),
                        "signature_verified": pay.get("signature_verified"),
                        "provider_response_code": pay.get("provider_response_code"),
                        "provider_transaction_status": pay.get("provider_transaction_status"),
                    }
                ],
                rationale=(
                    f"Payment {pay_id} has UNKNOWN status (not fraud; needs verification)."
                    if is_unknown_only
                    else (
                        f"Payment {pay_id} requires financial reconciliation: "
                        f"{note or 'conflict flagged'}."
                    )
                ),
                case_type="PAYMENT_DISPUTE",
                recommended_action_hint="PROVIDER_RECONCILIATION_REVIEW",
            )
        )

    for order in snapshot.orders:
        order_id = str(order.get("id") or "")
        if not order_id or order_id in seen_orders:
            continue
        if str(order.get("reconciliation_status") or "").upper() == "RECONCILIATION_REQUIRED":
            anomalies.append(
                DetectedAnomaly(
                    detector_code="D07",
                    anomaly_type="FINANCIAL_EVIDENCE_CONFLICT",
                    fingerprint=f"D07:{snapshot.merchant_id}:ORD:{order_id}",
                    severity="HIGH",
                    affected_order_id=order_id,
                    affected_payment_id=None,
                    affected_entities={"order_id": order_id},
                    evidence_refs=[
                        {
                            "source": "MINI_STORE_COMMERCE_HTTP",
                            "endpoint": f"/api/v1/service/orders/{order_id}",
                            "reconciliation_status": "RECONCILIATION_REQUIRED",
                            "reconciliation_reason": order.get("reconciliation_reason"),
                        }
                    ],
                    rationale=(
                        f"Order {order_id} is flagged RECONCILIATION_REQUIRED: "
                        f"{order.get('reconciliation_reason') or 'evidence conflict'}."
                    ),
                    case_type="PAYMENT_DISPUTE",
                    recommended_action_hint="PROVIDER_RECONCILIATION_REVIEW",
                )
            )
    return anomalies


# --- D08: Operational Degradation (Outbox Lag / Unsynced Backlog) ---
def detect_d08_operational_degradation(
    snapshot: CommerceSnapshot,
    *,
    outbox_lag_threshold: int = 10,
    unsynced_backlog_threshold: int = 5,
) -> list[DetectedAnomaly]:
    m = snapshot.metrics
    if m is None:
        return []
    breaches: list[str] = []
    if m.outbox_lag_count >= outbox_lag_threshold:
        breaches.append(f"outbox_lag_count={m.outbox_lag_count} >= {outbox_lag_threshold}")
    if m.unsynced_payments >= unsynced_backlog_threshold:
        breaches.append(f"unsynced_payments={m.unsynced_payments} >= {unsynced_backlog_threshold}")
    if not breaches:
        return []

    return [
        DetectedAnomaly(
            detector_code="D08",
            anomaly_type="OPERATIONAL_DEGRADATION",
            fingerprint=f"D08:{snapshot.merchant_id}:{snapshot.commerce_session_id}",
            severity="HIGH",
            affected_order_id=None,
            affected_payment_id=None,
            affected_entities={
                "merchant_id": snapshot.merchant_id,
                "commerce_session_id": snapshot.commerce_session_id,
            },
            evidence_refs=[
                {
                    "source": "MINI_STORE_COMMERCE_HTTP",
                    "endpoint": "/api/v1/service/metrics",
                    "outbox_lag_count": m.outbox_lag_count,
                    "unsynced_payments": m.unsynced_payments,
                    "reconciliation_required_orders": m.reconciliation_required_orders,
                }
            ],
            rationale="Commerce operational backlog threshold breached: "
            + "; ".join(breaches)
            + ".",
            case_type="OPERATIONAL_ANOMALY",
            recommended_action_hint="CHECK_COMMERCE_WORKERS",
        )
    ]


def run_all_detectors(
    snapshot: CommerceSnapshot,
    *,
    payment_sync_grace_seconds: int = 30,
    order_stall_seconds: int = 1800,
    outbox_lag_threshold: int = 10,
    unsynced_backlog_threshold: int = 5,
    include_missing_events: bool = False,
) -> list[DetectedAnomaly]:
    """Execute all deterministic detectors D01..D08 and return deduplicated candidates."""
    candidates: list[DetectedAnomaly] = []
    candidates.extend(
        detect_d01_payment_sync_mismatch(snapshot, grace_seconds=payment_sync_grace_seconds)
    )
    candidates.extend(detect_d02_late_payment_on_cancelled_order(snapshot))
    candidates.extend(detect_d03_stalled_order(snapshot, stall_seconds=order_stall_seconds))
    candidates.extend(detect_d04_refund_anomaly(snapshot))
    candidates.extend(detect_d05_inventory_inconsistency(snapshot))
    if include_missing_events:
        candidates.extend(
            detect_d06_missing_event_delivery(snapshot, grace_seconds=payment_sync_grace_seconds)
        )
    candidates.extend(detect_d07_financial_evidence_conflict(snapshot))
    candidates.extend(
        detect_d08_operational_degradation(
            snapshot,
            outbox_lag_threshold=outbox_lag_threshold,
            unsynced_backlog_threshold=unsynced_backlog_threshold,
        )
    )
    # Deduplicate by fingerprint within a single scan
    seen: set[str] = set()
    unique: list[DetectedAnomaly] = []
    for item in candidates:
        if item.fingerprint not in seen:
            seen.add(item.fingerprint)
            unique.append(item)
    return unique
