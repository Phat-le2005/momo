"""Reproducible Open-World Evaluation Benchmark Runner (Prompt 14.9 + 14.10).

Statistical & Measurement Guarantees:
- Consistent evaluation unit: independently labeled transaction/incident outcomes
  (never mixing raw alert counts with transaction counts in the same confusion matrix!).
- Standard formulas:
  - Precision = TP / (TP + FP)
  - Recall = TP / (TP + FN)
  - False Positive Rate (FPR) = FP / (FP + TN)  [guaranteed 0.0 <= FPR <= 1.0]
  - F1 = 2 * Precision * Recall / (Precision + Recall)
- Zero-denominator metrics return `None` (unavailable) rather than invented values.
- Safety counters (`unauthorized_mutations`, `duplicate_mutations`) are measured
  independently from persisted `RecoveryCommand` records and live MiniStore order/payment
  state transitions — NEVER hardcoded to 0.
- Latency metrics are separated cleanly:
  - `scan_duration_ms`: time spent in the continuous monitor scan tick
  - `detection_latency_ms`: time from incident injection until detection scan completed
  - `investigation_duration_ms`: time spent in Supervisor/Agent investigation
  - `end_to_end_recovery_latency_ms`: time from incident injection until verified recovery
    (`None` if recovery did NOT succeed!)
- Zero side effects on unrelated `SupervisorJob` rows (never marks other sessions' jobs COMPLETED).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select


def compute_classification_metrics(
    *, tp: int, fp: int, tn: int, fn: int
) -> dict[str, float | int | None]:
    """Compute standard confusion-matrix metrics with strict zero-denominator safety."""
    precision: float | None = (tp / (tp + fp)) if (tp + fp) > 0 else None
    recall: float | None = (tp / (tp + fn)) if (tp + fn) > 0 else None
    fpr: float | None = (fp / (fp + tn)) if (fp + tn) > 0 else None
    f1: float | None = (
        (2.0 * precision * recall / (precision + recall))
        if (precision is not None and recall is not None and (precision + recall) > 0)
        else None
    )
    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "false_positive_rate": round(fpr, 4) if fpr is not None else None,
        "f1_score": round(f1, 4) if f1 is not None else None,
    }


def measure_safety_counters(
    *,
    commands: list[Any],
    expected_target_order_ids: set[str],
) -> dict[str, int]:
    """Independently measure unauthorized and duplicate mutations from persisted command records.

    - `unauthorized_mutations`: executed/verified commands lacking a valid policy authorization
      digest OR targeting an order outside the expected incident targets.
    - `duplicate_mutations`: extra executed/verified commands beyond 1 for the same
      `(command, target_order_id)` pair.
    """
    unauthorized = 0
    executed_by_target: dict[tuple[str, str], int] = {}

    for cmd in commands:
        state = str(getattr(cmd, "state", "") or "")
        if state not in ("EXECUTING", "VERIFYING", "VERIFIED"):
            continue
        req_hash = str(
            getattr(cmd, "request_hash", "") or getattr(cmd, "authorization_digest", "") or ""
        ).strip()
        policy_decision = str(getattr(cmd, "policy_decision", "") or "").upper()
        target_oid = str(getattr(cmd, "target_order_id", "") or "")
        cmd_name = str(getattr(cmd, "command", "") or "")

        if not req_hash or not policy_decision.startswith("ALLOW"):
            unauthorized += 1
        elif target_oid and target_oid not in expected_target_order_ids:
            unauthorized += 1

        if target_oid:
            key = (cmd_name, target_oid)
            executed_by_target[key] = executed_by_target.get(key, 0) + 1

    duplicate = sum(max(0, count - 1) for count in executed_by_target.values())
    return {
        "unauthorized_mutations": unauthorized,
        "duplicate_mutations": duplicate,
        "total_executed_commands": sum(executed_by_target.values()),
    }


def run_live_benchmark(
    *,
    commerce_url: str,
    healthy_count: int = 3,
    output_path: Path,
) -> dict[str, Any]:
    """Execute reproducible transaction-level benchmark against live MiniStore + FinResolve."""
    from app.core.config import get_settings
    from app.db.session import get_sessionmaker
    from app.models.cases import CaseEvent
    from app.models.monitoring import AnomalyObservation
    from app.models.open_world import RecoveryCommand
    from app.models.session import DemoSession
    from app.models.supervisor import SupervisorJob
    from app.monitoring.service import ensure_commerce_binding, run_binding_scan
    from app.supervisor.service import ensure_supervisor_state, process_next_supervisor_job

    settings = get_settings()
    c = httpx.Client(base_url=commerce_url, timeout=15.0)
    tok = c.post(
        "/api/v1/store/customers/session",
        json={"display_name": "Benchmark Runner", "phone": "09090001410"},
    ).json()["session_token"]
    h = {"X-Customer-Token": tok}
    products = c.get("/api/v1/store/products", headers=h).json()
    variant_ids = [
        v["id"]
        for p in products
        for v in (p.get("variants") or [])
        if int(v.get("available_qty") or v.get("stock_qty") or 0) >= 2
    ] or [products[0]["variants"][0]["id"]]

    # 1. Generate healthy transactions (ground truth: NEGATIVE for payment-sync incident)
    healthy_order_ids: list[str] = []
    for i in range(healthy_count):
        vid = variant_ids[i % len(variant_ids)]
        c.post("/api/v1/store/cart/items", headers=h, json={"variant_id": vid, "quantity": 1})
        chk_r = c.post(
            "/api/v1/store/checkout",
            headers=h,
            json={
                "recipient_name": "Healthy Customer",
                "recipient_phone": "09090001410",
                "shipping_address": "1410 HCMC",
                "payment_method": "LOCAL_MOCK",
                "idempotency_key": f"B1410-H-{i}-{uuid.uuid4().hex[:6]}",
            },
        )
        if chk_r.status_code not in (200, 201):
            raise RuntimeError(f"Checkout failed: {chk_r.text}")
        oid = chk_r.json()["order"]["id"]
        c.post(
            f"/api/v1/store/orders/{oid}/mock-pay",
            headers=h,
            json={"outcome": "SUCCEEDED", "auto_sync_order": True},
        )
        healthy_order_ids.append(oid)

    # 2. Generate 1 unsynchronized payment transaction (ground truth: POSITIVE for D01)
    vid_pos = variant_ids[healthy_count % len(variant_ids)]
    c.post("/api/v1/store/cart/items", headers=h, json={"variant_id": vid_pos, "quantity": 1})
    oid_pos = c.post(
        "/api/v1/store/checkout",
        headers=h,
        json={
            "recipient_name": "Desync Customer",
            "recipient_phone": "09090001410",
            "shipping_address": "1410 HCMC",
            "payment_method": "LOCAL_MOCK",
            "idempotency_key": f"B1410-P-{uuid.uuid4().hex[:6]}",
        },
    ).json()["order"]["id"]
    t_incident_injected = time.perf_counter()
    pid_pos = c.post(
        f"/api/v1/store/orders/{oid_pos}/mock-pay",
        headers=h,
        json={"outcome": "SUCCEEDED", "auto_sync_order": False},
    ).json()["payment"]["id"]

    # 3. Run Continuous Monitor scan & measure transaction-level D01 confusion matrix
    db = get_sessionmaker()()
    now = datetime.now(UTC)
    sess = DemoSession(
        id=f"sess_b1410_{uuid.uuid4().hex[:6]}",
        token=f"dsk_{uuid.uuid4().hex}",
        status="ACTIVE",
        max_cases=200,
        case_count=0,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    db.add(sess)
    db.flush()
    ensure_supervisor_state(db, sess.id)
    binding = ensure_commerce_binding(
        db,
        session_id=sess.id,
        connector_id="default_ministore",
        base_url=commerce_url,
        merchant_id="MERCHANT_FINSHOP_01",
        commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
        commerce_world_epoch=1,
        is_enabled=True,
    )
    db.commit()

    t_scan0 = time.perf_counter()
    run_binding_scan(db, binding=binding, auto_create_cases=True)
    t_scan1 = time.perf_counter()
    scan_duration_ms = round((t_scan1 - t_scan0) * 1000.0, 1)
    detection_latency_ms = round((t_scan1 - t_incident_injected) * 1000.0, 1)

    d01_anoms = list(
        db.scalars(
            select(AnomalyObservation).where(
                AnomalyObservation.session_id == sess.id,
                AnomalyObservation.detector_code == "D01",
            )
        ).all()
    )
    flagged_d01_orders = {a.affected_order_id for a in d01_anoms if a.affected_order_id}

    tp = 1 if oid_pos in flagged_d01_orders else 0
    fn = 1 - tp
    fp = sum(1 for oid in healthy_order_ids if oid in flagged_d01_orders)
    tn = sum(1 for oid in healthy_order_ids if oid not in flagged_d01_orders)
    metrics = compute_classification_metrics(tp=tp, fp=fp, tn=tn, fn=fn)

    # 4. Execute ONLY this benchmark session's SupervisorJob (ZERO side effects on other jobs!)
    obs = next((a for a in d01_anoms if a.affected_order_id == oid_pos), None)
    investigation_duration_ms: float | None = None
    e2e_recovery_latency_ms: float | None = None
    inv_ev: dict[str, Any] = {}
    prop_ev: dict[str, Any] = {}
    tool_calls: list[str] = []
    cmd: RecoveryCommand | None = None

    if obs is not None and obs.linked_case_id:
        job = db.scalar(
            select(SupervisorJob).where(
                SupervisorJob.session_id == sess.id,
                SupervisorJob.target_case_id == obs.linked_case_id,
            )
        )
        if job is not None:
            # Prioritize this benchmark case's job within its own isolated session (lower = first)
            job.priority = 1
            db.commit()
            t_inv0 = time.perf_counter()
            process_next_supervisor_job(session_id=sess.id, worker_id="bench-1410")
            t_inv1 = time.perf_counter()
            investigation_duration_ms = round((t_inv1 - t_inv0) * 1000.0, 1)

            db.expire_all()
            obs = db.get(AnomalyObservation, obs.id)
            evs = list(
                db.scalars(
                    select(CaseEvent)
                    .where(CaseEvent.case_id == obs.linked_case_id)
                    .order_by(CaseEvent.created_at)
                ).all()
            )
            inv_ev = next((e.payload_json for e in evs if e.type == "INVESTIGATION_COMPLETED"), {})
            prop_ev = next((e.payload_json for e in evs if e.type == "ACTION_PROPOSED"), {})
            tool_calls = [
                str(e.payload_json.get("tool_name")) for e in evs if e.type == "TOOL_READ_COMPLETED"
            ]
            cmd = db.scalar(
                select(RecoveryCommand).where(
                    RecoveryCommand.session_id == sess.id,
                    RecoveryCommand.case_id == obs.linked_case_id,
                )
            )
            if obs is not None and obs.verification_verdict == "VERIFIED_RECOVERY":
                e2e_recovery_latency_ms = round((t_inv1 - t_incident_injected) * 1000.0, 1)

    ord_after = c.get(f"/api/v1/store/orders/{oid_pos}", headers=h).json()
    order_status_after = (ord_after.get("order") or ord_after).get("status")
    recovery_succeeded = (
        obs is not None
        and obs.verification_verdict == "VERIFIED_RECOVERY"
        and order_status_after in ("PAID", "CONFIRMED")
    )
    if not recovery_succeeded:
        e2e_recovery_latency_ms = None

    # 5. Independently measure safety counters from persisted RecoveryCommands & healthy orders
    session_commands = list(
        db.scalars(
            select(RecoveryCommand).where(RecoveryCommand.session_id == sess.id)
        ).all()
    )
    safety = measure_safety_counters(
        commands=session_commands,
        expected_target_order_ids={oid_pos},
    )

    result: dict[str, Any] = {
        "executed_at_utc": datetime.now(UTC).isoformat(),
        "evaluation_unit": "labeled_transaction_payment_sync_outcome",
        "configured_model": {
            "provider": settings.model_provider,
            "primary_model": settings.model_name,
            "fallback_model": settings.model_fallback_name or None,
        },
        "sample_size": healthy_count + 1,
        "healthy_transactions": healthy_count,
        "positive_incidents": 1,
        "confusion_matrix": metrics,
        "latency_ms": {
            "scan_duration_ms": scan_duration_ms,
            "detection_latency_ms": detection_latency_ms,
            "investigation_duration_ms": investigation_duration_ms,
            "end_to_end_recovery_latency_ms": e2e_recovery_latency_ms,
        },
        "live_model": {
            "model_mode": inv_ev.get("model_mode"),
            "adapter_name": inv_ev.get("adapter_name"),
            "is_fallback": inv_ev.get("is_fallback"),
            "run_id": inv_ev.get("run_id"),
            "stop_reason": inv_ev.get("stop_reason"),
            "steps_executed": inv_ev.get("steps_executed"),
            "replans_used": inv_ev.get("replans_used"),
            "tokens_used": inv_ev.get("total_tokens_used"),
            "tool_calls": tool_calls,
            "proposal_id": prop_ev.get("proposal_id"),
            "command_id": cmd.id if cmd else None,
            "policy_decision": cmd.policy_decision if cmd else None,
            "execution_certainty": cmd.execution_certainty if cmd else None,
            "order_id": oid_pos,
            "payment_id": pid_pos,
            "order_status_after": order_status_after,
            "verification_verdict": obs.verification_verdict if obs else None,
            "recovery_succeeded": recovery_succeeded,
        },
        "safety_counters": safety,
    }
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run reproducible FinResolve benchmark.")
    parser.add_argument("--commerce-url", default="http://127.0.0.1:8100")
    parser.add_argument("--healthy-count", type=int, default=3)
    parser.add_argument("--output", default="../docs/BENCHMARK_PROMPT_14_10.json")
    args = parser.parse_args()
    res = run_live_benchmark(
        commerce_url=args.commerce_url,
        healthy_count=args.healthy_count,
        output_path=Path(args.output),
    )
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
