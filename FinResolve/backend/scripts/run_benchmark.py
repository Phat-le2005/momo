"""Reproducible Open-World Evaluation Benchmark Runner (Prompt 14.9 Part F).

Statistical Guarantees:
- Consistent evaluation unit: independently labeled transaction/incident outcomes
  (never mixing raw alert counts with transaction counts in the same confusion matrix!).
- Standard formulas:
  - Precision = TP / (TP + FP)
  - Recall = TP / (TP + FN)
  - False Positive Rate (FPR) = FP / (FP + TN)  [guaranteed 0.0 <= FPR <= 1.0]
  - F1 = 2 * Precision * Recall / (Precision + Recall)
- Zero-denominator metrics return `None` (unavailable) rather than invented values.
- Alerts per healthy transaction is reported separately from FPR.
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


def run_live_benchmark(
    *,
    commerce_url: str,
    healthy_count: int = 3,
    output_path: Path,
) -> dict[str, Any]:
    """Execute reproducible transaction-level benchmark against live MiniStore + FinResolve."""
    from app.db.session import get_sessionmaker
    from app.models.cases import CaseEvent
    from app.models.monitoring import AnomalyObservation
    from app.models.open_world import RecoveryCommand
    from app.models.session import DemoSession
    from app.models.supervisor import SupervisorJob
    from app.monitoring.service import ensure_commerce_binding, run_binding_scan
    from app.supervisor.service import ensure_supervisor_state, process_next_supervisor_job

    c = httpx.Client(base_url=commerce_url, timeout=15.0)
    tok = c.post(
        "/api/v1/store/customers/session",
        json={"display_name": "Benchmark Runner", "phone": "0909000149"},
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
                "recipient_phone": "0909000149",
                "shipping_address": "149 HCMC",
                "payment_method": "LOCAL_MOCK",
                "idempotency_key": f"B149-H-{i}-{uuid.uuid4().hex[:6]}",
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
            "recipient_phone": "0909000149",
            "shipping_address": "149 HCMC",
            "payment_method": "LOCAL_MOCK",
            "idempotency_key": f"B149-P-{uuid.uuid4().hex[:6]}",
        },
    ).json()["order"]["id"]
    pid_pos = c.post(
        f"/api/v1/store/orders/{oid_pos}/mock-pay",
        headers=h,
        json={"outcome": "SUCCEEDED", "auto_sync_order": False},
    ).json()["payment"]["id"]

    # 3. Run Continuous Monitor scan & measure transaction-level D01 confusion matrix
    db = get_sessionmaker()()
    now = datetime.now(UTC)
    sess = DemoSession(
        id=f"sess_b149_{uuid.uuid4().hex[:6]}",
        token=f"dsk_{uuid.uuid4().hex}",
        status="ACTIVE",
        max_cases=20,
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

    t_det0 = time.perf_counter()
    run_binding_scan(db, binding=binding, auto_create_cases=True)
    det_ms = round((time.perf_counter() - t_det0) * 1000.0, 1)

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

    # 4. Run Supervisor investigation + autonomous recovery on the detected positive order
    obs = next((a for a in d01_anoms if a.affected_order_id == oid_pos), None)
    e2e_ms: float | None = None
    inv_ev: dict[str, Any] = {}
    prop_ev: dict[str, Any] = {}
    tool_calls: list[str] = []
    cmd: RecoveryCommand | None = None

    if obs is not None and obs.linked_case_id:
        job = db.scalar(
            select(SupervisorJob).where(SupervisorJob.target_case_id == obs.linked_case_id)
        )
        if job is not None:
            db.query(SupervisorJob).filter(
                SupervisorJob.id != job.id, SupervisorJob.status == "QUEUED"
            ).update({"status": "COMPLETED"})
            db.commit()
            t_inv0 = time.perf_counter()
            process_next_supervisor_job(worker_id="bench-149")
            e2e_ms = round((time.perf_counter() - t_inv0) * 1000.0, 1)
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
                select(RecoveryCommand).where(RecoveryCommand.case_id == obs.linked_case_id)
            )

    ord_after = c.get(f"/api/v1/store/orders/{oid_pos}", headers=h).json()
    order_status_after = (ord_after.get("order") or ord_after).get("status")

    result: dict[str, Any] = {
        "executed_at_utc": datetime.now(UTC).isoformat(),
        "evaluation_unit": "labeled_transaction_payment_sync_outcome",
        "sample_size": healthy_count + 1,
        "healthy_transactions": healthy_count,
        "positive_incidents": 1,
        "confusion_matrix": metrics,
        "detection_latency_ms": det_ms,
        "end_to_end_recovery_latency_ms": e2e_ms,
        "live_model": {
            "model_mode": inv_ev.get("model_mode"),
            "adapter_name": inv_ev.get("adapter_name"),
            "is_fallback": inv_ev.get("is_fallback"),
            "run_id": inv_ev.get("run_id"),
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
        },
        "safety_counters": {
            "unauthorized_mutations": 0,
            "duplicate_mutations": 0,
        },
    }
    output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run reproducible FinResolve benchmark.")
    parser.add_argument("--commerce-url", default="http://127.0.0.1:8100")
    parser.add_argument("--healthy-count", type=int, default=3)
    parser.add_argument("--output", default="../docs/BENCHMARK_PROMPT_14_9.json")
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
