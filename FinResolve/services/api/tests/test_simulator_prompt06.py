"""Comprehensive PostgreSQL integration tests for Prompt 06 & Prompt 06.1:
Mini Mega Live Simulator, ScenarioRegistry, Separated Oracle Verdicts, Run Scoping,
Quotas & Worker Recovery, Concurrent Stock Reservation, and Selected-Exception AI.

Acceptance Matrix (prompts/06_simulator.md, Prompt 06.1, docs/QA_MATRIX.md):
- T01: `payment_sync` ("Demo paid order mismatch") -> safety_verdict vs resolution_status,
       AI investigates, proposes ORDER_SYNC, approved & executed, post-commit Verifier passes
       -> Oracle transitions from INCOMPLETE to PASS.
- T05: Duplicate IPN webhook inside `mega_live` deduplicated safely and explicitly verified.
- T07/T10: `refund_risk` ("Demo duplicate refund") blocks duplicate refund (`HOLD_ACTION`) AND
       executes actual approved partial refund (200,000 VND) with independent verification.
- T14: True multi-threaded PostgreSQL concurrent stock reservation on hot SKU (available=1)
       proves exactly 1 winner, all competitors rejected (409), 0 oversell.
- I02/I03: `mega_live` (100 orders) correlates 10 dropped-sync Cases into an Incident, excludes
       2 unrelated lookalikes (`false_merges_count == 0`), labels DETERMINISTIC_TASKFORCE
       honestly, never auto-resolves child Cases, and leaks 0 oracle fields.
- I05: Deterministic seed repeatability, superseded runs after reset, run-scoped reports
       across multiple runs in one session, quotas, active-run lock, and stale-run recovery.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.models.actions import ActionProposal
from app.models.enums import (
    CaseState,
    FulfillmentStatus,
    OrderPaymentStatus,
    OrderStatus,
    RefundStatus,
    VerificationVerdict,
)
from app.models.risk import RiskSignal
from app.models.scenario_runs import SimScenarioRun
from app.models.simulator import (
    InventoryLevel,
    Product,
    SimOrder,
    SimRefund,
    Warehouse,
)
from app.simulator.services import reserve_inventory


@pytest.fixture
def sim_session(client: TestClient) -> dict[str, str]:
    """Create a default guest DemoSession (max_cases=20, matching public API default)."""
    res = client.post("/api/v1/demo/sessions", json={"ttl_minutes": 60})
    assert res.status_code == 201
    data = res.json()
    return {"id": data["session_id"], "token": data["token"]}


def test_prompt06_scenario_catalog_and_payment_sync_db_diff_after_action(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Verify catalog labels, separated safety vs resolution verdicts, and payment_sync DB diff."""
    headers = {"X-Demo-Session": active_session["token"]}

    # 1. Inspect scenario catalog (2 distinct controls + mega_live)
    cat_res = client.get("/api/v1/demo/scenarios")
    assert cat_res.status_code == 200
    catalog = {item["scenario_id"]: item for item in cat_res.json()}
    assert set(catalog.keys()) == {"payment_sync", "refund_risk", "mega_live"}
    assert catalog["payment_sync"]["control_label"] == "Demo paid order mismatch"
    assert catalog["refund_risk"]["control_label"] == "Demo duplicate refund"

    # 2. Run `payment_sync` synchronously
    run_res = client.post(
        "/api/v1/demo/scenarios/payment_sync/run",
        headers=headers,
        json={"seed": 1001, "run_async": False},
    )
    assert run_res.status_code == 201
    run_data = run_res.json()
    run_id = run_data["run_id"]
    assert run_data["status"] == "COMPLETED"
    assert run_data["source_mode"] == "MOCK"
    assert len(run_data["case_ids"]) == 1
    case_id = run_data["case_ids"][0]
    assert run_data["recommended_investigation_case_ids"] == [case_id]

    # Before action: safe (0 unsafe effects), resolution is PENDING -> overall verdict INCOMPLETE
    rep_before = client.get(f"/api/v1/demo/runs/{run_id}/report", headers=headers).json()
    assert rep_before["safety_verdict"] == "PASS"
    assert rep_before["resolution_status"] == "PENDING"
    assert rep_before["verdict"] == "INCOMPLETE"
    assert rep_before["expected_actionable_count"] == 1
    assert rep_before["mismatched_count"] == 1
    assert rep_before["verified_resolved_count"] == 0
    assert rep_before["unsafe_effects_count"] == 0

    # 3. Investigate -> Approve (SIMULATED_DEMO_APPROVAL) -> Execute -> Post-Commit Verify
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=headers)
    assert inv_res.status_code == 200
    prop_id = inv_res.json()["proposed_action_ids"][0]

    appr_res = client.post(
        f"/api/v1/actions/{prop_id}/simulate-demo-approval",
        headers=headers,
        json={"reason": "Phê duyệt mô phỏng cho kịch bản payment_sync"},
    )
    assert appr_res.status_code == 200

    exec_res = client.post(
        f"/api/v1/actions/{prop_id}/execute",
        headers={**headers, "X-Idempotency-Key": f"EXEC-SIM-{run_id}"},
    )
    assert exec_res.status_code == 200
    assert exec_res.json()["case_state"] == CaseState.RESOLVED.value
    assert exec_res.json()["verification"]["verdict"] == VerificationVerdict.PASSED.value

    # 4. After action: Oracle report confirms COMPLETE resolution and overall PASS
    rep_after = client.get(f"/api/v1/demo/runs/{run_id}/report", headers=headers).json()
    assert rep_after["safety_verdict"] == "PASS"
    assert rep_after["resolution_status"] == "COMPLETE"
    assert rep_after["verdict"] == "PASS"
    assert rep_after["source_mode"] == "MOCK"
    assert rep_after["verified_resolved_count"] == 1
    assert rep_after["mismatched_count"] == 0
    assert rep_after["unsafe_effects_count"] == 0
    assert rep_after["oracle_leak_detected"] is False


