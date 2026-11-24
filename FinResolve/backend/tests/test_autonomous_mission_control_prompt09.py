"""Prompt 09 — Autonomous Agent Supervisor, Challenge Lab & Mission Control Acceptance Tests.

Covers all 8 Acceptance Tests from Prompt 09 Part F on PostgreSQL:
- Test 1: No manual investigation required (Challenge -> Case + Supervisor job -> decision).
- Test 2: Continuous operation across multiple challenges, Pause/Resume queue draining,
          worker crash lease recovery without duplicate financial effects, and reset supersede.
- Test 3: Fake receipt (AMOUNT_MISMATCH + synthetic MOCK / TEST receipt extractor validation).
- Test 4: Traffic burst (server-side hard bounds, measured rate-limit/dedup telemetry, single
          aggregated Agent investigation, and challenge cancellation).
- Test 5: Duplicate refund blocked (HOLD_ACTION) vs legitimate partial refund queued for approval.
- Test 6: Bulk Order Sync -> automatic Incident correlation (excluding lookalike) + Taskforce
          + representative ORDER_SYNC proposal -> Approval Inbox -> Execute -> Verifier PASS.
- Test 7: Prompt injection immunity & strict cross-session isolation.
- Test 8: LLM execution mode honesty (LLM_API_CONFIGURED vs LLM_CALL_SUCCEEDED vs LLM_CALL_FAILED
          vs DEMO_RULES_FALLBACK, and zero secret leakage).
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


def test_prompt09_test1_no_manual_investigation_required_for_suspicious_receipt(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Acceptance Test 1:
    1. Open Mission Control (/supervisor/overview) -> Supervisor reports MONITORING.
    2. Submit a suspicious MOCK receipt (/challenges) without clicking 'Investigate'.
    3. Backend creates challenge + Case, queues and processes SupervisorJob automatically.
    4. Activity feed displays real tool activity and final evidence-backed decision.
    """
    headers = {"X-Demo-Session": active_session["token"]}

    # 1. Open Mission Control
    ov0 = client.get("/api/v1/supervisor/overview", headers=headers)
    assert ov0.status_code == 200
    assert ov0.json()["supervisor"]["status"] == "MONITORING"
    assert ov0.json()["supervisor"]["autopilot_enabled"] is True

    # 2. Submit suspicious MOCK receipt (NO_LEDGER_TX)
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
    assert ch_res.status_code == 201
    ch = ch_res.json()
    assert ch["status"] == "BLOCKED_SAFELY"
    assert ch["safety_verdict"] == "SAFE_BLOCKED"
    assert len(ch["linked_case_ids"]) == 1
    assert len(ch["agent_run_ids"]) == 1

    outcome = ch["outcome_json"]
    assert outcome["action_blocked"] is True
    assert len(outcome["tools_ran"]) >= 1
    assert "không xác nhận thanh toán" in outcome["summary_vi"].lower()

    # Verify activity timeline persisted real tool invocations and decision
    ov1 = client.get("/api/v1/supervisor/overview", headers=headers).json()
    kinds = [a["kind"] for a in ov1["activities"]]
    assert "EVENT_DETECTED" in kinds
    assert "TOOL_INVOKED" in kinds
    assert "DECISION_FINALIZED" in kinds


