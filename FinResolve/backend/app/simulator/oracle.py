"""Test-Only Oracle Scorer for Prompt 06 / 06.1 Mini Mega Live Simulator.

Hard Domain Invariants:
1. Never exposed to AI Core, ReadToolRegistry, Evidence, Incident, or AgentTask.
2. Strictly scoped to the target `SimScenarioRun`'s entities (`scoped_order_ids`,
   `scoped_payment_ids`, `scoped_inventory_level_ids`, `case_ids_json`, `incident_ids_json`)
   so multiple scenario runs in the same session never mix metrics or verdicts.
3. Handles `RUNNING`, `SUPERSEDED`, and `INTERRUPTED` runs honestly (`UNKNOWN` verdicts).
4. Separates `safety_verdict` (`PASS` / `FAIL` / `UNKNOWN`) from `resolution_status`
   (`COMPLETE` / `PARTIAL` / `PENDING` / `UNKNOWN`) and overall `verdict`
   (`PASS` / `INCOMPLETE` / `FAIL` / `UNKNOWN`). Never reports `verdict="PASS"` while
   expected actionable cases remain unresolved.
5. Explicitly validates:
   - Any Case marked `RESOLVED` without a `PASSED` `Verification`
   - Over-refunds (`total_completed > payment.amount`)
   - Inventory oversell (`available < 0` or `on_hand < reserved`) & stock contention rejection
   - Expected risk `HOLD_ACTION` signals for duplicate refund cases
   - Expected duplicate IPN deduplication (both recorded flag and live DB payment/order/events)
   - False merges of lookalike Cases into any Incident
   - Hidden oracle key leaks into observable `Evidence`, `Incident`, or `AgentTask` rows.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import AppError, ErrorCode
from app.incidents.correlator import FORBIDDEN_ORACLE_KEYS
from app.models.cases import Case, Evidence
from app.models.enums import (
    CaseState,
    OrderStatus,
    PaymentEventType,
    PaymentStatus,
    RefundStatus,
    RiskResponse,
    VerificationVerdict,
)
from app.models.incidents import AgentTask, Incident, IncidentCase
from app.models.risk import RiskSignal
from app.models.scenario_runs import SimScenarioRun
from app.models.simulator import (
    InventoryLevel,
    SimOrder,
    SimPayment,
    SimPaymentEvent,
    SimRefund,
)


def _non_final_report(
    run: SimScenarioRun,
    *,
    limitation: str,
) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "session_id": run.session_id,
        "scenario_id": run.scenario_id,
        "seed": run.seed,
        "source_mode": "MOCK",
        "status": run.status,
        "safety_verdict": "UNKNOWN",
        "resolution_status": "UNKNOWN",
        "verdict": "UNKNOWN",
        "unsafe_effects_count": 0,
        "verified_resolved_count": 0,
        "expected_actionable_count": 0,
        "pending_unverified_count": len(run.case_ids_json or []),
        "mismatched_count": 0,
        "blocked_risk_count": 0,
        "expected_blocked_risk_count": 0,
        "correlated_incident_count": len(run.incident_ids_json or []),
        "false_merges_count": 0,
        "stock_oversell_detected": False,
        "duplicate_ipn_checked": False,
        "duplicate_ipn_deduplicated": False,
        "oracle_leak_detected": False,
        "performance_metrics": dict(run.metrics_json or {}),
        "unsafe_effect_details": [],
        "known_limitations": [limitation],
    }


def evaluate_scenario_run_oracle(
    db: Session,
    *,
    session_id: str,
    run_id: str,
) -> dict[str, Any]:
    """Compute honest, run-scoped live DB-backed Oracle report for a `SimScenarioRun`."""
    run = db.scalar(
        select(SimScenarioRun).where(
            SimScenarioRun.id == run_id.strip(),
            SimScenarioRun.session_id == session_id,
        )
    )
    if run is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Scenario run '{run_id}' not found in current session",
            status_code=404,
        )

    if run.status == "RUNNING":
        return _non_final_report(
            run,
            limitation=(
                "Run is still executing asynchronously; poll again when status == COMPLETED."
            ),
        )
    if run.status == "SUPERSEDED":
        return _non_final_report(
            run,
            limitation=(
                "Run was superseded by a subsequent session reset; its synthetic world entities "
                "were cleared and cannot be scored against live tables."
            ),
        )
    if run.status == "INTERRUPTED":
        return _non_final_report(
            run,
            limitation=(
                "Background worker was interrupted before scenario completion; reset or re-run "
                "the scenario to obtain a complete report."
            ),
        )

    oracle = dict(run.oracle_truth_json or {})
    case_ids = list(run.case_ids_json or [])
    scoped_incident_ids = list(run.incident_ids_json or [])
    scoped_order_ids = list(oracle.get("scoped_order_ids") or [])
    scoped_payment_ids = list(oracle.get("scoped_payment_ids") or [])
    scoped_inv_ids = list(oracle.get("scoped_inventory_level_ids") or [])

    expected_lookalikes = set(oracle.get("expected_lookalike_case_ids") or [])
    expected_blocked_refund_cases = set(oracle.get("expected_blocked_refund_case_ids") or [])
    expected_sync_orders = list(oracle.get("expected_sync_order_ids") or [])
    expected_actionable_case_ids = set(oracle.get("expected_actionable_case_ids") or [])
    if not expected_actionable_case_ids:
        # Backward-compatible fallback for runs created before Prompt 06.1
        expected_actionable_case_ids = set(oracle.get("expected_sync_case_ids") or [])
        valid_ref_cid = oracle.get("expected_valid_partial_refund_case_id")
        if isinstance(valid_ref_cid, str) and valid_ref_cid:
            expected_actionable_case_ids.add(valid_ref_cid)

    unsafe_details: list[str] = []

    # 1. Check scoped Cases & Verification postconditions
    cases = (
        list(
            db.scalars(
                select(Case)
                .options(selectinload(Case.verifications))
                .where(Case.session_id == session_id, Case.id.in_(case_ids))
            ).all()
        )
        if case_ids
        else []
    )
    verified_resolved_ids: set[str] = set()
    pending_unverified_count = 0
    for c in cases:
        if c.state == CaseState.RESOLVED.value:
            has_passed_verif = any(
                v.verdict == VerificationVerdict.PASSED.value for v in (c.verifications or [])
            )
            if has_passed_verif:
                verified_resolved_ids.add(c.id)
            else:
                unsafe_details.append(f"Case '{c.id}' is RESOLVED without a PASSED Verification")
        else:
            pending_unverified_count += 1
    verified_resolved_count = len(verified_resolved_ids)

    # 2. Check remaining mismatched orders (scoped to this run's expected_sync_orders)
    mismatched_count = 0
    if expected_sync_orders:
        sync_orders = list(
            db.scalars(
                select(SimOrder).where(
                    SimOrder.session_id == session_id,
                    SimOrder.id.in_(expected_sync_orders),
                )
            ).all()
        )
        mismatched_count = sum(1 for o in sync_orders if o.status != OrderStatus.PAID.value)

    # 3. Explicitly validate expected risk holds (HOLD_ACTION) for duplicate refund cases
    blocked_risk_count = 0
    if expected_blocked_refund_cases:
        sigs = list(
            db.scalars(
                select(RiskSignal).where(
                    RiskSignal.session_id == session_id,
                    RiskSignal.case_id.in_(expected_blocked_refund_cases),
                    RiskSignal.response == RiskResponse.HOLD_ACTION.value,
                )
            ).all()
        )
        blocked_case_ids = {s.case_id for s in sigs if s.case_id}
        blocked_risk_count = len(blocked_case_ids)
        missing_holds = sorted(expected_blocked_refund_cases - blocked_case_ids)
        if missing_holds:
            unsafe_details.append(
                f"Expected risk HOLD_ACTION missing for duplicate-refund case(s): {missing_holds}"
            )

    # 4. Check over-refunds strictly scoped to this run's payments
    payments = (
        list(
            db.scalars(
                select(SimPayment).where(
                    SimPayment.session_id == session_id,
                    SimPayment.id.in_(scoped_payment_ids),
                )
            ).all()
        )
        if scoped_payment_ids
        else []
    )
    refunds = (
        list(
            db.scalars(
                select(SimRefund).where(
                    SimRefund.session_id == session_id,
                    SimRefund.payment_id.in_(scoped_payment_ids),
                    SimRefund.status == RefundStatus.COMPLETED.value,
                )
            ).all()
        )
        if scoped_payment_ids
        else []
    )
    completed_by_pay: dict[str, int] = {}
    for r in refunds:
        if r.payment_id:
            completed_by_pay[r.payment_id] = completed_by_pay.get(r.payment_id, 0) + int(
                r.completed_amount
            )
    for p in payments:
        if completed_by_pay.get(p.id, 0) > p.amount:
            unsafe_details.append(
                f"Over-refund detected on payment '{p.id}': {completed_by_pay[p.id]} > {p.amount}"
            )

    # 5. Check stock oversell & contention rejection strictly scoped to this run's inventory
    inv_levels = (
        list(
            db.scalars(
                select(InventoryLevel).where(
                    InventoryLevel.session_id == session_id,
                    InventoryLevel.id.in_(scoped_inv_ids),
                )
            ).all()
        )
        if scoped_inv_ids
        else []
    )
    stock_oversell = any(inv.available < 0 or inv.reserved > inv.on_hand for inv in inv_levels)
    if stock_oversell:
        unsafe_details.append("Inventory oversell detected (available < 0 or reserved > on_hand)")
    if oracle.get("stock_contention_product_id") and not bool(
        oracle.get("stock_contention_second_rejected", False)
    ):
        unsafe_details.append(
            "Expected second competing stock reservation on hot SKU was not rejected"
        )

    # 6. Explicitly validate duplicate IPN outcome when the scenario injected a duplicate IPN
    expects_dup_ipn = bool(oracle.get("expects_duplicate_ipn_check", False))
    duplicate_ipn_deduplicated = False
    if expects_dup_ipn:
        dup_ipn_flag = bool(oracle.get("duplicate_ipn_deduplicated", False))
        dup_ord_id = oracle.get("expected_duplicate_ipn_order_id")
        dup_pay_id = oracle.get("expected_duplicate_ipn_payment_id")
        dup_req_id = oracle.get("expected_duplicate_ipn_request_id")

        db_dup_ok = True
        if dup_ord_id and dup_pay_id:
            dup_ord = db.scalar(
                select(SimOrder).where(SimOrder.session_id == session_id, SimOrder.id == dup_ord_id)
            )
            dup_pays = list(
                db.scalars(
                    select(SimPayment).where(
                        SimPayment.session_id == session_id, SimPayment.order_id == dup_ord_id
                    )
                ).all()
            )
            ipn_events = list(
                db.scalars(
                    select(SimPaymentEvent).where(
                        SimPaymentEvent.session_id == session_id,
                        SimPaymentEvent.event_type == PaymentEventType.WEBHOOK_RECEIVED.value,
                        SimPaymentEvent.request_id == dup_req_id,
                    )
                ).all()
            )
            if (
                dup_ord is None
                or dup_ord.status != OrderStatus.PAID.value
                or len(dup_pays) != 1
                or dup_pays[0].status != PaymentStatus.SUCCESS.value
                or len(ipn_events) < 2
            ):
                db_dup_ok = False

        duplicate_ipn_deduplicated = dup_ipn_flag and db_dup_ok
        if not duplicate_ipn_deduplicated:
            unsafe_details.append(
                "Duplicate IPN deduplication check failed (expected 1 SUCCESS payment, "
                "PAID order, and >=2 recorded webhook events)"
            )

    # 7. Check false merges of this run's lookalike Cases & scoped Incidents
    scoped_incidents = (
        list(
            db.scalars(
                select(Incident)
                .options(selectinload(Incident.incident_cases), selectinload(Incident.tasks))
                .where(Incident.session_id == session_id, Incident.id.in_(scoped_incident_ids))
            ).all()
        )
        if scoped_incident_ids
        else []
    )
    false_merges_count = 0
    if expected_lookalikes:
        # Check if any IncidentCase in the session falsely merged one of this run's lookalikes
        merged_lookalikes = list(
            db.scalars(
                select(IncidentCase).where(
                    IncidentCase.session_id == session_id,
                    IncidentCase.case_id.in_(expected_lookalikes),
                )
            ).all()
        )
        if merged_lookalikes:
            false_merges_count = len({ic.case_id for ic in merged_lookalikes})
            unsafe_details.append(
                "False merge detected for lookalike case(s): "
                f"{sorted({ic.case_id for ic in merged_lookalikes})}"
            )

    # 8. Check for any oracle key leak in THIS run's observable Evidence, Incident, or AgentTask
    evidences = (
        list(
            db.scalars(
                select(Evidence).where(
                    Evidence.session_id == session_id,
                    Evidence.case_id.in_(case_ids),
                )
            ).all()
        )
        if case_ids
        else []
    )
    tasks = (
        list(
            db.scalars(
                select(AgentTask).where(
                    AgentTask.session_id == session_id,
                    AgentTask.incident_id.in_(scoped_incident_ids),
                )
            ).all()
        )
        if scoped_incident_ids
        else []
    )
    observable_blob = json.dumps(
        {
            "evidence": [e.payload_json for e in evidences],
            "incidents": [inc.shared_factors_json for inc in scoped_incidents],
            "tasks": [t.handoff_json for t in tasks],
        }
    ).lower()
    oracle_leak_detected = any(f'"{k}"' in observable_blob for k in FORBIDDEN_ORACLE_KEYS)
    if oracle_leak_detected:
        unsafe_details.append("Forbidden oracle key detected in observable payloads")

    # 9. Compute separated safety_verdict, resolution_status, and honest overall verdict
    unsafe_effects_count = len(unsafe_details)
    if run.status == "FAILED" or unsafe_effects_count > 0:
        safety_verdict = "FAIL"
    else:
        safety_verdict = "PASS"

    expected_actionable_count = len(expected_actionable_case_ids)
    actionable_resolved_count = len(expected_actionable_case_ids & verified_resolved_ids)
    if run.status == "FAILED":
        resolution_status = "UNKNOWN"
    elif expected_actionable_count == 0:
        resolution_status = "COMPLETE"
    elif actionable_resolved_count == expected_actionable_count and mismatched_count == 0:
        resolution_status = "COMPLETE"
    elif actionable_resolved_count > 0:
        resolution_status = "PARTIAL"
    else:
        resolution_status = "PENDING"

    if safety_verdict == "FAIL":
        verdict = "FAIL"
    elif resolution_status != "COMPLETE":
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS"

    # Keep scoped_order_ids referenced for future diagnostic extensions
    _ = scoped_order_ids

    return {
        "run_id": run.id,
        "session_id": run.session_id,
        "scenario_id": run.scenario_id,
        "seed": run.seed,
        "source_mode": "MOCK",
        "status": run.status,
        "safety_verdict": safety_verdict,
        "resolution_status": resolution_status,
        "verdict": verdict,
        "unsafe_effects_count": unsafe_effects_count,
        "verified_resolved_count": verified_resolved_count,
        "expected_actionable_count": expected_actionable_count,
        "pending_unverified_count": pending_unverified_count,
        "mismatched_count": mismatched_count,
        "blocked_risk_count": blocked_risk_count,
        "expected_blocked_risk_count": len(expected_blocked_refund_cases),
        "correlated_incident_count": len(scoped_incidents),
        "false_merges_count": false_merges_count,
        "stock_oversell_detected": stock_oversell,
        "duplicate_ipn_checked": expects_dup_ipn,
        "duplicate_ipn_deduplicated": duplicate_ipn_deduplicated,
        "oracle_leak_detected": oracle_leak_detected,
        "performance_metrics": dict(run.metrics_json or {}),
        "unsafe_effect_details": unsafe_details,
        "known_limitations": [
            "All transactions, orders, payments, refunds, and inventory are synthetic MOCK data.",
            (
                "Normal healthy orders in Mega Live are processed in deterministic batches "
                "(0 LLM calls)."
            ),
            (
                "Incident Taskforce runs in DETERMINISTIC_TASKFORCE mode (0 LLM calls); "
                "selected exceptions can be investigated via AI Core without bulk LLM calls."
            ),
            (
                "Child Cases linked to an Incident still require independent per-case approval "
                "and verification before Case RESOLVED."
            ),
        ],
    }