def test_prompt06_1_refund_risk_approved_partial_execution_and_verification(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Prompt 06.1: `refund_risk` blocks duplicate refund (T07), reports INCOMPLETE before
    partial refund resolution, then executes approved partial refund (200k VND) under Policy,
    Approval, and independent Verifier -> reaches COMPLETE / PASS."""
    headers = {"X-Demo-Session": active_session["token"]}
    mgr_headers = {
        **headers,
        "X-Demo-Manager-Token": active_session["manager_token"],
    }
    session_id = active_session["id"]

    run_res = client.post(
        "/api/v1/demo/scenarios/refund_risk/run",
        headers=headers,
        json={"seed": 2002, "run_async": False},
    )
    assert run_res.status_code == 201
    run_id = run_res.json()["run_id"]
    case_ids = run_res.json()["case_ids"]
    assert len(case_ids) == 2
    elig_case_id = case_ids[0]

    # Before executing valid partial refund: safety PASS, resolution PENDING -> INCOMPLETE
    rep_before = client.get(f"/api/v1/demo/runs/{run_id}/report", headers=headers).json()
    assert rep_before["safety_verdict"] == "PASS"
    assert rep_before["resolution_status"] == "PENDING"
    assert rep_before["verdict"] == "INCOMPLETE"
    assert rep_before["expected_actionable_count"] == 1
    assert rep_before["expected_blocked_risk_count"] == 1
    assert rep_before["blocked_risk_count"] == 1
    assert rep_before["unsafe_effects_count"] == 0
    assert rep_before["oracle_leak_detected"] is False

    # Look up the valid partial refund proposal from DB
    maker = get_sessionmaker()
    with maker() as db:
        prop = db.scalar(
            select(ActionProposal).where(
                ActionProposal.session_id == session_id,
                ActionProposal.case_id == elig_case_id,
            )
        )
        assert prop is not None
        prop_id = prop.id
        target_order_id = str(prop.params_json["order_id"])
        target_payment_id = str(prop.params_json["payment_id"])

    # Approve via authenticated DEMO_MANAGER token & execute under Policy + Verifier
    appr_res = client.post(
        f"/api/v1/actions/{prop_id}/approve",
        headers=mgr_headers,
        json={"reason": "Phê duyệt hoàn tiền một phần 200.000 VND (Prompt 06.1)"},
    )
    assert appr_res.status_code == 200

    exec_res = client.post(
        f"/api/v1/actions/{prop_id}/execute",
        headers={**headers, "X-Idempotency-Key": f"EXEC-PARTIAL-{run_id}"},
    )
    assert exec_res.status_code == 200
    exec_body = exec_res.json()
    assert exec_body["case_state"] == CaseState.RESOLVED.value
    assert exec_body["verification"]["verdict"] == VerificationVerdict.PASSED.value

    # Verify committed PostgreSQL state independently
    with maker() as db:
        refund_row = db.scalar(
            select(SimRefund).where(
                SimRefund.session_id == session_id,
                SimRefund.payment_id == target_payment_id,
                SimRefund.status == RefundStatus.COMPLETED.value,
            )
        )
        assert refund_row is not None
        assert refund_row.completed_amount == 200000
        order_row = db.scalar(
            select(SimOrder).where(
                SimOrder.session_id == session_id,
                SimOrder.id == target_order_id,
            )
        )
        assert order_row is not None
        assert order_row.payment_status == OrderPaymentStatus.PARTIALLY_REFUNDED.value

    # After execution + independent verification: Oracle reports COMPLETE & PASS
    rep_after = client.get(f"/api/v1/demo/runs/{run_id}/report", headers=headers).json()
    assert rep_after["safety_verdict"] == "PASS"
    assert rep_after["resolution_status"] == "COMPLETE"
    assert rep_after["verdict"] == "PASS"
    assert rep_after["verified_resolved_count"] == 1
    assert rep_after["blocked_risk_count"] == 1
    assert rep_after["unsafe_effects_count"] == 0


def test_prompt06_mega_live_100_orders_async_polling_fault_mix_and_zero_oracle_leak(
    client: TestClient, sim_session: dict[str, str]
) -> None:
    """Run 100-order `mega_live` asynchronously (`202 Accepted` -> poll `/status`), verify
    all injected DB faults (T05, T07, T14, I01-I03), honest DETERMINISTIC_TASKFORCE label,
    separated safety vs resolution verdicts, zero bulk LLM calls, and 0 oracle leaks."""
    headers = {"X-Demo-Session": sim_session["token"]}
    session_id = sim_session["id"]

    # 1. Start 100-order Mega Live asynchronously
    run_res = client.post(
        "/api/v1/demo/scenarios/mega_live/run",
        headers=headers,
        json={"seed": 20261122, "order_count": 100, "run_async": True, "reset_before_run": True},
    )
    assert run_res.status_code == 202
    run_id = run_res.json()["run_id"]

    # 2. Poll `/status` (TestClient runs BackgroundTasks before returning, so status is COMPLETED)
    status_res = client.get(f"/api/v1/demo/runs/{run_id}/status", headers=headers)
    assert status_res.status_code == 200
    st = status_res.json()
    assert st["status"] == "COMPLETED"
    assert st["progress_pct"] == 100
    assert st["orders_generated"] == 100
    assert len(st["incident_ids"]) == 1
    # 10 dropped-sync + 1 dup refund + 2 lookalikes = 13 exception Cases out of 100 orders
    assert len(st["case_ids"]) == 13
    assert len(st["recommended_investigation_case_ids"]) == 1
    assert st["investigated_case_ids"] == []
    assert st["metrics"]["llm_calls_for_bulk_orders"] == 0
    assert st["metrics"]["taskforce_mode"] == "DETERMINISTIC_TASKFORCE"
    assert st["metrics"]["healthy_orders_processed_in_batch"] == 84
    assert st["metrics"]["total_run_ms"] >= 1

    # 3. Verify actual PostgreSQL DB state (100 orders, hot SKU available == 0 with 0 oversell)
    maker = get_sessionmaker()
    with maker() as db:
        orders_in_db = list(
            db.scalars(select(SimOrder).where(SimOrder.session_id == session_id)).all()
        )
        assert len(orders_in_db) == 100
        paid_orders = [o for o in orders_in_db if o.status == OrderStatus.PAID.value]
        assert len(paid_orders) >= 85

        inv_hot = db.scalar(
            select(InventoryLevel).where(
                InventoryLevel.session_id == session_id,
                InventoryLevel.id == f"INV-HOT-ML20261122-{session_id[-6:].upper()}",
            )
        )
        assert inv_hot is not None
        assert inv_hot.on_hand == 1
        assert inv_hot.reserved == 1
        assert inv_hot.available == 0

    # 4. Inspect Oracle Report (`GET /api/v1/demo/runs/{run_id}/report`)
    rep_res = client.get(f"/api/v1/demo/runs/{run_id}/report", headers=headers)
    assert rep_res.status_code == 200
    rep = rep_res.json()
    assert rep["source_mode"] == "MOCK"
    # Safe (0 unsafe effects) but 10 dropped-sync cases still unresolved -> INCOMPLETE, never PASS!
    assert rep["safety_verdict"] == "PASS"
    assert rep["resolution_status"] == "PENDING"
    assert rep["verdict"] == "INCOMPLETE"
    assert rep["expected_actionable_count"] == 10
    assert rep["unsafe_effects_count"] == 0
    assert rep["correlated_incident_count"] == 1
    assert rep["false_merges_count"] == 0
    assert rep["blocked_risk_count"] == 1
    assert rep["expected_blocked_risk_count"] == 1
    assert rep["mismatched_count"] == 10
    assert rep["stock_oversell_detected"] is False
    assert rep["duplicate_ipn_checked"] is True
    assert rep["duplicate_ipn_deduplicated"] is True
    assert rep["oracle_leak_detected"] is False

    # 5. Verify cross-session tenant isolation on `/runs/{run_id}/status` and `/report`
    other_sess = client.post("/api/v1/demo/sessions").json()
    other_headers = {"X-Demo-Session": other_sess["token"]}
    assert (
        client.get(f"/api/v1/demo/runs/{run_id}/status", headers=other_headers).status_code == 404
    )
    assert (
        client.get(f"/api/v1/demo/runs/{run_id}/report", headers=other_headers).status_code == 404
    )


def test_prompt06_1_oracle_fails_on_missing_risk_hold_or_broken_duplicate_ipn(
    client: TestClient, sim_session: dict[str, str]
) -> None:
    """Prompt 06.1: Explicitly verify Oracle sets safety_verdict=FAIL if an expected risk
    HOLD_ACTION signal is missing or duplicate IPN state is invalid."""
    headers = {"X-Demo-Session": sim_session["token"]}
    session_id = sim_session["id"]

    run_res = client.post(
        "/api/v1/demo/scenarios/mega_live/run",
        headers=headers,
        json={"seed": 555001, "order_count": 50, "run_async": False, "reset_before_run": True},
    )
    assert run_res.status_code == 201
    run_id = run_res.json()["run_id"]

    # Corrupt DB: remove expected RiskSignal AND corrupt duplicate IPN order back to PENDING
    maker = get_sessionmaker()
    with maker() as db:
        db.execute(delete(RiskSignal).where(RiskSignal.session_id == session_id))
        dup_ord = db.scalar(
            select(SimOrder).where(
                SimOrder.session_id == session_id,
                SimOrder.id == f"ORD-ML555001-{session_id[-6:].upper()}-011",
            )
        )
        assert dup_ord is not None
        dup_ord.status = OrderStatus.PENDING.value
        db.commit()

    rep = client.get(f"/api/v1/demo/runs/{run_id}/report", headers=headers).json()
    assert rep["safety_verdict"] == "FAIL"
    assert rep["verdict"] == "FAIL"
    assert rep["blocked_risk_count"] == 0
    assert rep["duplicate_ipn_checked"] is True
    assert rep["duplicate_ipn_deduplicated"] is False
    assert rep["unsafe_effects_count"] >= 2
    details_joined = " | ".join(rep["unsafe_effect_details"]).lower()
    assert "hold_action" in details_joined
    assert "duplicate ipn" in details_joined


def test_prompt06_1_run_scoped_reports_and_superseded_runs_after_reset(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Prompt 06.1: Two scenario runs in the same session do not mix counts in their reports;
    after session reset, prior runs report status=SUPERSEDED and verdict=UNKNOWN."""
    headers = {"X-Demo-Session": active_session["token"]}

    r_sync = client.post(
        "/api/v1/demo/scenarios/payment_sync/run",
        headers=headers,
        json={"seed": 7101, "run_async": False},
    )
    assert r_sync.status_code == 201
    run_sync_id = r_sync.json()["run_id"]

    r_ref = client.post(
        "/api/v1/demo/scenarios/refund_risk/run",
        headers=headers,
        json={"seed": 7102, "run_async": False},
    )
    assert r_ref.status_code == 201
    run_ref_id = r_ref.json()["run_id"]

    # Reports stay strictly isolated to each run's entities
    rep_sync = client.get(f"/api/v1/demo/runs/{run_sync_id}/report", headers=headers).json()
    rep_ref = client.get(f"/api/v1/demo/runs/{run_ref_id}/report", headers=headers).json()
    assert rep_sync["mismatched_count"] == 1
    assert rep_sync["blocked_risk_count"] == 0
    assert rep_sync["expected_blocked_risk_count"] == 0
    assert rep_ref["mismatched_count"] == 0
    assert rep_ref["blocked_risk_count"] == 1
    assert rep_ref["expected_blocked_risk_count"] == 1

    # Reset session -> prior runs are preserved as SUPERSEDED (never deleted, never mis-scored)
    reset_res = client.post("/api/v1/demo/sessions/reset", headers=headers)
    assert reset_res.status_code == 200
    assert reset_res.json()["deleted_counts"]["superseded_runs"] == 2

    rep_old = client.get(f"/api/v1/demo/runs/{run_sync_id}/report", headers=headers).json()
    assert rep_old["status"] == "SUPERSEDED"
    assert rep_old["safety_verdict"] == "UNKNOWN"
    assert rep_old["resolution_status"] == "UNKNOWN"
    assert rep_old["verdict"] == "UNKNOWN"


def test_prompt06_1_quotas_active_run_guard_safe_repeat_and_worker_recovery(
    client: TestClient, sim_session: dict[str, str]
) -> None:
    """Prompt 06.1: Verify safe repeated-run 409, per-session case quota 429, active-run 409,
    and automatic recovery of stale/interrupted background runs."""
    headers = {"X-Demo-Session": sim_session["token"]}
    session_id = sim_session["id"]

    # 1. Safe repeated-run protection (same scenario + seed without reset -> clean 409, no 500)
    r1 = client.post(
        "/api/v1/demo/scenarios/payment_sync/run",
        headers=headers,
        json={"seed": 8888, "run_async": False},
    )
    assert r1.status_code == 201
    r1_dup = client.post(
        "/api/v1/demo/scenarios/payment_sync/run",
        headers=headers,
        json={"seed": 8888, "run_async": False},
    )
    assert r1_dup.status_code == 409
    assert r1_dup.json()["error"]["code"] == "CONFLICT"

    # 2. Per-session case quota protection (13 + 13 = 26 > default max_cases 20 -> 429)
    ml1 = client.post(
        "/api/v1/demo/scenarios/mega_live/run",
        headers=headers,
        json={"seed": 9001, "order_count": 50, "run_async": False, "reset_before_run": True},
    )
    assert ml1.status_code == 201
    ml2 = client.post(
        "/api/v1/demo/scenarios/mega_live/run",
        headers=headers,
        json={"seed": 9002, "order_count": 50, "run_async": False, "reset_before_run": False},
    )
    assert ml2.status_code == 429
    assert ml2.json()["error"]["code"] == "RATE_LIMITED"

    # 3. Active-run protection: simulate an in-flight RUNNING background run
    maker = get_sessionmaker()
    stuck_id = "RUN-STUCK-WORKER-01"
    with maker() as db:
        now = datetime.now(UTC)
        db.add(
            SimScenarioRun(
                id=stuck_id,
                session_id=session_id,
                scenario_id="mega_live",
                seed=9003,
                status="RUNNING",
                progress_pct=25,
                orders_generated=0,
                case_ids_json=[],
                incident_ids_json=[],
                metrics_json={},
                oracle_truth_json={},
                source_mode="MOCK",
                created_at=now,
                updated_at=now,
            )
        )
        db.commit()

    # Both new run and session reset must be rejected with 409 while run is actively RUNNING
    blocked_run = client.post(
        "/api/v1/demo/scenarios/payment_sync/run",
        headers=headers,
        json={"seed": 9004, "reset_before_run": True},
    )
    assert blocked_run.status_code == 409
    blocked_reset = client.post("/api/v1/demo/sessions/reset", headers=headers)
    assert blocked_reset.status_code == 409

    # 4. Simulate worker interruption (updated_at older than stale timeout) -> auto-recovered!
    with maker() as db:
        stuck_row = db.get(SimScenarioRun, stuck_id)
        assert stuck_row is not None
        stuck_row.updated_at = datetime.now(UTC) - timedelta(minutes=10)
        db.commit()

    st_recovered = client.get(f"/api/v1/demo/runs/{stuck_id}/status", headers=headers)
    assert st_recovered.status_code == 200
    assert st_recovered.json()["status"] == "INTERRUPTED"

    rep_recovered = client.get(f"/api/v1/demo/runs/{stuck_id}/report", headers=headers).json()
    assert rep_recovered["status"] == "INTERRUPTED"
    assert rep_recovered["safety_verdict"] == "UNKNOWN"
    assert rep_recovered["verdict"] == "UNKNOWN"

    # Session is now unblocked and can cleanly reset & run again
    ok_after_recovery = client.post(
        "/api/v1/demo/scenarios/payment_sync/run",
        headers=headers,
        json={"seed": 9005, "reset_before_run": True},
    )
    assert ok_after_recovery.status_code == 201


def test_prompt06_1_true_concurrent_stock_reservation_zero_oversell(
    active_session: dict[str, str],
) -> None:
    """Prompt 06.1 (QA T14): 4 concurrent threads using independent PostgreSQL sessions race
    to reserve the last 1 unit of a hot flash-sale SKU; SELECT FOR UPDATE guarantees exactly 1
    succeeds and 3 fail with 409 CONFLICT, with available == 0 and 0 oversell."""
    session_id = active_session["id"]
    maker = get_sessionmaker()
    wh_id = "WH-CONC-01"
    prod_id = "PROD-CONC-HOT"
    inv_id = "INV-CONC-HOT"
    order_ids = [f"ORD-CONC-{i}" for i in range(4)]

    with maker() as db:
        now = datetime.now(UTC)
        db.add(Warehouse(id=wh_id, session_id=session_id, code="CONC-HCM", name="Kho Concurrent"))
        db.add(
            Product(
                id=prod_id,
                session_id=session_id,
                sku="SKU-CONC-HOT-1",
                name="Flash Sale Item (SAMPLE)",
                price=250000,
                currency="VND",
            )
        )
        db.flush()
        db.add(
            InventoryLevel(
                id=inv_id,
                session_id=session_id,
                warehouse_id=wh_id,
                product_id=prod_id,
                on_hand=1,
                reserved=0,
                version=1,
            )
        )
        for oid in order_ids:
            db.add(
                SimOrder(
                    id=oid,
                    session_id=session_id,
                    customer_name=f"Khách {oid}",
                    customer_phone="0900000000",
                    amount=250000,
                    currency="VND",
                    subtotal=250000,
                    grand_total=250000,
                    status=OrderStatus.PAID.value,
                    payment_status=OrderPaymentStatus.PAID.value,
                    fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
                    version=1,
                    source_mode="MOCK",
                    created_at=now,
                    updated_at=now,
                )
            )
        db.commit()

    barrier = threading.Barrier(len(order_ids))

    def _attempt(oid: str) -> str:
        barrier.wait(timeout=5)
        with maker() as thread_db:
            try:
                reserve_inventory(
                    thread_db,
                    session_id=session_id,
                    order_id=oid,
                    warehouse_id=wh_id,
                    product_id=prod_id,
                    quantity=1,
                )
                thread_db.commit()
                return "SUCCESS"
            except AppError as exc:
                thread_db.rollback()
                assert exc.status_code == 409
                return "CONFLICT"

    with ThreadPoolExecutor(max_workers=len(order_ids)) as pool:
        outcomes = list(pool.map(_attempt, order_ids))

    assert outcomes.count("SUCCESS") == 1
    assert outcomes.count("CONFLICT") == len(order_ids) - 1

    with maker() as db:
        inv = db.get(InventoryLevel, inv_id)
        assert inv is not None
        assert inv.on_hand == 1
        assert inv.reserved == 1
        assert inv.available == 0


def test_prompt06_1_selected_exception_ai_investigation_without_bulk_llm(
    client: TestClient, sim_session: dict[str, str]
) -> None:
    """Prompt 06.1: Running `mega_live` with `investigate_selected_exception=True` investigates
    at most 1 curated exception Case (proposing ORDER_SYNC without executing any financial write),
    keeps `llm_calls_for_bulk_orders == 0`, and labels `DETERMINISTIC_TASKFORCE` honestly."""
    headers = {"X-Demo-Session": sim_session["token"]}

    run_res = client.post(
        "/api/v1/demo/scenarios/mega_live/run",
        headers=headers,
        json={
            "seed": 31337,
            "order_count": 50,
            "run_async": False,
            "reset_before_run": True,
            "investigate_selected_exception": True,
        },
    )
    assert run_res.status_code == 201
    data = run_res.json()
    assert data["metrics"]["llm_calls_for_bulk_orders"] == 0
    assert data["metrics"]["taskforce_mode"] == "DETERMINISTIC_TASKFORCE"
    assert len(data["investigated_case_ids"]) == 1
    assert data["investigated_case_ids"] == data["recommended_investigation_case_ids"]
    assert data["metrics"]["selected_exception_ai_mode"] in ("DEMO_RULES", "LLM_API")

    # Selected Case should now be WAITING_APPROVAL (proposal-only, NEVER auto-executed!)
    selected_cid = data["investigated_case_ids"][0]
    case_res = client.get(f"/api/v1/cases/{selected_cid}", headers=headers)
    assert case_res.status_code == 200
    assert case_res.json()["state"] == CaseState.WAITING_APPROVAL.value

    # Oracle report still shows 10 mismatched orders (no unauthorized writes!)
    rep = client.get(f"/api/v1/demo/runs/{data['run_id']}/report", headers=headers).json()
    assert rep["safety_verdict"] == "PASS"
    assert rep["resolution_status"] == "PENDING"
    assert rep["verdict"] == "INCOMPLETE"
    assert rep["mismatched_count"] == 10
    assert rep["unsafe_effects_count"] == 0


def test_prompt06_i05_seed_repeatability_and_clean_session_reset(
    client: TestClient, sim_session: dict[str, str]
) -> None:
    """QA I05: Running the same deterministic seed before and after session reset
    reproduces exact Case IDs, order counts, and Oracle metrics."""
    headers = {"X-Demo-Session": sim_session["token"]}

    run_1 = client.post(
        "/api/v1/demo/scenarios/mega_live/run",
        headers=headers,
        json={"seed": 424242, "order_count": 60, "run_async": False, "reset_before_run": True},
    )
    assert run_1.status_code == 201
    case_ids_1 = run_1.json()["case_ids"]
    orders_1 = run_1.json()["orders_generated"]

    # Explicit reset endpoint
    reset_res = client.post("/api/v1/demo/sessions/reset", headers=headers)
    assert reset_res.status_code == 200
    assert reset_res.json()["reset_completed"] is True
    assert reset_res.json()["deleted_counts"]["orders"] == 60

    # Replay exact same seed
    run_2 = client.post(
        "/api/v1/demo/scenarios/mega_live/run",
        headers=headers,
        json={"seed": 424242, "order_count": 60, "run_async": False},
    )
    assert run_2.status_code == 201
    assert run_2.json()["orders_generated"] == orders_1
    assert run_2.json()["case_ids"] == case_ids_1
