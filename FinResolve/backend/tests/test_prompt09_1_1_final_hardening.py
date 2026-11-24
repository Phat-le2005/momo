"""Targeted regression tests for Prompt 09.1.1 — Final Supervisor Hardening & Merge Main.

Covers:
1. Real periodic heartbeat thread renewing job lease & worker heartbeat during slow external I/O.
2. Crashed worker stale-lease recovery without duplicate side effects.
3. Fencing enforcement inside write transactions of ActionExecutor, TaskforceManager, Correlator.
4. Atomic LLM budget reservation under concurrency + unknown-usage conservative fallback charge.
5. Cross-process PostgreSQL `llm_concurrency_leases` ceiling enforcement & cleanup.
6. Durable event inbox: duplicate replay (200), payload conflict (409), server-derived provenance,
   normal business event observation (0 Cases/jobs/LLM), world_epoch/session/merchant validation.
7. Running challenge cancellation (`CANCELLING` -> `CANCELLED`) & terminal challenge immutability.
8. Bulk challenge partial resolution (`PARTIALLY_RESOLVED`) when 1 of N linked Cases is verified.
9. Oracle-independent scenario routing (mutating `oracle_truth` does not alter routing).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.ai_core.model_adapter import DeterministicDemoAdapter, ModelInvocationResult
from app.ai_core.telemetry import (
    BudgetExhaustedError,
    JobSupersededError,
    LlmInvocationRecord,
    RuntimeExecutionContext,
    reset_execution_context,
    set_execution_context,
)
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.rate_limit import llm_concurrency_guard
from app.core.session import issue_service_ingestion_token
from app.db.session import get_sessionmaker
from app.incidents.correlator import IncidentCorrelator, seed_incident_taskforce_scenario
from app.incidents.taskforce import TaskforceManager
from app.models.actions import ActionExecution, ActionProposal
from app.models.cases import Case, Evidence
from app.models.enums import ChallengeStatus, EvidenceKind, SupervisorJobStatus
from app.models.incidents import AgentTask, Incident
from app.models.session import DemoSession
from app.models.simulator import SimOrder
from app.models.supervisor import (
    ChallengeRun,
    LlmConcurrencyLease,
    NormalizedEvent,
    SupervisorJob,
    SupervisorState,
    SupervisorWorker,
)
from app.policy.executor import ActionExecutor
from app.simulator.scenarios import ScenarioRegistry
from app.supervisor.service import (
    _build_execution_context,
    drain_session_supervisor_jobs,
    ensure_supervisor_state,
    process_next_supervisor_job,
    recover_stale_supervisor_jobs,
)


class _SlowAdapter(DeterministicDemoAdapter):
    """Deterministic adapter that simulates slow external I/O on step 1."""

    def __init__(self, delay_seconds: float = 1.6) -> None:
        self._delay = delay_seconds
        self._slept = False

    def generate_plan(self, context: Any) -> ModelInvocationResult:
        if not self._slept:
            self._slept = True
            time.sleep(self._delay)
        return super().generate_plan(context)


def test_periodic_heartbeat_renews_lease_during_slow_io(
    client: TestClient,
    active_session: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job whose external call exceeds heartbeat interval stays alive via background renewal."""
    monkeypatch.setenv("SUPERVISOR_JOB_LEASE_SECONDS", "3")
    monkeypatch.setenv("SUPERVISOR_HEARTBEAT_INTERVAL_SECONDS", "0.35")
    monkeypatch.setenv("WORKER_HEARTBEAT_TTL_SECONDS", "3")
    get_settings.cache_clear()

    headers = {"X-Demo-Session": active_session["token"]}
    res = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "SUSPICIOUS_RECEIPT", "preset_id": "NO_LEDGER_TX"},
    )
    assert res.status_code == 202
    ch_id = res.json()["id"]

    # Patch BoundedAgentRuntime to use a slow adapter (1.6s > 0.35s heartbeat interval)
    import app.ai_core.runtime as runtime_mod

    orig_init = runtime_mod.BoundedAgentRuntime.__init__

    def _patched_init(self: Any, db: Any, **kwargs: Any) -> None:
        kwargs.setdefault("adapter", _SlowAdapter(delay_seconds=1.5))
        orig_init(self, db, **kwargs)

    monkeypatch.setattr(runtime_mod.BoundedAgentRuntime, "__init__", _patched_init)

    out = process_next_supervisor_job(
        session_id=active_session["id"],
        worker_id="wrk-hb-test",
    )
    assert out is not None
    assert out["status"] == "COMPLETED"

    maker = get_sessionmaker()
    with maker() as db:
        job = db.scalar(
            select(SupervisorJob).where(
                SupervisorJob.session_id == active_session["id"],
                SupervisorJob.challenge_id == ch_id,
            )
        )
        assert job is not None
        assert job.status == SupervisorJobStatus.COMPLETED.value
        assert job.heartbeat_at is not None and job.started_at is not None
        assert job.heartbeat_at > job.started_at

        wrk = db.get(SupervisorWorker, "wrk-hb-test")
        assert wrk is not None
        assert wrk.last_heartbeat_at >= job.started_at


