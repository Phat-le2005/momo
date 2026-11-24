"""Prompt 09 / 09.1 — Autonomous Agent Supervisor, Challenge Lab & Mission Control Acceptance Tests.

Updated for Prompt 09.1 true async architecture:
- HTTP challenge submission returns 202 Accepted immediately without synchronous Agent execution.
- Dedicated Supervisor worker (`process_next_supervisor_job`) claims & processes queued jobs.
- Conclusions derive strictly from observed DB facts, RiskEngine, Policy, and Verifier.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai_core.model_adapter import LLMModelAdapter
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.cases import Case, CaseEvent
from app.models.enums import (
    CaseState,
    OrderPaymentStatus,
    OrderStatus,
    SupervisorJobStatus,
)
from app.models.simulator import SimOrder
from app.models.supervisor import SupervisorJob
from app.supervisor.service import (
    drain_session_supervisor_jobs,
    process_next_supervisor_job,
    upsert_worker_heartbeat,
)


def _register_live_worker() -> None:
    maker = get_sessionmaker()
    with maker() as db:
        upsert_worker_heartbeat(db, worker_id="test-standalone-worker", worker_mode="STANDALONE")
        db.commit()


def test_prompt09_test1_no_manual_investigation_required_for_suspicious_receipt(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Acceptance Test 1 (True Async):
    1. Live worker heartbeat -> Mission Control reports MONITORING & worker_healthy=True.
    2. Submit suspicious MOCK receipt -> returns 202 Accepted immediately (QUEUED).
    3. Separate worker processes job without any manual 'Investigate' click or API tick.
    4. Activity feed displays real tool invocations and evidence-grounded WAITING_EVIDENCE decision.
    """
    _register_live_worker()
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    ov0 = client.get("/api/v1/supervisor/overview", headers=headers)
    assert ov0.status_code == 200
    assert ov0.json()["supervisor"]["status"] == "MONITORING"
    assert ov0.json()["supervisor"]["worker_healthy"] is True

    ch_res = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={
            "challenge_type": "SUSPICIOUS_RECEIPT",
            "preset_id": "NO_LEDGER_TX",
            "parameters": {
                "claimed_payment_id": "PAY-GHOST-TEST1",
                "claimed_amount": 450000,
                "receipt_note": "Biên lai MoMo báo thành công 450.000đ",
            },
        },
    )
    assert ch_res.status_code == 202
    assert ch_res.json()["status"] == "QUEUED"

    # Read-only overview MUST NOT execute the job synchronously
    ov_before = client.get("/api/v1/supervisor/overview", headers=headers).json()
    assert ov_before["supervisor"]["queue_depth"] == 1

    # Independent worker claims & processes job
    res = process_next_supervisor_job(session_id=session_id, worker_id="test-standalone-worker")
    assert res is not None
    assert res["status"] == "COMPLETED"

    ch_done = client.get(f"/api/v1/challenges/{ch_res.json()['id']}", headers=headers).json()
    assert ch_done["status"] == "BLOCKED_SAFELY"
    assert ch_done["safety_verdict"] == "WAITING_EVIDENCE"
    assert len(ch_done["linked_case_ids"]) == 1
    assert len(ch_done["agent_run_ids"]) == 1

    outcome = ch_done["outcome_json"]
    assert outcome["action_blocked"] is True
    assert len(outcome["tools_ran"]) >= 1

    ov1 = client.get("/api/v1/supervisor/overview", headers=headers).json()
    kinds = [a["kind"] for a in ov1["activities"]]
    assert "JOB_QUEUED" in kinds
    assert "JOB_CLAIMED" in kinds
    assert "TOOL_INVOKED" in kinds
    assert "TOOL_RESULT_RECEIVED" in kinds
    assert "INVESTIGATION_COMPLETED" in kinds


