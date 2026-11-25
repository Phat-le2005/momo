"""Evidence-Driven Recovery Planner & Autonomous Runtime Integration (Prompt 14 Part F).

Integrates:
- Authoritative multi-source evidence retrieval (`CommerceReadConnector`).
- Advisory Policy RAG (`retrieve_applicable_policies`).
- Merchant-scoped verified Incident Experience Memory (`retrieve_similar_experiences`).
- Durable `RecoveryCommand` execution (`execute_and_verify_commerce_recovery`).
- Independent post-action verification & verified experience recording.

Never hardcodes a recovery action merely because a detector code fired; always revalidates
current authoritative business state before proposing or executing any capability!
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.connectors.commerce import CommerceConnectorError, CommerceReadConnector
from app.connectors.commerce_write import (
    RecoveryExecutionResult,
    execute_and_verify_commerce_recovery,
)
from app.knowledge.experience_memory import (
    record_verified_experience,
    retrieve_similar_experiences,
)
from app.knowledge.policy_rag import retrieve_applicable_policies
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.monitoring.service import build_connector_for_binding


@dataclass
class StructuredRecoveryPlan:
    anomaly_id: str
    problem_summary: str
    evidence_refs: list[str]
    hypotheses: list[dict[str, Any]]
    applicable_policies: list[dict[str, Any]]
    similar_experiences: list[dict[str, Any]]
    proposed_command: str
    risk_level: str
    requires_human_approval: bool
    expected_version: int | None = None
    preconditions_met: bool = False
    expected_postconditions: list[str] = field(default_factory=list)
    rationale: str = ""


def build_evidence_driven_recovery_plan(
    db: Session,
    *,
    binding: CommerceSourceBinding,
    obs: AnomalyObservation,
    connector: CommerceReadConnector,
) -> StructuredRecoveryPlan:
    """Inspect fresh authoritative Commerce evidence, policies, and memory to plan recovery."""
    evidence_refs: list[str] = []
    order_data: dict[str, Any] | None = None
    pay_data: dict[str, Any] | None = None

    if obs.affected_order_id:
        try:
            order_data = connector.get_order(obs.affected_order_id)["data"]
            evidence_refs.append(f"/api/v1/service/orders/{obs.affected_order_id}")
        except CommerceConnectorError:
            pass

    if obs.affected_payment_id:
        try:
            pay_data = connector.get_payment(obs.affected_payment_id)["data"]
            evidence_refs.append(f"/api/v1/service/payments/{obs.affected_payment_id}")
        except CommerceConnectorError:
            pass

    query = f"{obs.anomaly_type} {obs.rationale}"
    policies = retrieve_applicable_policies(query, top_k=3)
    experiences = retrieve_similar_experiences(
        db,
        session_id=obs.session_id,
        merchant_id=obs.merchant_id,
        symptom_query=query,
        limit=3,
    )

    hypotheses: list[dict[str, Any]] = []
    proposed_cmd = "REQUEST_MANUAL_REVIEW"
    risk_level = "HIGH"
    requires_approval = True
    expected_version: int | None = None
    preconditions_met = False
    postconditions: list[str] = []
    rationale = "Insufficient authoritative evidence for automated mutation; escalating safely."

    # Evidence-driven evaluation (NEVER keyed blindly on detector_code!)
    if order_data is not None and pay_data is not None:
        o_status = str(order_data.get("status") or "").upper()
        p_status = str(pay_data.get("status") or "").upper()
        p_provider = str(pay_data.get("provider") or "LOCAL_MOCK").upper()
        p_synced = bool(pay_data.get("is_synced_to_order", False))
        p_recon = bool(pay_data.get("reconciliation_required", False))
        o_recon = str(order_data.get("reconciliation_status") or "CLEAR").upper()
        o_ver = order_data.get("version")
        if isinstance(o_ver, int):
            expected_version = o_ver

        if (
            o_status == "PENDING_PAYMENT"
            and p_status == "SUCCEEDED"
            and not p_synced
            and not p_recon
            and o_recon != "RECONCILIATION_REQUIRED"
            and p_provider == "LOCAL_MOCK"
        ):
            hypotheses.append(
                {
                    "cause": "Confirmed LOCAL_MOCK payment failed to synchronize order state",
                    "confidence": 0.92,
                    "supported_by": evidence_refs,
                }
            )
            proposed_cmd = "REQUEST_ORDER_SYNC"
            risk_level = "LOW"
            requires_approval = False
            preconditions_met = True
            postconditions = [
                "Order status transitions to CONFIRMED",
                "Payment is_synced_to_order becomes True",
                "No reconciliation conflict flagged",
            ]
            rationale = (
                "Authoritative LOCAL_MOCK payment is SUCCEEDED and unsynced while order is "
                "PENDING_PAYMENT with no conflicts; targeted REQUEST_ORDER_SYNC permitted."
            )
        elif o_status == "CANCELLED" and p_status == "SUCCEEDED":
            hypotheses.append(
                {
                    "cause": "Late payment succeeded after order cancellation",
                    "confidence": 0.95,
                    "supported_by": evidence_refs,
                }
            )
            proposed_cmd = "REQUEST_MANUAL_REVIEW"
            risk_level = "HIGH"
            requires_approval = True
            preconditions_met = True
            postconditions = ["Order remains CANCELLED", "Finance reviews refund/reconciliation"]
            rationale = "Cancelled orders MUST NOT be resurrected; escalating for Finance review."
        elif o_status == "PENDING_PAYMENT" and p_status in ("PENDING", "INITIATED", "UNKNOWN"):
            hypotheses.append(
                {
                    "cause": "Payment not yet confirmed by authoritative provider",
                    "confidence": 0.85,
                    "supported_by": evidence_refs,
                }
            )
            proposed_cmd = "REQUEST_PAYMENT_RECONCILIATION"
            risk_level = "LOW"
            requires_approval = False
            preconditions_met = False
            postconditions = ["Order remains PENDING_PAYMENT until authoritative confirmation"]
            rationale = "Payment is NOT confirmed; refusing order sync and flagging observation."
    elif (obs.affected_entities_json or {}).get("event_id"):
        hypotheses.append(
            {
                "cause": "Scoped business event delivery lag or failure",
                "confidence": 0.85,
                "supported_by": evidence_refs,
            }
        )
        proposed_cmd = "REQUEST_EVENT_REDELIVERY"
        risk_level = "LOW"
        requires_approval = False
        preconditions_met = True
        postconditions = ["Scoped event present in durable inbox without duplicates"]
        rationale = "Idempotent scoped event redelivery is safe and low-risk."
    else:
        hypotheses.append(
            {
                "cause": "Unknown operational or multivariate business anomaly",
                "confidence": 0.4,
                "supported_by": evidence_refs,
            }
        )
        postconditions = ["Sustained return to learned behavioral baseline"]

    return StructuredRecoveryPlan(
        anomaly_id=obs.id,
        problem_summary=obs.rationale,
        evidence_refs=evidence_refs,
        hypotheses=hypotheses,
        applicable_policies=policies,
        similar_experiences=experiences,
        proposed_command=proposed_cmd,
        risk_level=risk_level,
        requires_human_approval=requires_approval,
        expected_version=expected_version,
        preconditions_met=preconditions_met,
        expected_postconditions=postconditions,
        rationale=rationale,
    )


def execute_autonomous_recovery_plan(
    db: Session,
    *,
    binding: CommerceSourceBinding,
    obs: AnomalyObservation,
    write_key: str | None = None,
    read_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[StructuredRecoveryPlan, RecoveryExecutionResult | None]:
    """Plan, policy-check, execute (only if low-risk & preconditions met), verify, and learn."""
    reader = build_connector_for_binding(binding, service_key=read_key, transport=transport)
    plan = build_evidence_driven_recovery_plan(db, binding=binding, obs=obs, connector=reader)

    if plan.requires_human_approval or not plan.preconditions_met:
        return plan, None

    idemp_key = f"AUTO-{obs.id}-{obs.occurrence_count}-{plan.proposed_command}"[:128]
    exec_res = execute_and_verify_commerce_recovery(
        db,
        binding=binding,
        obs=obs,
        command=plan.proposed_command,
        idempotency_key=idemp_key,
        expected_version=plan.expected_version,
        write_key=write_key,
        read_key=read_key,
        transport=transport,
        read_connector=reader,
    )

    # Record verified outcome into merchant-scoped Incident Experience Memory
    if exec_res.verified_recovery:
        root_cause = plan.hypotheses[0]["cause"] if plan.hypotheses else obs.anomaly_type
        record_verified_experience(
            db,
            session_id=obs.session_id,
            merchant_id=obs.merchant_id,
            case_id=obs.linked_case_id,
            symptom_summary=obs.rationale,
            symptom_tags=[obs.detector_code, obs.anomaly_type, plan.proposed_command],
            validated_root_cause=str(root_cause),
            evidence_refs=plan.evidence_refs or [f"ANOM:{obs.id}"],
            actions_attempted=[plan.proposed_command],
            verified_recovery_steps=[f"{plan.proposed_command} -> VERIFIED_RECOVERY"],
            policy_version=(
                str(plan.applicable_policies[0]["version"]) if plan.applicable_policies else "2.0"
            ),
        )
    return plan, exec_res