def test_crashed_worker_recovery_without_duplicate_side_effects(
    client: TestClient,
    active_session: dict[str, str],
) -> None:
    """When a worker crashes and its lease expires, recovery requeues cleanly with no duplicates."""
    headers = {"X-Demo-Session": active_session["token"]}
    res = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "DUPLICATE_REFUND", "preset_id": "LEGITIMATE_PARTIAL"},
    )
    assert res.status_code == 202
    ch_id = res.json()["id"]
    case_id = res.json()["linked_case_ids"][0]

    maker = get_sessionmaker()
    with maker() as db:
        job = db.scalar(
            select(SupervisorJob).where(
                SupervisorJob.session_id == active_session["id"],
                SupervisorJob.challenge_id == ch_id,
            )
        )
        assert job is not None
        job.status = SupervisorJobStatus.RUNNING.value
        job.worker_id = "wrk-crashed-1"
        job.claim_token = "CLM-CRASHED"  # noqa: S105
        job.attempt_count = 1
        job.locked_until = datetime.now(UTC) - timedelta(seconds=10)
        db.commit()

        recovered = recover_stale_supervisor_jobs(db, session_id=active_session["id"])
        assert job.id in recovered

        # Clear retry backoff so recovery can immediately run in test
        db.refresh(job)
        assert job.status == SupervisorJobStatus.QUEUED.value
        job.next_retry_at = None
        db.commit()

    out = process_next_supervisor_job(session_id=active_session["id"], worker_id="wrk-healthy-2")
    assert out is not None
    assert out["status"] == "COMPLETED"

    with maker() as db:
        props = list(
            db.scalars(select(ActionProposal).where(ActionProposal.case_id == case_id)).all()
        )
        assert len(props) == 1


def test_fencing_blocks_stale_worker_at_executor_taskforce_and_correlator(
    client: TestClient,
    active_session: dict[str, str],
) -> None:
    """A stale claim_token MUST be blocked inside the write transaction of ActionExecutor,
    IncidentCorrelator, and TaskforceManager."""
    headers = {
        "X-Demo-Session": active_session["token"],
        "X-Demo-Manager-Token": active_session["manager_token"],
    }
    res = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "DUPLICATE_REFUND", "preset_id": "LEGITIMATE_PARTIAL"},
    )
    assert res.status_code == 202
    case_id = res.json()["linked_case_ids"][0]
    drain_session_supervisor_jobs(active_session["id"], max_jobs=3)

    maker = get_sessionmaker()
    with maker() as db:
        prop = db.scalar(select(ActionProposal).where(ActionProposal.case_id == case_id))
        assert prop is not None
        prop_id = prop.id

    app_res = client.post(
        f"/api/v1/actions/{prop_id}/approve",
        headers=headers,
        json={"reason": "Approve for fencing test"},
    )
    assert app_res.status_code == 200

    # Create a real job with a NEWER claim_token, while our execution context holds a STALE token
    with maker() as db:
        exec_job = db.scalar(
            select(SupervisorJob).where(
                SupervisorJob.session_id == active_session["id"],
                SupervisorJob.job_type == "EXECUTE_APPROVED_ACTION",
            )
        )
        assert exec_job is not None
        exec_job.status = SupervisorJobStatus.RUNNING.value
        exec_job.claim_token = "CLM-NEW-OWNER"  # noqa: S105
        db.commit()
        job_id = exec_job.id
        state = ensure_supervisor_state(db, active_session["id"])
        world_epoch = state.world_epoch

    stale_ctx = RuntimeExecutionContext(
        job_id=job_id,
        session_id=active_session["id"],
        claim_token="CLM-STALE-OLD-OWNER",
        world_epoch=world_epoch,
        check_guard=lambda _s: None,
        before_llm_call=lambda: None,
        record_llm_outcome=lambda _r: None,
        emit_activity=lambda _k, _t, _d=None, _m=None: None,
    )
    tok = set_execution_context(stale_ctx)
    try:
        with maker() as db:
            executor = ActionExecutor(db, active_session["id"])
            with pytest.raises(JobSupersededError):
                executor.execute_proposal(
                    proposal_id=prop_id,
                    idempotency_key="IDEMP-STALE-FENCE-1",
                )
            db.rollback()
            # Zero ActionExecution rows committed!
            exec_cnt = (
                db.scalar(
                    select(func.count(ActionExecution.id)).where(
                        ActionExecution.proposal_id == prop_id
                    )
                )
                or 0
            )
            assert exec_cnt == 0
    finally:
        reset_execution_context(tok)

    # Also test IncidentCorrelator and TaskforceManager fencing
    with maker() as db:
        seed_incident_taskforce_scenario(db, active_session["id"])
        db.commit()
        existing_inc = db.scalar(
            select(Incident).where(Incident.session_id == active_session["id"])
        )
        assert existing_inc is not None
        inc_id = existing_inc.id

    tok2 = set_execution_context(stale_ctx)
    try:
        with maker() as db:
            correlator = IncidentCorrelator(db, active_session["id"])
            with pytest.raises(JobSupersededError):
                correlator.correlate_session_cases()
            db.rollback()

            inc_obj = db.get(Incident, inc_id)
            assert inc_obj is not None
            tf_mgr = TaskforceManager(db, active_session["id"])
            before_tasks = (
                db.scalar(select(func.count(AgentTask.id)).where(AgentTask.incident_id == inc_id))
                or 0
            )
            with pytest.raises(JobSupersededError):
                tf_mgr.assign_and_run_taskforce(inc_obj, force_rerun=True)
            db.rollback()
            after_tasks = (
                db.scalar(select(func.count(AgentTask.id)).where(AgentTask.incident_id == inc_id))
                or 0
            )
            assert after_tasks == before_tasks
    finally:
        reset_execution_context(tok2)


