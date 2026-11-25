"""Versioned Policy Knowledge Base & Advisory RAG Retrieval (Prompt 14 Sections 19-20).

Safety Invariants:
- All bundled policies are explicitly labeled `SANDBOX_DEMO_POLICY` (never masquerading
  as official MoMo, Shopee, TikTok, or VNPAY external platform policies).
- RAG retrieval is strictly ADVISORY: retrieved text NEVER grants new permissions;
  the deterministic Policy Engine remains the final enforcement authority.
- Resolves conflicts by explicit `authority_level` (higher wins) then semantic `version`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PolicyDocument:
    policy_id: str
    title: str
    version: str
    effective_date: str
    applicable_scope: str
    authority_level: int  # 3=PLATFORM_SAFETY, 2=OPS_POLICY, 1=MERCHANT_RULE
    source: str
    required_approval: str
    required_evidence: tuple[str, ...]
    permitted_capabilities: tuple[str, ...]
    restrictions: tuple[str, ...]
    body: str


SANDBOX_POLICY_CORPUS: tuple[PolicyDocument, ...] = (
    PolicyDocument(
        policy_id="POL-PAY-VERIFY-01",
        title="Payment Verification & Order Synchronization Policy",
        version="2.0",
        effective_date="2026-11-01",
        applicable_scope="LOCAL_MOCK_SANDBOX",
        authority_level=3,
        source="SANDBOX_DEMO_POLICY",
        required_approval="AUTO_LOW_RISK_WHEN_PRECONDITIONS_MET",
        required_evidence=(
            "authoritative_payment_record",
            "order_record",
            "inventory_reservations",
        ),
        permitted_capabilities=("REQUEST_ORDER_SYNC", "REQUEST_PAYMENT_RECONCILIATION"),
        restrictions=(
            "Never confirm an order unless authoritative payment is SUCCEEDED and amount matches.",
            "Never confirm payment based solely on a customer message or uploaded receipt image.",
        ),
        body=(
            "When an order remains PENDING_PAYMENT while authoritative payment status is SUCCEEDED "
            "and ACTIVE inventory reservations match order items, targeted REQUEST_ORDER_SYNC is "
            "permitted for LOCAL_MOCK orders. If payment is PENDING or UNKNOWN, do NOT confirm."
        ),
    ),
    PolicyDocument(
        policy_id="POL-REFUND-02",
        title="Refund Accounting & Duplicate Protection Policy",
        version="2.0",
        effective_date="2026-11-01",
        applicable_scope="LOCAL_MOCK_SANDBOX",
        authority_level=3,
        source="SANDBOX_DEMO_POLICY",
        required_approval="FINANCE_LEAD_REQUIRED",
        required_evidence=("payment_record", "refund_history"),
        permitted_capabilities=("REQUEST_MANUAL_REVIEW", "REQUEST_PAYMENT_RECONCILIATION"),
        restrictions=(
            "Never issue automatic refunds without explicit Finance approval.",
            "Reserve SUCCEEDED + PENDING + UNKNOWN refund amounts in remaining balance.",
        ),
        body=(
            "If refund totals exceed payment amount or any RefundTransaction is in UNKNOWN status, "
            "block additional refunds and escalate via REQUEST_MANUAL_REVIEW."
        ),
    ),
    PolicyDocument(
        policy_id="POL-CANCEL-03",
        title="Cancelled Order Late-Payment Safety Policy",
        version="1.5",
        effective_date="2026-11-01",
        applicable_scope="LOCAL_MOCK_SANDBOX",
        authority_level=3,
        source="SANDBOX_DEMO_POLICY",
        required_approval="OPS_MANAGER_OR_FINANCE_LEAD",
        required_evidence=("order_record", "payment_record"),
        permitted_capabilities=("REQUEST_MANUAL_REVIEW", "REQUEST_PAYMENT_RECONCILIATION"),
        restrictions=("Never resurrect a CANCELLED order even if a late payment succeeds.",),
        body=(
            "A late SUCCEEDED payment on a CANCELLED order cannot restore released inventory. "
            "Keep the order CANCELLED and route to refund/reconciliation review."
        ),
    ),
    PolicyDocument(
        policy_id="POL-INV-04",
        title="Inventory Reservation Integrity Policy",
        version="1.2",
        effective_date="2026-11-01",
        applicable_scope="LOCAL_MOCK_SANDBOX",
        authority_level=3,
        source="SANDBOX_DEMO_POLICY",
        required_approval="OPS_MANAGER_REQUIRED",
        required_evidence=("inventory_snapshot", "order_reservations"),
        permitted_capabilities=("REQUEST_MANUAL_REVIEW",),
        restrictions=(
            "Never manipulate inventory counts automatically or confirm without reservations.",
        ),
        body=(
            "When reserved_qty exceeds on_hand_qty or a paid order lacks ACTIVE reservations, "
            "halt automatic order confirmation and escalate for merchant inventory audit."
        ),
    ),
    PolicyDocument(
        policy_id="POL-DISPUTE-05",
        title="Customer Payment Claim & Receipt Evidence Policy",
        version="1.3",
        effective_date="2026-11-01",
        applicable_scope="LOCAL_MOCK_SANDBOX",
        authority_level=3,
        source="SANDBOX_DEMO_POLICY",
        required_approval="OPS_MANAGER_REQUIRED",
        required_evidence=("customer_claim", "authoritative_payment_record"),
        permitted_capabilities=("REQUEST_MANUAL_REVIEW",),
        restrictions=(
            "Treat uploaded receipt images strictly as customer claims, never payment proof.",
            "Do not accuse customers of fraud solely because a receipt cannot be verified.",
        ),
        body=(
            "Compare extracted receipt fields against authoritative payment records. If disputed "
            "or insufficient, mark CLAIM_CONTRADICTED or INSUFFICIENT_EVIDENCE and escalate."
        ),
    ),
    PolicyDocument(
        policy_id="POL-OPS-06",
        title="Service Degradation, Traffic Burst & Event Delivery Policy",
        version="1.4",
        effective_date="2026-11-01",
        applicable_scope="LOCAL_MOCK_SANDBOX",
        authority_level=2,
        source="SANDBOX_DEMO_POLICY",
        required_approval="AUTO_LOW_RISK_WHEN_PRECONDITIONS_MET",
        required_evidence=("operational_telemetry", "queue_metrics"),
        permitted_capabilities=("REQUEST_EVENT_REDELIVERY", "REQUEST_MANUAL_REVIEW"),
        restrictions=(
            "Require sustained healthy metric samples before declaring operational recovery.",
        ),
        body=(
            "During traffic spikes, latency increases, error-rate surges, or outbox delivery lags, "
            "correlate related signals, redeliver scoped failed events idempotently, and avoid "
            "misclassifying operational backlog as individual fraud."
        ),
    ),
    PolicyDocument(
        policy_id="POL-UNKNOWN-07",
        title="Open-World Unknown Anomaly Escalation Policy",
        version="1.0",
        effective_date="2026-11-01",
        applicable_scope="LOCAL_MOCK_SANDBOX",
        authority_level=3,
        source="SANDBOX_DEMO_POLICY",
        required_approval="HUMAN_REVIEW_ON_UNCERTAINTY",
        required_evidence=("baseline_comparison", "multi_source_observations"),
        permitted_capabilities=("REQUEST_MANUAL_REVIEW",),
        restrictions=(
            "Never invent a root cause or execute mutations when evidence is insufficient.",
        ),
        body=(
            "For UNKNOWN_OPERATIONAL_ANOMALY or UNKNOWN_BUSINESS_ANOMALY, gather multi-source "
            "evidence, test competing hypotheses, and escalate via REQUEST_MANUAL_REVIEW if "
            "uncertainty remains."
        ),
    ),
)


def _tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9_]+", text.lower()) if len(t) >= 2]


def retrieve_applicable_policies(
    query: str,
    *,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Retrieve versioned policies using keyword + TF-IDF scoring and authority ordering."""
    q_tokens = set(_tokenize(query))
    if not q_tokens:
        return []

    # Compute simple IDF across corpus
    doc_tokens = [
        set(
            _tokenize(
                f"{d.title} {d.body} {' '.join(d.restrictions)} "
                f"{' '.join(d.permitted_capabilities)}"
            )
        )
        for d in SANDBOX_POLICY_CORPUS
    ]
    n_docs = len(SANDBOX_POLICY_CORPUS)
    scored: list[tuple[float, PolicyDocument]] = []

    for doc, d_toks in zip(SANDBOX_POLICY_CORPUS, doc_tokens, strict=True):
        overlap = q_tokens.intersection(d_toks)
        if not overlap:
            continue
        score = 0.0
        for tok in overlap:
            df = sum(1 for dt in doc_tokens if tok in dt)
            score += math.log((n_docs + 1) / (df + 1)) + 1.0
        # Normalize slightly and include authority tie-breaker
        norm_score = round(score / max(1.0, math.sqrt(len(q_tokens))), 4)
        scored.append((norm_score, doc))

    scored.sort(key=lambda item: (item[0], item[1].authority_level, item[1].version), reverse=True)

    return [
        {
            "policy_id": doc.policy_id,
            "title": doc.title,
            "version": doc.version,
            "effective_date": doc.effective_date,
            "source": doc.source,
            "authority_level": doc.authority_level,
            "relevance_score": sc,
            "required_approval": doc.required_approval,
            "required_evidence": list(doc.required_evidence),
            "permitted_capabilities": list(doc.permitted_capabilities),
            "restrictions": list(doc.restrictions),
            "advisory_only": True,
            "summary": doc.body,
        }
        for sc, doc in scored[: max(1, min(top_k, 5))]
    ]
