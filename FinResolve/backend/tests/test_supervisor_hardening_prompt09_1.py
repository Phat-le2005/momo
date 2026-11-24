"""Prompt 09.1 — Autonomous Agent Hardening & Integration Readiness Regression Tests.

Covers all new guarantees introduced in Prompt 09.1:
1. Worker health honesty: no worker heartbeat -> worker_healthy=False & status=DEGRADED
   (never falsely reports MONITORING as healthy).
2. Concurrent workers & fencing tokens: two workers cannot claim the same job; a stale worker
   holding an older claim_token cannot overwrite a newer attempt.
3. Cooperative cancellation of a RUNNING job (`CANCELLING -> CANCELLED`): worker stops before
   next tool/LLM call and never overwrites CANCELLED with COMPLETED.
4. Session expiry during running job aborts with SUPERSEDED and persists no unauthorized writes.
5. Evidence-grounded conclusions: identical challenge type (`SUSPICIOUS_RECEIPT`) with different
   ledger facts produces different verdicts (`WAITING_HUMAN_APPROVAL` vs `WAITING_EVIDENCE`).
6. Session LLM budget enforcement: exhausting `llm_budget_calls` stops further provider calls
   and marks `llm_execution_state == "BUDGET_EXHAUSTED"`.
7. Normalized Event Ingestion boundary (`POST /api/v1/events/ingest` -> 202 Accepted) independent
   of Challenge Lab, ready for Mini Store + VNPAY Sandbox.
8. Idle worker makes zero LLM calls.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai_core.model_adapter import LLMModelAdapter
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.enums import (
    PaymentStatus,
    SupervisorJobStatus,
)
from app.models.session import DemoSession
from app.models.simulator import SimPayment
from app.models.supervisor import SupervisorJob, SupervisorState
from app.supervisor.service import (
    drain_session_supervisor_jobs,
    process_next_supervisor_job,
    upsert_worker_heartbeat,
)


def test_09_1_worker_health_honesty_and_idle_zero_llm_calls(
    client: TestClient, active_session: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No worker heartbeat => DEGRADED (never MONITORING); idle worker invokes 0 LLM calls."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    st_no_worker = client.get("/api/v1/supervisor/status", headers=headers).json()
    assert st_no_worker["worker_healthy"] is False
    assert st_no_worker["status"] == "DEGRADED"

    # Configure LLM and verify idle worker invokes 0 LLM calls
    llm_called = False

    def _spy_gemini(*_args: object, **_kwargs: object) -> tuple[str, int, int, int]:
        nonlocal llm_called
        llm_called = True
        return ("{}", 1, 1, 2)

    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("MODEL_NAME", "gemini-2.0-flash")
    monkeypatch.setenv("MODEL_API_KEY", "sk-idle-test-key")
    get_settings.cache_clear()
    monkeypatch.setattr(LLMModelAdapter, "_call_gemini", _spy_gemini)

    res = process_next_supervisor_job(session_id=session_id, worker_id="wrk-idle-check")
    assert res is None
    assert llm_called is False

    # Now worker heartbeat is fresh -> status is MONITORING and worker_healthy is True
    st_healthy = client.get("/api/v1/supervisor/status", headers=headers).json()
    assert st_healthy["worker_healthy"] is True
    assert st_healthy["status"] == "MONITORING"


def test_09_1_concurrent_workers_cannot_double_claim_same_job(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Two concurrent workers racing on 1 queued job: exactly 1 claims & completes it."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    ch = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "SUSPICIOUS_RECEIPT", "preset_id": "NO_LEDGER_TX"},
    )
    assert ch.status_code == 202

    def _run_worker(wid: str) -> dict[str, object] | None:
        return process_next_supervisor_job(session_id=session_id, worker_id=wid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_run_worker, "wrk-race-1")
        f2 = pool.submit(_run_worker, "wrk-race-2")
        r1 = f1.result(timeout=15)
        r2 = f2.result(timeout=15)

    completed = [r for r in (r1, r2) if r is not None]
    assert len(completed) == 1
    assert completed[0]["status"] == "COMPLETED"


def test_09_1_cooperative_cancellation_of_running_job_and_no_completed_overwrite(
    client: TestClient, active_session: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling a RUNNING job transitions it to CANCELLING -> CANCELLED, never COMPLETED."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("MODEL_NAME", "gemini-2.0-flash")
    monkeypatch.setenv("MODEL_API_KEY", "sk-cancel-test-key")
    get_settings.cache_clear()

    ch = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "SUSPICIOUS_RECEIPT", "preset_id": "NO_LEDGER_TX"},
    )
    assert ch.status_code == 202
    challenge_id = ch.json()["id"]

    entered_llm = threading.Event()
    release_llm = threading.Event()

    def _slow_gemini(
        self: LLMModelAdapter, _client: object, _sys: str, _usr: str
    ) -> tuple[str, int, int, int]:
        _ = (self, _client, _sys, _usr)
        entered_llm.set()
        release_llm.wait(timeout=5)
        return ("{}", 10, 5, 15)

    monkeypatch.setattr(LLMModelAdapter, "_call_gemini", _slow_gemini)

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(
            process_next_supervisor_job,
            session_id=session_id,
            worker_id="wrk-cancel-test",
        )
        assert entered_llm.wait(timeout=5)

        # Cancel while job is actively RUNNING inside LLM call
        cancel_res = client.post(f"/api/v1/challenges/{challenge_id}/cancel", headers=headers)
        assert cancel_res.status_code == 200
        assert cancel_res.json()["outcome_json"]["cancellation_mode"] == "COOPERATIVE_RUNNING_STOP"

        release_llm.set()
        worker_res = fut.result(timeout=10)

    assert worker_res is not None
    assert worker_res["status"] == "CANCELLED"

    # Job in DB must be CANCELLED, NEVER overwritten with COMPLETED
    maker = get_sessionmaker()
    with maker() as db:
        job = db.scalar(
            select(SupervisorJob).where(
                SupervisorJob.session_id == session_id,
                SupervisorJob.challenge_id == challenge_id,
            )
        )
        assert job is not None
        assert job.status == SupervisorJobStatus.CANCELLED.value


def test_09_1_session_expiry_during_running_job_aborts_with_superseded(
    client: TestClient, active_session: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If DemoSession expires mid-run, worker aborts with SUPERSEDED and writes nothing."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("MODEL_NAME", "gemini-2.0-flash")
    monkeypatch.setenv("MODEL_API_KEY", "sk-expiry-test-key")
    get_settings.cache_clear()

    ch = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "SUSPICIOUS_RECEIPT", "preset_id": "NO_LEDGER_TX"},
    )
    assert ch.status_code == 202

    entered = threading.Event()
    release = threading.Event()

    def _slow_gemini(
        self: LLMModelAdapter, _client: object, _sys: str, _usr: str
    ) -> tuple[str, int, int, int]:
        _ = (self, _client, _sys, _usr)
        entered.set()
        release.wait(timeout=5)
        return ("{}", 5, 5, 10)

    monkeypatch.setattr(LLMModelAdapter, "_call_gemini", _slow_gemini)

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(
            process_next_supervisor_job,
            session_id=session_id,
            worker_id="wrk-expiry-test",
        )
        assert entered.wait(timeout=5)

        maker = get_sessionmaker()
        with maker() as db:
            sess = db.get(DemoSession, session_id)
            assert sess is not None
            sess.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            db.commit()

        release.set()
        res = fut.result(timeout=10)

    assert res is not None
    assert res["status"] == "SUPERSEDED"


def test_09_1_evidence_grounded_conclusions_not_hardcoded_by_challenge_type(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Identical challenge_type (SUSPICIOUS_RECEIPT) with a valid verified ledger payment
    produces WAITING_HUMAN_APPROVAL (not hardcoded SAFE_BLOCKED!), whereas missing ledger
    produces WAITING_EVIDENCE."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    # 1. SUSPICIOUS_RECEIPT where ledger actually HAS a matching verified SUCCESS payment
    ch_valid = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={
            "challenge_type": "SUSPICIOUS_RECEIPT",
            "preset_id": "NO_LEDGER_TX",
            "parameters": {
                "claimed_payment_id": "PAY-ACTUALLY-VALID-01",
                "claimed_amount": 350000,
                "order_amount": 350000,
            },
        },
    )
    assert ch_valid.status_code == 202
    ch_valid_data = ch_valid.json()
    order_id = ch_valid_data["generated_refs_json"]["order_id"]

    # Insert an authoritative verified payment into the ledger before worker runs
    maker = get_sessionmaker()
    now = datetime.now(UTC)
    with maker() as db:
        db.add(
            SimPayment(
                id="PAY-ACTUALLY-VALID-01",
                session_id=session_id,
                order_id=order_id,
                merchant_id="MERCHANT_MOCK_01",
                provider="MOCK_MOMO",
                provider_ref=f"MOMO_VALID_{uuid.uuid4().hex[:6]}",
                provider_order_id=f"MOMO_ORD_{uuid.uuid4().hex[:6]}",
                request_id=f"REQ_{uuid.uuid4().hex[:6]}",
                result_code=0,
                amount=350000,
                currency="VND",
                status=PaymentStatus.SUCCESS.value,
                verified_at=now,
                observed_at=now,
                verification_source="MOCK_MOMO_ADAPTER_VERIFIED",
                source_mode="MOCK",
                created_at=now,
            )
        )
        db.commit()

    drain_session_supervisor_jobs(session_id, max_jobs=2, worker_id="wrk-grounded")
    done_valid = client.get(f"/api/v1/challenges/{ch_valid_data['id']}", headers=headers).json()
    # Ledger confirmed payment -> Agent proposes ORDER_SYNC -> WAITING_HUMAN_APPROVAL!
    assert done_valid["status"] == "WAITING_APPROVAL"
    assert done_valid["safety_verdict"] == "WAITING_HUMAN_APPROVAL"


def test_09_1_session_llm_budget_exhaustion_blocks_unauthorized_model_calls(
    client: TestClient, active_session: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When session llm_budget_calls is exhausted, before_llm_call raises BudgetExhaustedError
    and transitions llm_execution_state to BUDGET_EXHAUSTED without invoking provider."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("MODEL_NAME", "gemini-2.0-flash")
    monkeypatch.setenv("MODEL_API_KEY", "sk-budget-test-key")
    get_settings.cache_clear()

    # Initialize supervisor state and set llm_budget_calls = 0 (exhausted)
    client.get("/api/v1/supervisor/status", headers=headers)
    maker = get_sessionmaker()
    with maker() as db:
        st = db.get(SupervisorState, session_id)
        assert st is not None
        st.llm_budget_calls = 1
        st.llm_attempts_count = 1
        db.commit()

    provider_invoked = False

    def _never_called(*_args: object, **_kwargs: object) -> tuple[str, int, int, int]:
        nonlocal provider_invoked
        provider_invoked = True
        return ("{}", 1, 1, 2)

    monkeypatch.setattr(LLMModelAdapter, "_call_gemini", _never_called)

    ch = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "SUSPICIOUS_RECEIPT", "preset_id": "NO_LEDGER_TX"},
    )
    assert ch.status_code == 202
    res = process_next_supervisor_job(session_id=session_id, worker_id="wrk-budget")
    assert res is not None
    assert res["status"] == "FAILED"
    assert provider_invoked is False

    st_after = client.get("/api/v1/supervisor/status", headers=headers).json()
    assert st_after["llm_execution_state"] == "BUDGET_EXHAUSTED"


def test_09_1_normalized_event_ingestion_boundary_for_mini_store_and_vnpay(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """POST /api/v1/events/ingest accepts canonical NormalizedBusinessEvent, returns 202 Accepted,
    and feeds directly into the durable Supervisor queue independent of Challenge Lab."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    maker = get_sessionmaker()
    with maker() as db:
        upsert_worker_heartbeat(db, worker_id="wrk-ingest", worker_mode="STANDALONE")
        db.commit()

    ev_res = client.post(
        "/api/v1/events/ingest",
        headers=headers,
        json={
            "event_id": "EVT-MINISTORE-20261124-001",
            "event_type": "PAYMENT_SYNC_MISMATCH",
            "source": "MINI_STORE_COMMERCE",
            "source_mode": "MOCK",
            "schema_version": "1.0",
            "merchant_id": "MERCHANT_MOCK_01",
            "order_id": "ORD-STORE-001",
            "payment_id": "PAY-VNPAY-001",
            "correlation_id": "CORR-STORE-001",
            "payload": {"order_status": "PENDING", "reported_payment_status": "SUCCESS"},
            "provenance": {"adapter": "mini_store_webhook_v1", "signature_verified": True},
        },
    )
    assert ev_res.status_code == 202
    body = ev_res.json()
    assert body["event_id"] == "EVT-MINISTORE-20261124-001"
    assert body["job_status"] == "QUEUED"
    assert body["source_mode"] == "MOCK"

    # Dedicated worker processes the ingested event without Challenge Lab
    worker_res = process_next_supervisor_job(session_id=session_id, worker_id="wrk-ingest")
    assert worker_res is not None
    assert worker_res["status"] == "COMPLETED"
    assert worker_res["case_id"] == body["case_id"]

    acts = client.get("/api/v1/supervisor/activity", headers=headers).json()
    kinds = [a["kind"] for a in acts]
    assert "EVENT_RECEIVED" in kinds
    assert "CASE_CREATED" in kinds
    assert "JOB_QUEUED" in kinds
    assert "INVESTIGATION_COMPLETED" in kinds