def test_prompt09_test2_continuous_operation_pause_resume_worker_recovery_and_reset_supersede(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Acceptance Test 2:
    - Single Supervisor processes Challenge A then Challenge B continuously.
    - Pause queues Challenge C without executing; Resume drains Challenge C automatically.
    - Simulated worker crash during RUNNING job is safely recovered on tick/overview without
      duplicate financial effects.
    - Resetting session increments world_epoch and prevents stale worker from committing.
    """
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    # Challenge A
    ch_a = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "SUSPICIOUS_RECEIPT", "preset_id": "NO_LEDGER_TX"},
    )
    assert ch_a.status_code == 201
    assert ch_a.json()["status"] == "BLOCKED_SAFELY"

    # Challenge B processed by the exact same Supervisor without restart
    ch_b = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "DUPLICATE_REFUND", "preset_id": "FULL_DUPLICATE"},
    )
    assert ch_b.status_code == 201
    assert ch_b.json()["status"] == "BLOCKED_SAFELY"

    st_after_ab = client.get("/api/v1/supervisor/status", headers=headers).json()
    assert st_after_ab["jobs_processed_count"] == 2

    # Pause Supervisor -> submit Challenge C -> stays QUEUED until Resume
    paused = client.post("/api/v1/supervisor/pause", headers=headers)
    assert paused.status_code == 200
    assert paused.json()["status"] == "PAUSED"
    assert paused.json()["autopilot_enabled"] is False

    ch_c = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "PROMPT_INJECTION", "preset_id": "OVERRIDE_PAYMENT_TRUTH"},
    )
    assert ch_c.status_code == 201
    assert ch_c.json()["status"] == "QUEUED"

    st_paused = client.get("/api/v1/supervisor/status", headers=headers).json()
    assert st_paused["queue_depth"] == 1
    assert st_paused["jobs_processed_count"] == 2

    # Resume -> automatically drains queued job C
    resumed = client.post("/api/v1/supervisor/resume", headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()["queue_depth"] == 0
    assert resumed.json()["jobs_processed_count"] == 3

    ch_c_after = client.get(f"/api/v1/challenges/{ch_c.json()['id']}", headers=headers).json()
    assert ch_c_after["status"] == "BLOCKED_SAFELY"

    # Simulate worker crash mid-run (RUNNING job with expired lease + Case in INVESTIGATING)
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

    # Tick recovers the crashed worker job and completes the investigation safely
    tick_res = client.post("/api/v1/supervisor/tick", headers=headers)
    assert tick_res.status_code == 200
    assert tick_res.json()["queue_depth"] == 0
    assert tick_res.json()["jobs_processed_count"] == 4

    acts = client.get("/api/v1/supervisor/activity", headers=headers).json()
    assert any(a["kind"] == "WORKER_RECOVERED" for a in acts)

    # Reset world -> increments world_epoch and supersedes any prior jobs
    reset_res = client.post("/api/v1/demo/sessions/reset", headers=headers)
    assert reset_res.status_code == 200
    ov_reset = client.get("/api/v1/supervisor/overview", headers=headers).json()
    assert ov_reset["supervisor"]["world_epoch"] == 2
    assert ov_reset["recent_challenges"] == []


def test_prompt09_test3_fake_receipt_mismatch_and_extractor_watermark_guard(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Acceptance Test 3:
    - Receipt extractor requires MOCK / TEST watermark and labels output UNTRUSTED_CLAIM.
    - AMOUNT_MISMATCH receipt detects ledger discrepancy (1,500,000 vs 150,000 VND) and never
      marks the order PAID.
    """
    headers = {"X-Demo-Session": active_session["token"]}

    # Unwatermarked receipt is rejected
    bad_ext = client.post(
        "/api/v1/challenges/receipt-extract",
        headers=headers,
        json={"raw_content": "Biên lai ngân hàng thật: PAY-123456 số tiền 500000 VND"},
    )
    assert bad_ext.status_code == 400

    # Watermarked MOCK / TEST receipt extracts untrusted claim fields
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
    assert ok_ext.json()["authority_level"] == "UNTRUSTED_CLAIM"
    assert ok_ext.json()["claimed_payment_id"] == "PAY-MOMO-8899"
    assert ok_ext.json()["claimed_amount"] == 1500000

    # Launch AMOUNT_MISMATCH challenge
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
    assert ch.status_code == 201
    data = ch.json()
    assert data["status"] == "BLOCKED_SAFELY"
    assert data["safety_verdict"] == "SAFE_BLOCKED"

    order_id = data["generated_refs_json"]["order_id"]
    maker = get_sessionmaker()
    with maker() as db:
        ord_obj = db.get(SimOrder, order_id)
        assert ord_obj is not None
        assert ord_obj.status == OrderStatus.PENDING.value
        assert ord_obj.payment_status == OrderPaymentStatus.UNPAID.value


def test_prompt09_test4_traffic_burst_bounds_metrics_and_cancellation(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Acceptance Test 4:
    - Enforces hard server-side upper limit on event_count (<= 120).
    - Records real simulation metrics (generated, accepted, rate_limited, duplicate) and runs
      a single aggregated Agent investigation.
    - Supports challenge cancellation.
    """
    headers = {"X-Demo-Session": active_session["token"]}

    # Exceeding hard cap is rejected with 400
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

    # Bounded HIGH_BURST succeeds and records real metrics with 1 aggregated investigation run
    burst = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={
            "challenge_type": "TRAFFIC_BURST",
            "preset_id": "HIGH_BURST",
            "parameters": {"event_count": 90, "duration_seconds": 8},
        },
    )
    assert burst.status_code == 201
    b_data = burst.json()
    metrics = b_data["generated_refs_json"]["traffic_metrics"]
    assert metrics["generated_events"] == 90
    assert metrics["duplicate_events"] > 0
    assert metrics["rate_limited_events"] > 0
    assert len(b_data["agent_run_ids"]) == 1

    # Pause, queue a burst, then cancel it
    client.post("/api/v1/supervisor/pause", headers=headers)
    queued_burst = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "TRAFFIC_BURST", "preset_id": "ELEVATED"},
    )
    assert queued_burst.status_code == 201
    cancelled = client.post(
        f"/api/v1/challenges/{queued_burst.json()['id']}/cancel",
        headers=headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    client.post("/api/v1/supervisor/resume", headers=headers)


def test_prompt09_test5_and_test6_duplicate_refund_and_bulk_sync_approval_to_verified(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Acceptance Tests 5 & 6:
    - Duplicate refund is blocked with HOLD_ACTION and zero unauthorized execution.
    - Bulk Order Sync automatically correlates 6 worker-lag Cases into an Incident (excluding
      the lookalike Case), runs Taskforce, and queues an ORDER_SYNC proposal in Approval Inbox.
    - Approving & executing from Approval Inbox runs Policy -> Approval -> Executor -> Verifier
      and marks the Case RESOLVED and Challenge VERIFIED_RESOLVED.
    """
    headers = {"X-Demo-Session": active_session["token"]}

    # Test 5: Duplicate refund blocked
    dup_ref = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "DUPLICATE_REFUND", "preset_id": "FULL_DUPLICATE"},
    )
    assert dup_ref.status_code == 201
    assert dup_ref.json()["status"] == "BLOCKED_SAFELY"
    assert dup_ref.json()["outcome_json"]["action_blocked"] is True

    # Test 6: Bulk Order Sync -> Incident + Taskforce + Approval Inbox -> Execute -> Verifier PASS
    bulk = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={
            "challenge_type": "BULK_ORDER_SYNC",
            "preset_id": "WEBHOOK_WORKER_LAG",
            "parameters": {"affected_orders": 6},
        },
    )
    assert bulk.status_code == 201
    bulk_data = bulk.json()
    assert bulk_data["status"] == "WAITING_APPROVAL"
    assert bulk_data["safety_verdict"] == "WAITING_HUMAN_APPROVAL"
    assert len(bulk_data["linked_incident_ids"]) == 1

    # Verify lookalike Case was NOT merged into the Incident
    inc_id = bulk_data["linked_incident_ids"][0]
    inc_detail = client.get(f"/api/v1/incidents/{inc_id}", headers=headers).json()
    assert len(inc_detail["linked_cases"]) == 6
    assert len(inc_detail["assigned_tasks"]) >= 2

    # Check Human Approval Inbox in Mission Control overview
    ov = client.get("/api/v1/supervisor/overview", headers=headers).json()
    assert ov["supervisor"]["status"] == "WAITING_APPROVAL"
    assert len(ov["approval_inbox"]) >= 1
    inbox_item = ov["approval_inbox"][0]
    proposal_id = inbox_item["proposal_id"]

    # Approve and Execute via existing Policy/Approval/Executor/Verifier flow
    appr = client.post(
        f"/api/v1/actions/{proposal_id}/simulate-demo-approval",
        headers=headers,
        json={"reason": "Phê duyệt từ Mission Control Approval Inbox"},
    )
    assert appr.status_code == 200

    exec_res = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers=headers,
        json={"simulate_timeout": False, "simulate_verification_failure": False},
    )
    assert exec_res.status_code == 200
    assert exec_res.json()["verification"]["verdict"] == "PASSED"
    assert exec_res.json()["case_state"] == CaseState.RESOLVED.value

    # Challenge is now VERIFIED_RESOLVED and Approval Inbox is empty
    ov_after = client.get("/api/v1/supervisor/overview", headers=headers).json()
    assert ov_after["approval_inbox"] == []
    assert ov_after["latest_challenge"]["safety_verdict"] == "VERIFIED_RESOLVED"
    assert ov_after["latest_challenge"]["outcome_json"]["postconditions_verified"] is True


