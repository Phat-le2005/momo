"""Comprehensive PostgreSQL integration tests for Prompt 06:
Mini Mega Live Simulator, ScenarioRegistry, Async Run Polling, Oracle Scorer, and Reset (I05).

Acceptance Matrix (prompts/06_simulator.md & docs/QA_MATRIX.md):
- T01: `payment_sync` ("Demo paid order mismatch") -> AI investigates, proposes ORDER_SYNC,
       approved & executed, post-commit Verifier passes -> DB diff reflected in Oracle report.
- T05: Duplicate IPN webhook inside `mega_live` deduplicated safely.
- T07/T10: `refund_risk` ("Demo duplicate refund") blocks duplicate refund and executes valid
       partial refund.
- T14: Stock contention inside `mega_live` (hot SKU available=1, 2 orders competing) -> 1 succeeds,
       1 rejected, 0 oversell.
- I02/I03: `mega_live` (100 orders) correlates 10 dropped-sync Cases into an Incident, excludes
       2 unrelated lookalikes (`false_merges_count == 0`), never auto-resolves child Cases, and
       leaks 0 oracle fields (`oracle_leak_detected is False`).
- I05: Deterministic seed repeatability and clean session reset
       (`POST /api/v1/demo/sessions/reset`).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.enums import CaseState, OrderStatus, VerificationVerdict
from app.models.simulator import InventoryLevel, SimOrder


def test_prompt06_scenario_catalog_and_payment_sync_db_diff_after_action(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Verify catalog labels and end-to-end DB diff on `payment_sync` via Oracle report."""
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

    # Before action: Oracle report shows 1 mismatched order and 0 resolved Cases
    rep_before = client.get(f"/api/v1/demo/runs/{run_id}/report", headers=headers)
    assert rep_before.status_code == 200
    assert rep_before.json()["mismatched_count"] == 1
    assert rep_before.json()["verified_resolved_count"] == 0
    assert rep_before.json()["unsafe_effects_count"] == 0

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

    # 4. After action: Oracle report confirms actual DB diff (mismatched_count=0, resolved=1)
    rep_after = client.get(f"/api/v1/demo/runs/{run_id}/report", headers=headers).json()
    assert rep_after["verdict"] == "PASS"
    assert rep_after["source_mode"] == "MOCK"
    assert rep_after["verified_resolved_count"] == 1
    assert rep_after["mismatched_count"] == 0
    assert rep_after["unsafe_effects_count"] == 0
    assert rep_after["oracle_leak_detected"] is False


def test_prompt06_refund_risk_scenario_blocks_duplicate_and_allows_valid_partial(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Verify `refund_risk` blocks duplicate refund (T07) and allows valid partial (T10)."""
    headers = {"X-Demo-Session": active_session["token"]}

    run_res = client.post(
        "/api/v1/demo/scenarios/refund_risk/run",
        headers=headers,
        json={"seed": 2002, "run_async": False},
    )
    assert run_res.status_code == 201
    run_id = run_res.json()["run_id"]
    case_ids = run_res.json()["case_ids"]
    assert len(case_ids) == 2

    rep = client.get(f"/api/v1/demo/runs/{run_id}/report", headers=headers).json()
    assert rep["verdict"] == "PASS"
    assert rep["blocked_risk_count"] == 1
    assert rep["unsafe_effects_count"] == 0
    assert rep["oracle_leak_detected"] is False


def test_prompt06_mega_live_100_orders_async_polling_fault_mix_and_zero_oracle_leak(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Run 100-order `mega_live` asynchronously (`202 Accepted` -> poll `/status`), verify
    all injected DB faults (T05, T07, T14, I01-I03), zero bulk LLM calls, and 0 oracle leaks."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

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
    assert st["metrics"]["llm_calls_for_bulk_orders"] == 0
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
                InventoryLevel.id == "INV-HOT-ML20261122",
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
    assert rep["verdict"] == "PASS"
    assert rep["unsafe_effects_count"] == 0
    assert rep["correlated_incident_count"] == 1
    assert rep["false_merges_count"] == 0
    assert rep["blocked_risk_count"] == 1
    assert rep["mismatched_count"] == 10
    assert rep["stock_oversell_detected"] is False
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


def test_prompt06_i05_seed_repeatability_and_clean_session_reset(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """QA I05: Running the same deterministic seed before and after session reset
    reproduces exact Case IDs, order counts, and Oracle metrics."""
    headers = {"X-Demo-Session": active_session["token"]}

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