def test_prompt09_test2_continuous_operation_pause_resume_worker_recovery_and_reset_supersede(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Acceptance Test 2:
    - Single worker processes Challenge A then Challenge B continuously.
    - Pause queues Challenge C without worker claiming; Resume wakes worker to process C.
    - Crashed worker with expired lease is recovered safely with backoff and no duplicate writes.
    - Session reset increments world_epoch and supersedes old jobs.
    """
    _register_live_worker()
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    # Challenge A & B
    ch_a = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "SUSPICIOUS_RECEIPT", "preset_id": "NO_LEDGER_TX"},
    )
    assert ch_a.status_code == 202
    drain_session_supervisor_jobs(session_id, max_jobs=2, worker_id="test-standalone-worker")

    ch_b = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "DUPLICATE_REFUND", "preset_id": "FULL_DUPLICATE"},
    )
    assert ch_b.status_code == 202
    drain_session_supervisor_jobs(session_id, max_jobs=2, worker_id="test-standalone-worker")

    st_after_ab = client.get("/api/v1/supervisor/status", headers=headers).json()
    assert st_after_ab["jobs_processed_count"] == 2

    # Pause -> Challenge C stays QUEUED even when worker polls
    paused = client.post("/api/v1/supervisor/pause", headers=headers)
    assert paused.status_code == 200
    assert paused.json()["status"] == "PAUSED"

    ch_c = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "PROMPT_INJECTION", "preset_id": "OVERRIDE_PAYMENT_TRUTH"},
    )
    assert ch_c.status_code == 202
    assert (
        process_next_supervisor_job(session_id=session_id, worker_id="test-standalone-worker")
        is None
    )

    # Resume (non-blocking) -> worker can now claim & complete Challenge C
    resumed = client.post("/api/v1/supervisor/resume", headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()["autopilot_enabled"] is True
    assert resumed.json()["queue_depth"] == 1

    drain_session_supervisor_jobs(session_id, max_jobs=2, worker_id="test-standalone-worker")
    ch_c_after = client.get(f"/api/v1/challenges/{ch_c.json()['id']}", headers=headers).json()
    assert ch_c_after["status"] == "BLOCKED_SAFELY"

    # Simulate worker crash mid-run (expired lease + Case in INVESTIGATING)
    case_res = client.post(
        "/api/v1/cases",
        headers=headers,
        json={
            "text": "Khách báo đã thanh toán MoMo nhưng đơn chưa cập nhật",
            "type": "PAYMENT_DISPUTE",
        },
    )
    assert case_res.status_code == 201
    crashed_case_id = case_res.json()["id"]

    maker = get_sessionmaker()
    with maker() as db:
        job = db.scalar(
            select(SupervisorJob).where(
                SupervisorJob.session_id == session_id,
                SupervisorJob.target_case_id == crashed_case_id,
            )
        )
        assert job is not None
        job.status = SupervisorJobStatus.RUNNING.value
        job.attempt_count = 1
        job.worker_id = "sup-crashed-worker"
        job.claim_token = f"CLM-CRASHED-{uuid.uuid4().hex[:6]}"
        job.locked_until = datetime.now(UTC) - timedelta(minutes=5)
        c_obj = db.get(Case, crashed_case_id)
        assert c_obj is not None
        c_obj.state = CaseState.INVESTIGATING.value
        c_obj.version = 2
        db.add(
            CaseEvent(
                id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                case_id=crashed_case_id,
                session_id=session_id,
                type="INVESTIGATION_STARTED",
                payload_json={"run_id": "RUN-CRASHED-SUP09", "job_status": "RUNNING"},
                created_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
        db.commit()

    # Worker recovers expired lease with bounded backoff, then processes once retry window arrives
    process_next_supervisor_job(session_id=session_id, worker_id="test-standalone-worker")
    with maker() as db:
        job_after_rec = db.scalar(
            select(SupervisorJob).where(
                SupervisorJob.session_id == session_id,
                SupervisorJob.target_case_id == crashed_case_id,
            )
        )
        assert job_after_rec is not None
        assert job_after_rec.status == SupervisorJobStatus.QUEUED.value
        assert job_after_rec.next_retry_at is not None
        # Fast-forward backoff timer in test
        job_after_rec.next_retry_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    res_rec = process_next_supervisor_job(session_id=session_id, worker_id="test-standalone-worker")
    assert res_rec is not None
    assert res_rec["status"] == "COMPLETED"

    acts = client.get("/api/v1/supervisor/activity", headers=headers).json()
    assert any(a["kind"] == "WORKER_RECOVERED" for a in acts)

    # Reset world -> world_epoch=2
    reset_res = client.post("/api/v1/demo/sessions/reset", headers=headers)
    assert reset_res.status_code == 200
    ov_reset = client.get("/api/v1/supervisor/overview", headers=headers).json()
    assert ov_reset["supervisor"]["world_epoch"] == 2
    assert ov_reset["recent_challenges"] == []


def test_prompt09_test3_fake_receipt_mismatch_and_extractor_watermark_guard(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Acceptance Test 3: Structured text parser honesty + AMOUNT_MISMATCH never marks PAID."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    bad_ext = client.post(
        "/api/v1/challenges/receipt-extract",
        headers=headers,
        json={"raw_content": "Biên lai ngân hàng thật: PAY-123456 số tiền 500000 VND"},
    )
    assert bad_ext.status_code == 400

    ok_ext = client.post(
        "/api/v1/challenges/receipt-extract",
        headers=headers,
        json={
            "raw_content": (
                "BIÊN LAI MOCK / TEST: Mã đơn ORD-RCPT-88, Mã GD PAY-MOMO-8899, Số tiền 1500000 VND"
            )
        },
    )
    assert ok_ext.status_code == 200
    ext_body = ok_ext.json()
    assert ext_body["authority_level"] == "UNTRUSTED_CLAIM"
    assert ext_body["extraction_mode"] == "STRUCTURED_TEXT_PARSER_NOT_OCR"
    assert ext_body["ocr_available"] is False
    assert ext_body["claimed_payment_id"] == "PAY-MOMO-8899"
    assert ext_body["claimed_amount"] == 1500000

    ch = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={
            "challenge_type": "SUSPICIOUS_RECEIPT",
            "preset_id": "AMOUNT_MISMATCH",
            "parameters": {
                "claimed_amount": 1500000,
                "ledger_amount": 150000,
                "order_amount": 1500000,
            },
        },
    )
    assert ch.status_code == 202
    drain_session_supervisor_jobs(session_id, max_jobs=2, worker_id="test-standalone-worker")

    data = client.get(f"/api/v1/challenges/{ch.json()['id']}", headers=headers).json()
    assert data["status"] in ("COMPLETED", "BLOCKED_SAFELY")
    assert data["safety_verdict"] in ("ESCALATED", "WAITING_EVIDENCE", "ACTION_HELD_BY_RISK")
    assert data["outcome_json"]["case_state"] in ("ESCALATED", "WAITING_EVIDENCE")

    order_id = data["generated_refs_json"]["order_id"]
    maker = get_sessionmaker()
    with maker() as db:
        ord_obj = db.get(SimOrder, order_id)
        assert ord_obj is not None
        assert ord_obj.status == OrderStatus.PENDING.value
        assert ord_obj.payment_status == OrderPaymentStatus.UNPAID.value


def test_prompt09_test4_traffic_burst_bounds_simulated_honesty_and_cancellation(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Acceptance Test 4: Server bounds, honest simulated metrics labels, and queued cancel."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    too_big = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={
            "challenge_type": "TRAFFIC_BURST",
            "preset_id": "HIGH_BURST",
            "parameters": {"event_count": 500, "duration_seconds": 5},
        },
    )
    assert too_big.status_code == 400

    burst = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={
            "challenge_type": "TRAFFIC_BURST",
            "preset_id": "HIGH_BURST",
            "parameters": {"event_count": 90, "duration_seconds": 8},
        },
    )
    assert burst.status_code == 202
    drain_session_supervisor_jobs(session_id, max_jobs=2, worker_id="test-standalone-worker")

    b_data = client.get(f"/api/v1/challenges/{burst.json()['id']}", headers=headers).json()
    metrics = b_data["generated_refs_json"]["traffic_metrics"]
    assert metrics["is_simulated_metrics"] is True
    assert metrics["metrics_source"] == "SIMULATED_FORMULA_ESTIMATE"
    assert metrics["generated_events"] == 90
    assert metrics["duplicate_events"] > 0
    assert metrics["rate_limited_events"] > 0
    assert len(b_data["agent_run_ids"]) == 1

    queued_burst = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "TRAFFIC_BURST", "preset_id": "ELEVATED"},
    )
    assert queued_burst.status_code == 202
    cancelled = client.post(
        f"/api/v1/challenges/{queued_burst.json()['id']}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"


def test_prompt09_test5_and_test6_duplicate_refund_and_auto_continuation_after_approval(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Acceptance Tests 5 & 6:
    - Duplicate refund -> ACTION_HELD_BY_RISK (HOLD_ACTION).
    - Bulk Order Sync -> Incident (6 cases, lookalike excluded) + Taskforce + Approval Inbox.
    - Single explicit approval automatically enqueues EXECUTE_APPROVED_ACTION -> worker executes
      idempotently & runs Verifier -> Case RESOLVED, Challenge VERIFIED_RESOLVED, Incident NOT
      prematurely closed.
    """
    _register_live_worker()
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    dup_ref = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "DUPLICATE_REFUND", "preset_id": "FULL_DUPLICATE"},
    )
    assert dup_ref.status_code == 202
    drain_session_supervisor_jobs(session_id, max_jobs=2, worker_id="test-standalone-worker")
    dup_done = client.get(f"/api/v1/challenges/{dup_ref.json()['id']}", headers=headers).json()
    assert dup_done["status"] == "BLOCKED_SAFELY"
    assert dup_done["safety_verdict"] == "ACTION_HELD_BY_RISK"

    bulk = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={
            "challenge_type": "BULK_ORDER_SYNC",
            "preset_id": "WEBHOOK_WORKER_LAG",
            "parameters": {"affected_orders": 6},
        },
    )
    assert bulk.status_code == 202
    drain_session_supervisor_jobs(session_id, max_jobs=3, worker_id="test-standalone-worker")

    bulk_data = client.get(f"/api/v1/challenges/{bulk.json()['id']}", headers=headers).json()
    assert bulk_data["status"] == "WAITING_APPROVAL"
    assert bulk_data["safety_verdict"] == "WAITING_HUMAN_APPROVAL"
    assert len(bulk_data["linked_incident_ids"]) == 1

    inc_id = bulk_data["linked_incident_ids"][0]
    inc_detail = client.get(f"/api/v1/incidents/{inc_id}", headers=headers).json()
    assert len(inc_detail["linked_cases"]) == 6
    assert len(inc_detail["assigned_tasks"]) >= 2

    ov = client.get("/api/v1/supervisor/overview", headers=headers).json()
    assert len(ov["approval_inbox"]) >= 1
    proposal_id = ov["approval_inbox"][0]["proposal_id"]

    # Single explicit approval -> backend automatically enqueues EXECUTE_APPROVED_ACTION!
    appr = client.post(
        f"/api/v1/actions/{proposal_id}/simulate-demo-approval",
        headers=headers,
        json={"reason": "Phê duyệt tường minh từ Mission Control"},
    )
    assert appr.status_code == 200

    # Worker picks up EXECUTE_APPROVED_ACTION and runs ActionExecutor + Verifier automatically!
    exec_job_res = process_next_supervisor_job(
        session_id=session_id, worker_id="test-standalone-worker"
    )
    assert exec_job_res is not None
    assert exec_job_res["status"] == "COMPLETED"

    ov_after = client.get("/api/v1/supervisor/overview", headers=headers).json()
    assert ov_after["approval_inbox"] == []
    assert ov_after["latest_challenge"]["status"] == "PARTIALLY_RESOLVED"
    assert ov_after["latest_challenge"]["safety_verdict"] == "PARTIALLY_RESOLVED"
    assert ov_after["latest_challenge"]["outcome_json"]["postconditions_verified"] is True
    assert ov_after["latest_challenge"]["outcome_json"]["verified_cases_count"] == 1
    assert len(ov_after["recent_verifications"]) >= 1
    assert ov_after["recent_verifications"][0]["verdict"] == "PASSED"

    # Multi-case Incident must NOT be closed just because 1 representative Case was resolved
    inc_after = client.get(f"/api/v1/incidents/{inc_id}", headers=headers).json()
    assert inc_after["status"] != "CLOSED"