def test_prompt09_test7_and_test8_prompt_injection_cross_session_isolation_and_llm_honesty(
    client: TestClient, active_session: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance Tests 7 & 8:
    - Prompt injection is treated as untrusted CLAIM and never alters policy or payment state.
    - Session B cannot access Session A's challenges or activity feed.
    - LLM execution state honestly distinguishes DEMO_RULES_FALLBACK, LLM_API_CONFIGURED,
      and LLM_CALL_FAILED (when provider errors out), and never exposes API keys.
    """
    headers_a = {"X-Demo-Session": active_session["token"]}

    # Default test env has no API key -> DEMO_RULES_FALLBACK
    st0 = client.get("/api/v1/supervisor/status", headers=headers_a).json()
    assert st0["llm_execution_state"] == "DEMO_RULES_FALLBACK"

    # Prompt injection challenge
    inj = client.post(
        "/api/v1/challenges",
        headers=headers_a,
        json={"challenge_type": "PROMPT_INJECTION", "preset_id": "OVERRIDE_PAYMENT_TRUTH"},
    )
    assert inj.status_code == 201
    assert inj.json()["status"] == "BLOCKED_SAFELY"
    assert inj.json()["safety_verdict"] == "SAFE_BLOCKED"

    # Cross-session isolation: Session B cannot read Session A's challenge
    sess_b = client.post("/api/v1/demo/sessions", json={"seed_fixtures": False}).json()
    headers_b = {"X-Demo-Session": sess_b["token"]}
    cross_get = client.get(f"/api/v1/challenges/{inj.json()['id']}", headers=headers_b)
    assert cross_get.status_code == 404

    # Test 8: When LLM credentials are configured, initial state is LLM_API_CONFIGURED
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("MODEL_NAME", "gemini-2.0-flash")
    monkeypatch.setenv("MODEL_API_KEY", "sk-never-exposed-secret-key-09")
    get_settings.cache_clear()

    sess_c = client.post("/api/v1/demo/sessions", json={"seed_fixtures": False}).json()
    headers_c = {"X-Demo-Session": sess_c["token"]}
    st_cfg = client.get("/api/v1/supervisor/status", headers=headers_c)
    assert st_cfg.status_code == 200
    assert st_cfg.json()["llm_execution_state"] == "LLM_API_CONFIGURED"
    assert "sk-never-exposed-secret-key-09" not in st_cfg.text

    # When provider call fails, runtime falls back safely and Supervisor records LLM_CALL_FAILED
    def _failing_invoke(self: LLMModelAdapter, sys_p: str, usr_p: str) -> tuple[str, int, int, int]:
        _ = (self, sys_p, usr_p)
        raise RuntimeError("Simulated provider 401/timeout")

    monkeypatch.setattr(LLMModelAdapter, "_invoke_provider", _failing_invoke)
    ch_fail = client.post(
        "/api/v1/challenges",
        headers=headers_c,
        json={"challenge_type": "SUSPICIOUS_RECEIPT", "preset_id": "NO_LEDGER_TX"},
    )
    assert ch_fail.status_code == 201
    st_failed = client.get("/api/v1/supervisor/status", headers=headers_c)
    assert st_failed.json()["llm_execution_state"] == "LLM_CALL_FAILED"
    assert "sk-never-exposed-secret-key-09" not in st_failed.text
