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
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.commerce import CommerceConnectorError, CommerceReadConnector
from app.connectors.commerce_write import (
    RecoveryExecutionResult,
    execute_and_verify_commerce_recovery,
)
from app.core.errors import AppError, ErrorCode
from app.knowledge.experience_memory import (
    record_verified_experience,
    retrieve_similar_experiences,
)
from app.knowledge.policy_rag import retrieve_applicable_policies
from app.models.cases import Evidence
from app.models.enums import EvidenceKind
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.monitoring.service import build_connector_for_binding

PROPOSABLE_RECOVERY_COMMANDS: frozenset[str] = frozenset(
    {
        "REQUEST_ORDER_SYNC",
        "REQUEST_EVENT_REDELIVERY",
        "REQUEST_PAYMENT_RECONCILIATION",
        "REQUEST_MANUAL_REVIEW",
        "WAIT_FOR_OBSERVATION",
        "COLLECT_MORE_EVIDENCE",
    }
)


class StructuredAgentRecoveryProposal(BaseModel):
    """Machine-readable structured recovery proposal bound to one investigation run."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str | None = Field(default=None, max_length=64)
    case_id: str = Field(..., min_length=3, max_length=64)
    run_id: str = Field(..., min_length=3, max_length=64)
    supervisor_job_id: str | None = Field(default=None, max_length=64)
    anomaly_id: str | None = Field(default=None, max_length=64)
    anomaly_occurrence: int = Field(default=1, ge=1)
    target_order_id: str | None = Field(default=None, max_length=64)
    target_payment_id: str | None = Field(default=None, max_length=64)
    expected_version: int | None = Field(default=None, ge=1)
    created_at: datetime | None = None
    expires_at: datetime | None = None
    lifecycle_state: str = Field(default="PROPOSED", max_length=32)
    affected_entities: dict[str, Any] = Field(default_factory=dict)
    observed_symptoms: list[str] = Field(default_factory=list)
    candidate_causes: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    proposed_command: str = Field(..., min_length=3, max_length=64)
    action_params: dict[str, Any] = Field(default_factory=dict)
    expected_preconditions: list[str] = Field(default_factory=list)
    expected_postconditions: list[str] = Field(default_factory=list)
    policy_references: list[str] = Field(default_factory=list)
    uncertainty_summary: str = Field(default="", max_length=1000)
    alternative_safe_outcomes: list[str] = Field(default_factory=list)
    proposal_source: Literal["AGENT_STRUCTURED_PROPOSAL", "DETERMINISTIC_FALLBACK"] = (
        "AGENT_STRUCTURED_PROPOSAL"
    )


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
    policy_ids: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    uncertainty_note: str = ""
    planning_mode: str = "DETERMINISTIC_SAFETY_BASELINE"


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
    missing_evidence: list[str] = []
    uncertainty_note = "Insufficient authoritative evidence for automated mutation."
    rationale = "Insufficient authoritative evidence for automated mutation; escalating safely."
    policy_ids = [
        f"{p['policy_id']}@{p.get('version', '1.0')}" for p in policies if "policy_id" in p
    ]

    # Deterministic safety baseline evaluation (PolicyEngine gates final authorization)
    if order_data is not None and pay_data is not None:
        o_status = str(order_data.get("status") or "").upper()
        p_status = str(pay_data.get("status") or "").upper()
        p_provider = str(pay_data.get("provider") or "").upper()
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
                    "evidence_support": "AUTHORITATIVE_STATE_MATCH",
                    "supported_by": evidence_refs,
                    "contradicting_evidence": [],
                }
            )
            proposed_cmd = "REQUEST_ORDER_SYNC"
            risk_level = "LOW"
            requires_approval = False
            preconditions_met = True
            uncertainty_note = "Postconditions must still be verified independently after sync."
            postconditions = [
                "Order status transitions to CONFIRMED",
                "Payment is_synced_to_order becomes True",
                "No reconciliation conflict flagged",
            ]
            rationale = (
                "Authoritative LOCAL_MOCK payment is SUCCEEDED and unsynced while order is "
                "PENDING_PAYMENT with no conflicts; proposing targeted REQUEST_ORDER_SYNC "
                "subject to PolicyEngine authorization."
            )
        elif o_status == "CANCELLED" and p_status == "SUCCEEDED":
            hypotheses.append(
                {
                    "cause": "Late payment succeeded after order cancellation",
                    "evidence_support": "AUTHORITATIVE_STATE_MATCH",
                    "supported_by": evidence_refs,
                    "contradicting_evidence": [],
                }
            )
            proposed_cmd = "REQUEST_MANUAL_REVIEW"
            risk_level = "HIGH"
            requires_approval = True
            preconditions_met = True
            uncertainty_note = "Requires Finance decision on refund vs re-order."
            postconditions = ["Order remains CANCELLED", "Finance reviews refund/reconciliation"]
            rationale = "Cancelled orders MUST NOT be resurrected; escalating for Finance review."
        elif o_status == "PENDING_PAYMENT" and p_status in ("PENDING", "INITIATED", "UNKNOWN"):
            hypotheses.append(
                {
                    "cause": "Payment not yet confirmed by authoritative provider",
                    "evidence_support": "AUTHORITATIVE_STATE_MATCH",
                    "supported_by": evidence_refs,
                    "contradicting_evidence": [],
                }
            )
            proposed_cmd = "REQUEST_PAYMENT_RECONCILIATION"
            risk_level = "LOW"
            requires_approval = False
            preconditions_met = False
            missing_evidence.append("authoritative_provider_confirmation")
            uncertainty_note = "Provider settlement status not yet finalized."
            postconditions = ["Order remains PENDING_PAYMENT until authoritative confirmation"]
            rationale = "Payment is NOT confirmed; refusing order sync and flagging observation."
    elif (obs.affected_entities_json or {}).get("event_id"):
        hypotheses.append(
            {
                "cause": "Scoped business event delivery lag or failure",
                "evidence_support": "EVENT_OUTBOX_OBSERVATION",
                "supported_by": evidence_refs,
                "contradicting_evidence": [],
            }
        )
        proposed_cmd = "REQUEST_EVENT_REDELIVERY"
        risk_level = "LOW"
        requires_approval = False
        preconditions_met = True
        uncertainty_note = "Verify inbox arrival without duplicates after redelivery."
        postconditions = ["Scoped event present in durable inbox without duplicates"]
        rationale = "Idempotent scoped event redelivery proposed subject to PolicyEngine."
    else:
        missing_evidence.append("authoritative_root_cause_evidence")
        hypotheses.append(
            {
                "cause": "Unknown operational or business anomaly (root cause unconfirmed)",
                "evidence_support": "STATISTICAL_DEVIATION_ONLY",
                "supported_by": evidence_refs,
                "contradicting_evidence": [],
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
        policy_ids=policy_ids,
        missing_evidence=missing_evidence,
        uncertainty_note=uncertainty_note,
        planning_mode="DETERMINISTIC_SAFETY_BASELINE",
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


def validate_and_execute_agent_proposal(
    db: Session,
    *,
    proposal: StructuredAgentRecoveryProposal,
    binding: CommerceSourceBinding,
    obs: AnomalyObservation,
    expected_run_id: str | None = None,
    expected_job_id: str | None = None,
    expected_claim_token: str | None = None,
    expected_anomaly_occurrence: int | None = None,
    expected_world_epoch: int | None = None,
    require_worker_fence: bool = False,
    write_key: str | None = None,
    read_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
    approval_id: str | None = None,
    require_human_approval: bool = False,
) -> tuple[StructuredAgentRecoveryProposal, RecoveryExecutionResult | None]:
    """Validate an Agent recovery proposal against run fencing, evidence, and PolicyEngine.

    Hard Invariants (Prompt 14.2 Fix 3 + Prompt 14.3 Fix 2):
    - Rejects rejected, expired, superseded, or earlier-run proposals.
    - Rejects target anomaly/order/payment mismatches (never silently substitutes defaults!).
    - Rejects superseded Supervisor jobs or stale world_epoch.
    - NEVER silently refreshes stale expected_version to force execution.
    - Respects REQUEST_MANUAL_REVIEW / WAIT_FOR_OBSERVATION / COLLECT_MORE_EVIDENCE directly.
    """
    norm_cmd = proposal.proposed_command.strip().upper()
    if norm_cmd not in PROPOSABLE_RECOVERY_COMMANDS:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"UNSUPPORTED_AGENT_PROPOSAL_COMMAND: '{norm_cmd}' is not an allowed capability",
            status_code=403,
        )

    # 1. Lifecycle, expiration, run & job fencing
    if proposal.lifecycle_state.strip().upper() != "PROPOSED":
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"PROPOSAL_NOT_EXECUTABLE: lifecycle_state is '{proposal.lifecycle_state}'",
            status_code=403,
        )
    if proposal.expires_at is not None:
        exp = proposal.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp <= datetime.now(UTC):
            raise AppError(
                ErrorCode.FORBIDDEN,
                f"PROPOSAL_EXPIRED: proposal expired at {exp.isoformat()}",
                status_code=403,
            )
    if expected_run_id and proposal.run_id != expected_run_id:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"PROPOSAL_RUN_MISMATCH: run '{proposal.run_id}' != current '{expected_run_id}'",
            status_code=403,
        )
    # Non-mutating Agent recommendations are respected directly before mutating fences
    is_non_mutating = norm_cmd in (
        "REQUEST_MANUAL_REVIEW",
        "WAIT_FOR_OBSERVATION",
        "COLLECT_MORE_EVIDENCE",
    )

    if (require_worker_fence or expected_job_id) and not is_non_mutating:
        from app.models.supervisor import SupervisorJob

        if not expected_job_id or not expected_job_id.strip():
            raise AppError(
                ErrorCode.CONFLICT,
                "MISSING_SUPERVISOR_JOB: autonomous mutation requires an active SupervisorJob ID",
                status_code=409,
            )
        # Re-read authoritative job ownership directly from PostgreSQL immediately before dispatch
        job_row = db.scalar(
            select(SupervisorJob).where(SupervisorJob.id == expected_job_id).with_for_update()
        )
        if job_row is None:
            raise AppError(
                ErrorCode.CONFLICT,
                f"MISSING_SUPERVISOR_JOB: job '{expected_job_id}' does not exist",
                status_code=409,
            )
        if job_row.status != "RUNNING":
            raise AppError(
                ErrorCode.CONFLICT,
                f"SUPERSEDED_SUPERVISOR_JOB: job '{expected_job_id}' is '{job_row.status}', "
                "not RUNNING",
                status_code=409,
            )
        if job_row.session_id != binding.session_id:
            raise AppError(
                ErrorCode.CONFLICT,
                f"JOB_SESSION_MISMATCH: job '{expected_job_id}' belongs to another session",
                status_code=409,
            )
        if not expected_claim_token or not expected_claim_token.strip():
            raise AppError(
                ErrorCode.CONFLICT,
                "MISSING_WORKER_CLAIM_TOKEN: autonomous mutation requires a non-empty claim token",
                status_code=409,
            )
        if job_row.claim_token != expected_claim_token:
            raise AppError(
                ErrorCode.CONFLICT,
                f"INVALID_WORKER_CLAIM_TOKEN: job '{expected_job_id}' owned by another worker",
                status_code=409,
            )
        if job_row.locked_until is None:
            raise AppError(
                ErrorCode.CONFLICT,
                f"MISSING_WORKER_LEASE: job '{expected_job_id}' has no active lease expiration",
                status_code=409,
            )
        lu = job_row.locked_until
        if lu.tzinfo is None:
            lu = lu.replace(tzinfo=UTC)
        if lu <= datetime.now(UTC):
            raise AppError(
                ErrorCode.CONFLICT,
                f"EXPIRED_WORKER_LEASE: job '{expected_job_id}' lease expired at {lu.isoformat()}",
                status_code=409,
            )
    if (
        expected_anomaly_occurrence is not None
        and proposal.anomaly_occurrence != expected_anomaly_occurrence
    ):
        raise AppError(
            ErrorCode.CONFLICT,
            f"ANOMALY_OCCURRENCE_MISMATCH: proposal occurrence {proposal.anomaly_occurrence} "
            f"!= current {expected_anomaly_occurrence}",
            status_code=409,
        )
    if expected_world_epoch is not None and binding.commerce_world_epoch != expected_world_epoch:
        raise AppError(
            ErrorCode.CONFLICT,
            f"STALE_WORLD_EPOCH: expected {expected_world_epoch}, binding at "
            f"{binding.commerce_world_epoch}",
            status_code=409,
        )

    # 2. Exact target correspondence (never silently replace Agent targets with anomaly defaults!)
    if proposal.anomaly_id and proposal.anomaly_id != obs.id:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"PROPOSAL_ANOMALY_MISMATCH: proposal targets '{proposal.anomaly_id}', not '{obs.id}'",
            status_code=403,
        )
    if (
        proposal.target_order_id
        and obs.affected_order_id
        and proposal.target_order_id != obs.affected_order_id
    ):
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"PROPOSAL_TARGET_MISMATCH: proposal order '{proposal.target_order_id}' != "
            f"'{obs.affected_order_id}'",
            status_code=403,
        )
    if (
        proposal.target_payment_id
        and obs.affected_payment_id
        and proposal.target_payment_id != obs.affected_payment_id
    ):
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"PROPOSAL_TARGET_MISMATCH: proposal payment '{proposal.target_payment_id}' != "
            f"'{obs.affected_payment_id}'",
            status_code=403,
        )

    # Non-mutating Agent recommendations are respected directly (NO hidden auto-recovery!)
    if is_non_mutating:
        return proposal, None

    # 3 (Prompt 14.5 Fix 3): Command-specific required target schema validation!
    # NEVER silently substitute anomaly defaults when the Agent omits required targets!
    eff_order_id = proposal.target_order_id or (
        str(proposal.action_params["order_id"]).strip()
        if proposal.action_params.get("order_id")
        else None
    )
    eff_payment_id = proposal.target_payment_id or (
        str(proposal.action_params["payment_id"]).strip()
        if proposal.action_params.get("payment_id")
        else None
    )
    eff_event_id = (
        str(proposal.action_params["event_id"]).strip()
        if proposal.action_params.get("event_id")
        else None
    )
    expected_version = proposal.expected_version
    if expected_version is None:
        raw_v = proposal.action_params.get("expected_version")
        if isinstance(raw_v, int):
            expected_version = raw_v

    if norm_cmd in ("REQUEST_ORDER_SYNC", "REQUEST_PAYMENT_RECONCILIATION"):
        if not eff_order_id:
            raise AppError(
                ErrorCode.FORBIDDEN,
                f"MISSING_REQUIRED_PROPOSAL_TARGET: '{norm_cmd}' requires explicit target_order_id",
                status_code=403,
            )
        if not eff_payment_id:
            raise AppError(
                ErrorCode.FORBIDDEN,
                f"MISSING_REQUIRED_PROPOSAL_TARGET: '{norm_cmd}' requires explicit "
                "target_payment_id",
                status_code=403,
            )
        if expected_version is None:
            raise AppError(
                ErrorCode.FORBIDDEN,
                f"MISSING_EXPECTED_VERSION: '{norm_cmd}' requires explicit expected_version",
                status_code=403,
            )
    elif norm_cmd == "REQUEST_EVENT_REDELIVERY":
        if not eff_event_id and not eff_order_id:
            raise AppError(
                ErrorCode.FORBIDDEN,
                "MISSING_REQUIRED_PROPOSAL_TARGET: 'REQUEST_EVENT_REDELIVERY' requires "
                "explicit event_id or target_order_id",
                status_code=403,
            )

    # 4. Validate supporting_evidence_ids belong to this exact Case + session, are OBSERVED_FACT,
    # are fresh (<= 24h), and do not contradict the target order!
    if not proposal.supporting_evidence_ids:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "MISSING_SUPPORTING_EVIDENCE_IDS: state-changing Agent proposal requires evidence IDs",
            status_code=403,
        )
    ev_rows = list(
        db.scalars(
            select(Evidence).where(
                Evidence.id.in_(proposal.supporting_evidence_ids),
                Evidence.case_id == proposal.case_id,
                Evidence.session_id == binding.session_id,
            )
        ).all()
    )
    valid_obs_ids = {e.id for e in ev_rows if e.kind == EvidenceKind.OBSERVED_FACT.value}
    missing_or_invalid = set(proposal.supporting_evidence_ids) - valid_obs_ids
    if missing_or_invalid:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"INVALID_OR_UNRELATED_EVIDENCE_REFS: evidence IDs {sorted(missing_or_invalid)} "
            f"are missing, belong to another Case/session, or are not OBSERVED_FACT",
            status_code=403,
        )

    freshness_cutoff = datetime.now(UTC) - timedelta(hours=24)
    for ev_row in ev_rows:
        obs_ts = ev_row.observed_at
        if obs_ts is not None:
            if obs_ts.tzinfo is None:
                obs_ts = obs_ts.replace(tzinfo=UTC)
            if obs_ts < freshness_cutoff:
                raise AppError(
                    ErrorCode.FORBIDDEN,
                    f"STALE_SUPPORTING_EVIDENCE: evidence '{ev_row.id}' observed at "
                    f"{obs_ts.isoformat()} exceeds freshness window",
                    status_code=403,
                )
        ev_order = (ev_row.payload_json or {}).get("order_id")
        if eff_order_id and ev_order and str(ev_order).strip() != eff_order_id:
            raise AppError(
                ErrorCode.FORBIDDEN,
                f"EVIDENCE_TARGET_MISMATCH: evidence '{ev_row.id}' references order "
                f"'{ev_order}', not '{eff_order_id}'",
                status_code=403,
            )

    reader = build_connector_for_binding(binding, service_key=read_key, transport=transport)

    if expected_version is not None and obs.affected_order_id:
        try:
            live_resp = reader.get_order(obs.affected_order_id)
            live_order = (
                live_resp.get("data")
                if isinstance(live_resp, dict) and isinstance(live_resp.get("data"), dict)
                else live_resp
            )
        except Exception:
            live_order = None
        if isinstance(live_order, dict) and live_order.get("version") is not None:
            live_ver = int(live_order["version"])
            if live_ver != expected_version:
                raise AppError(
                    ErrorCode.CONFLICT,
                    f"PROPOSAL_STALE_VERSION: proposal expected_version={expected_version} "
                    f"!= authoritative version={live_ver}",
                    status_code=409,
                )

    # Coordinate duplicate workers on the same incident occurrence via stable occurrence idemp_key
    idemp_key = f"AGT-{obs.id[:16]}-OCC{obs.occurrence_count}-{norm_cmd}"[:128]
    exec_res = execute_and_verify_commerce_recovery(
        db,
        binding=binding,
        obs=obs,
        command=norm_cmd,
        idempotency_key=idemp_key,
        expected_version=expected_version,
        approval_id=approval_id,
        require_human_approval=require_human_approval,
        write_key=write_key,
        read_key=read_key,
        transport=transport,
        read_connector=reader,
    )

    if exec_res.verified_recovery:
        cause = f"[EVIDENCE_SUPPORTED:{','.join(sorted(valid_obs_ids)[:3])}] " + (
            proposal.candidate_causes[0] if proposal.candidate_causes else obs.anomaly_type
        )
        record_verified_experience(
            db,
            session_id=obs.session_id,
            merchant_id=obs.merchant_id,
            case_id=proposal.case_id,
            symptom_summary="; ".join(proposal.observed_symptoms) or obs.rationale,
            symptom_tags=[obs.detector_code, obs.anomaly_type, norm_cmd, proposal.proposal_source],
            validated_root_cause=str(cause)[:255],
            evidence_refs=list(proposal.supporting_evidence_ids),
            actions_attempted=[norm_cmd],
            verified_recovery_steps=[f"{norm_cmd} -> VERIFIED_RECOVERY"],
            policy_version=(
                proposal.policy_references[0]
                if proposal.policy_references
                else "FinResolve-Policy-v1.2"
            ),
        )
    return proposal, exec_res