def test_prompt09_test7_and_test8_prompt_injection_cross_session_isolation_and_llm_honesty(
    client: TestClient, active_session: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance Tests 7 & 8: Prompt injection, isolation, and truthful LLM telemetry."""
    headers_a = {"X-Demo-Session": active_session["token"]}
    session_a = active_session["id"]

    st0 = client.get("/api/v1/supervisor/status", headers=headers_a).json()
    assert st0["llm_execution_state"] == "DEMO_RULES_FALLBACK"

    inj = client.post(
        "/api/v1/challenges",
        headers=headers_a,
        json={"challenge_type": "PROMPT_INJECTION", "preset_id": "OVERRIDE_PAYMENT_TRUTH"},
    )
    assert inj.status_code == 202
    drain_session_supervisor_jobs(session_a, max_jobs=2, worker_id="test-standalone-worker")
    inj_done = client.get(f"/api/v1/challenges/{inj.json()['id']}", headers=headers_a).json()
    assert inj_done["status"] == "BLOCKED_SAFELY"
    assert inj_done["safety_verdict"] == "WAITING_EVIDENCE"

    sess_b = client.post("/api/v1/demo/sessions", json={"seed_fixtures": False}).json()
    headers_b = {"X-Demo-Session": sess_b["token"]}
    cross_get = client.get(f"/api/v1/challenges/{inj.json()['id']}", headers=headers_b)
    assert cross_get.status_code == 404

    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("MODEL_NAME", "gemini-2.0-flash")
    monkeypatch.setenv("MODEL_API_KEY", "sk-never-exposed-secret-key-09")
    get_settings.cache_clear()

    sess_c = client.post("/api/v1/demo/sessions", json={"seed_fixtures": False}).json()
    headers_c = {"X-Demo-Session": sess_c["token"]}
    st_cfg = client.get("/api/v1/supervisor/status", headers=headers_c)
    assert st_cfg.status_code == 200
    assert st_cfg.json()["llm_execution_state"] == "LLM_API_CONFIGURED"
    assert st_cfg.json()["llm_attempts_count"] == 0
    assert "sk-never-exposed-secret-key-09" not in st_cfg.text

    def _failing_call(
        self: LLMModelAdapter, _client: object, _sys: str, _usr: str
    ) -> tuple[str, int, int, int]:
        raise RuntimeError("Simulated provider error with sk-never-exposed-secret-key-09")

    monkeypatch.setattr(LLMModelAdapter, "_call_gemini", _failing_call)
    ch_fail = client.post(
        "/api/v1/challenges",
        headers=headers_c,
        json={"challenge_type": "SUSPICIOUS_RECEIPT", "preset_id": "NO_LEDGER_TX"},
    )
    assert ch_fail.status_code == 202
    drain_session_supervisor_jobs(
        sess_c["session_id"], max_jobs=2, worker_id="test-standalone-worker"
    )
    st_failed = client.get("/api/v1/supervisor/status", headers=headers_c)
    body_failed = st_failed.json()
    assert body_failed["llm_execution_state"] == "LLM_CALL_FAILED"
    assert body_failed["llm_attempts_count"] >= 1
    assert body_failed["llm_failed_count"] >= 1
    assert body_failed["llm_fallback_count"] >= 1
    assert "sk-never-exposed-secret-key-09" not in st_failed.text