def test_atomic_llm_budget_reservation_and_unknown_usage_charge(
    active_session: dict[str, str],
) -> None:
    """Concurrent calls cannot bypass `llm_budget_calls`, and unknown token usage still charges."""
    maker = get_sessionmaker()
    with maker() as db:
        state = ensure_supervisor_state(db, active_session["id"])
        state.llm_budget_calls = 2
        state.llm_budget_tokens = 2500
        job = SupervisorJob(
            id="JOB-BUDGET-TEST",
            session_id=active_session["id"],
            world_epoch=state.world_epoch,
            job_type="INVESTIGATE_CASE",
            dedupe_key="budget-test",
            status=SupervisorJobStatus.RUNNING.value,
            claim_token="CLM-BUDGET",
            worker_id="wrk-budget",
        )
        db.add(job)
        db.commit()
        world_epoch = state.world_epoch

    ctx = _build_execution_context(
        job_id="JOB-BUDGET-TEST",
        session_id=active_session["id"],
        claim_token="CLM-BUDGET",
        world_epoch=world_epoch,
        worker_id="wrk-budget",
        worker_mode="STANDALONE",
        challenge_id=None,
        case_id=None,
    )

    def _try_reserve() -> str:
        try:
            ctx.before_llm_call()
            return "RESERVED"
        except BudgetExhaustedError:
            return "EXHAUSTED"

    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(lambda _: _try_reserve(), range(5)))

    assert outcomes.count("RESERVED") == 2
    assert outcomes.count("EXHAUSTED") == 3

    # Reconcile both calls with tokens_unknown=True -> must charge conservative fallback tokens
    for _ in range(2):
        ctx.record_llm_outcome(
            LlmInvocationRecord(
                provider="test",
                model_name="test-m",
                succeeded=True,
                tokens_unknown=True,
                total_tokens=None,
            )
        )

    with maker() as db:
        st = db.get(SupervisorState, active_session["id"])
        assert st is not None
        assert st.llm_reserved_calls == 0
        assert st.llm_reserved_tokens == 0
        assert st.llm_attempts_count == 2
        assert st.llm_tokens_unknown_count == 2
        assert st.llm_tokens_used == 1000  # 2 * 500 conservative charge


def test_cross_process_llm_concurrency_leases_enforce_ceiling(
    active_session: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL `llm_concurrency_leases` enforces `max_concurrent_llm_calls` across callers."""
    monkeypatch.setenv("MAX_CONCURRENT_LLM_CALLS", "2")
    get_settings.cache_clear()

    barrier = threading.Barrier(2)
    release_event = threading.Event()
    acquired_count = 0
    lock = threading.Lock()

    def _hold_lease(idx: int) -> None:
        nonlocal acquired_count
        with llm_concurrency_guard(
            session_id=active_session["id"],
            provider="test",
            worker_id=f"wrk-{idx}",
        ):
            with lock:
                acquired_count += 1
            barrier.wait(timeout=5)
            release_event.wait(timeout=5)

    t1 = threading.Thread(target=_hold_lease, args=(1,))
    t2 = threading.Thread(target=_hold_lease, args=(2,))
    t1.start()
    t2.start()

    # Wait until both leases are held
    deadline = time.monotonic() + 5.0
    while acquired_count < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert acquired_count == 2

    # 3rd concurrent caller must be rejected with 429 CONCURRENT_LLM_LIMIT_EXCEEDED
    with pytest.raises(AppError) as exc_info:
        with llm_concurrency_guard(
            session_id=active_session["id"],
            provider="test",
            worker_id="wrk-3",
        ):
            pass
    assert exc_info.value.status_code == 429

    release_event.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    # All leases cleaned up after exit
    maker = get_sessionmaker()
    with maker() as db:
        remaining = db.scalar(select(func.count(LlmConcurrencyLease.id))) or 0
        assert remaining == 0


def test_event_inbox_hardening_idempotency_provenance_and_observation_separation(
    client: TestClient,
    active_session: dict[str, str],
) -> None:
    """Tests Section 6: durable inbox, idempotency, provenance, normal vs anomaly, scope."""
    headers = {"X-Demo-Session": active_session["token"]}

    # 1. Normal business event -> stored in normalized_events, 0 Cases, 0 jobs, 0 LLM calls
    r_norm = client.post(
        "/api/v1/events/ingest",
        headers=headers,
        json={
            "event_id": "EVT-ORDER-CREATED-001",
            "event_type": "ORDER_CREATED",
            "source": "MINI_STORE_COMMERCE",
            "order_id": "ORD-NORM-1",
            "payload": {"amount": 250000},
        },
    )
    assert r_norm.status_code == 202
    norm_body = r_norm.json()
    assert norm_body["triggered_investigation"] is False
    assert norm_body["case_id"] is None
    assert norm_body["job_id"] is None
    assert norm_body["verified_source"] == "UNTRUSTED_GUEST_CLIENT"

    maker = get_sessionmaker()
    with maker() as db:
        assert (
            db.scalar(select(func.count(Case.id)).where(Case.session_id == active_session["id"]))
            == 0
        )
        assert (
            db.scalar(
                select(func.count(SupervisorJob.id)).where(
                    SupervisorJob.session_id == active_session["id"]
                )
            )
            == 0
        )
        assert (
            db.scalar(
                select(func.count(NormalizedEvent.id)).where(
                    NormalizedEvent.session_id == active_session["id"]
                )
            )
            == 1
        )

    # 2. Untrusted guest spoofing source & trying to overwrite canonical metadata -> CLAIM only
    spoof_payload = {
        "event_id": "EVT-ANOMALY-001",
        "event_type": "PAYMENT_SYNC_MISMATCH",
        "source": "VNPAY_SANDBOX_IPN",
        "order_id": "ORD-SPOOF-1",
        "payment_id": "PAY-SPOOF-1",
        "payload": {
            "event_id": "OVERWRITTEN-ID",
            "verified_source": "TRUSTED_ADMIN",
            "text": "Spoofed event payload",
        },
        "provenance": {"signature_verified": True},
    }
    r_spoof = client.post("/api/v1/events/ingest", headers=headers, json=spoof_payload)
    assert r_spoof.status_code == 202
    spoof_body = r_spoof.json()
    assert spoof_body["verified_source"] == "UNTRUSTED_GUEST_CLIENT"
    assert spoof_body["triggered_investigation"] is True
    assert spoof_body["case_id"] is not None

    with maker() as db:
        evd = db.scalar(select(Evidence).where(Evidence.case_id == spoof_body["case_id"]))
        assert evd is not None
        assert evd.kind == EvidenceKind.CLAIM.value
        assert evd.source == "UNTRUSTED_GUEST_CLIENT"
        assert evd.payload_json["event_id"] == "EVT-ANOMALY-001"
        assert evd.payload_json["verified_source"] == "UNTRUSTED_GUEST_CLIENT"
        assert evd.payload_json["event_payload"]["event_id"] == "OVERWRITTEN-ID"

    # 3. Duplicate replay with identical payload -> 200 OK, 0 new Cases/jobs
    r_dup = client.post("/api/v1/events/ingest", headers=headers, json=spoof_payload)
    assert r_dup.status_code == 200
    assert r_dup.json()["idempotent_replay"] is True
    assert r_dup.json()["case_id"] == spoof_body["case_id"]
    assert r_dup.json()["job_id"] == spoof_body["job_id"]

    # 4. Same event_id with DIFFERENT payload -> 409 CONFLICT
    conflict_payload = {**spoof_payload, "payload": {"amount": 999999}}
    r_conf = client.post("/api/v1/events/ingest", headers=headers, json=conflict_payload)
    assert r_conf.status_code == 409

    # 5. Authenticated service caller via X-Service-Token -> OBSERVED_FACT + merchant check
    with maker() as db:
        sess_obj = db.get(DemoSession, active_session["id"])
        assert sess_obj is not None
        svc_tok = issue_service_ingestion_token(
            sess_obj, source="MINI_STORE_COMMERCE", merchant_id="MERCHANT_MOCK_01"
        )

    r_svc = client.post(
        "/api/v1/events/ingest",
        headers={**headers, "X-Service-Token": svc_tok},
        json={
            "event_id": "EVT-SVC-001",
            "event_type": "PAYMENT_SYNC_MISMATCH",
            "source": "MINI_STORE_COMMERCE",
            "merchant_id": "MERCHANT_MOCK_01",
            "order_id": "ORD-SVC-1",
            "payment_id": "PAY-SVC-1",
        },
    )
    assert r_svc.status_code == 202
    svc_body = r_svc.json()
    assert svc_body["verified_source"] == "MINI_STORE_COMMERCE"
    with maker() as db:
        evd_svc = db.scalar(select(Evidence).where(Evidence.case_id == svc_body["case_id"]))
        assert evd_svc is not None
        assert evd_svc.kind == EvidenceKind.OBSERVED_FACT.value

    # 6. Wrong merchant_id, wrong session_id, forged service token, stale world_epoch
    r_bad_merch = client.post(
        "/api/v1/events/ingest",
        headers={**headers, "X-Service-Token": svc_tok},
        json={
            "event_id": "EVT-BAD-MERCH",
            "event_type": "ORDER_CREATED",
            "source": "MINI_STORE_COMMERCE",
            "merchant_id": "OTHER_MERCHANT",
        },
    )
    assert r_bad_merch.status_code == 403

    r_bad_tok = client.post(
        "/api/v1/events/ingest",
        headers={**headers, "X-Service-Token": "svc_MINI_STORE_COMMERCE.MERCHANT_MOCK_01.forged"},
        json={
            "event_id": "EVT-FORGED",
            "event_type": "ORDER_CREATED",
            "source": "MINI_STORE_COMMERCE",
        },
    )
    assert r_bad_tok.status_code == 403

    r_stale_epoch = client.post(
        "/api/v1/events/ingest",
        headers=headers,
        json={
            "event_id": "EVT-STALE-EPOCH",
            "event_type": "ORDER_CREATED",
            "source": "MINI_STORE_COMMERCE",
            "world_epoch": 999,
        },
    )
    assert r_stale_epoch.status_code == 409


def test_running_challenge_cancellation_and_terminal_immutability(
    client: TestClient,
    active_session: dict[str, str],
) -> None:
    """Cancelling a RUNNING challenge enters CANCELLING (not CANCELLED until worker stops),
    and cancelling a COMPLETED challenge never overwrites COMPLETED."""
    headers = {"X-Demo-Session": active_session["token"]}
    res = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "SUSPICIOUS_RECEIPT", "preset_id": "NO_LEDGER_TX"},
    )
    assert res.status_code == 202
    ch_id = res.json()["id"]

    # Simulate RUNNING state
    maker = get_sessionmaker()
    with maker() as db:
        job = db.scalar(select(SupervisorJob).where(SupervisorJob.challenge_id == ch_id))
        ch = db.get(ChallengeRun, ch_id)
        assert job is not None and ch is not None
        job.status = SupervisorJobStatus.RUNNING.value
        ch.status = ChallengeStatus.RUNNING.value
        db.commit()

    cancel_res = client.post(f"/api/v1/challenges/{ch_id}/cancel", headers=headers)
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == ChallengeStatus.CANCELLING.value

    # Now create a second challenge, finish it completely, and verify cancel cannot overwrite
    res2 = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "SUSPICIOUS_RECEIPT", "preset_id": "NO_LEDGER_TX"},
    )
    assert res2.status_code == 202
    ch2_id = res2.json()["id"]
    drain_session_supervisor_jobs(active_session["id"], max_jobs=3)

    with maker() as db:
        ch2 = db.get(ChallengeRun, ch2_id)
        assert ch2 is not None
        final_status_before = ch2.status
        assert final_status_before in (
            ChallengeStatus.BLOCKED_SAFELY.value,
            ChallengeStatus.COMPLETED.value,
        )

    cancel_done = client.post(f"/api/v1/challenges/{ch2_id}/cancel", headers=headers)
    assert cancel_done.status_code == 200
    assert cancel_done.json()["status"] == final_status_before


def test_bulk_challenge_single_case_verification_sets_partially_resolved(
    client: TestClient,
    active_session: dict[str, str],
) -> None:
    """Verifying 1 representative Case in a multi-case bulk Challenge marks PARTIALLY_RESOLVED."""
    headers = {
        "X-Demo-Session": active_session["token"],
        "X-Demo-Manager-Token": active_session["manager_token"],
    }
    res = client.post(
        "/api/v1/challenges",
        headers=headers,
        json={"challenge_type": "BULK_ORDER_SYNC", "parameters": {"affected_orders": 4}},
    )
    assert res.status_code == 202
    ch_id = res.json()["id"]
    rep_case_id = res.json()["generated_refs_json"]["representative_case_id"]
    drain_session_supervisor_jobs(active_session["id"], max_jobs=3)

    maker = get_sessionmaker()
    with maker() as db:
        prop = db.scalar(select(ActionProposal).where(ActionProposal.case_id == rep_case_id))
        assert prop is not None
        prop_id = prop.id

    app_res = client.post(
        f"/api/v1/actions/{prop_id}/approve",
        headers=headers,
        json={"reason": "Approve single representative case in bulk challenge"},
    )
    assert app_res.status_code == 200
    drain_session_supervisor_jobs(active_session["id"], max_jobs=3)

    with maker() as db:
        ch = db.get(ChallengeRun, ch_id)
        assert ch is not None
        assert ch.status == ChallengeStatus.PARTIALLY_RESOLVED.value
        assert ch.safety_verdict == "PARTIALLY_RESOLVED"
        assert ch.outcome_json["verified_cases_count"] == 1
        assert ch.outcome_json["total_cases_count"] == 5  # 4 affected + 1 lookalike
        assert ch.outcome_json["all_linked_cases_resolved"] is False
        # Verify the non-approved orders were NOT mutated
        paid_orders = (
            db.scalar(
                select(func.count(SimOrder.id)).where(
                    SimOrder.session_id == active_session["id"],
                    SimOrder.status == "PAID",
                )
            )
            or 0
        )
        assert paid_orders == 1


def test_scenario_routing_is_oracle_independent(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutating `oracle_truth` inside mega_live generation must NOT alter Incident/Taskforce
    routing (proving runtime routing depends solely on observable causal evidence)."""
    sess_res = client.post("/api/v1/demo/sessions", json={"ttl_minutes": 60, "max_cases": 50})
    assert sess_res.status_code == 201
    session_id = sess_res.json()["session_id"]

    registry = ScenarioRegistry()
    orig_gen = registry._run_mega_live_generate

    def _gen_with_emptied_oracle(db: Any, *, session_id: str, seed: int, order_count: int) -> Any:
        res = orig_gen(db, session_id=session_id, seed=seed, order_count=order_count)
        # Corrupt oracle_truth routing keys completely — runtime must still discover Incidents!
        res["oracle_truth"]["expected_sync_case_ids"] = []
        return res

    monkeypatch.setattr(registry, "_run_mega_live_generate", _gen_with_emptied_oracle)

    maker = get_sessionmaker()
    with maker() as db:
        run_rec = registry.create_run_record(
            db,
            session_id=session_id,
            scenario_id="mega_live",
            seed=42,
        )
        db.commit()
        run = registry.execute_run(
            db,
            run=run_rec,
            order_count=20,
            investigate_selected_exception=False,
        )
        assert run.status == "COMPLETED"
        assert len(run.incident_ids_json) >= 1
        tasks = list(db.scalars(select(AgentTask).where(AgentTask.session_id == session_id)).all())
        assert len(tasks) >= 2
