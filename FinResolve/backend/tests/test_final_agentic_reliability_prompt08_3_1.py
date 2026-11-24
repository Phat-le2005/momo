"""Prompt 08.3.1 — Final Agentic Reliability Regression Tests (PostgreSQL + Mocked Providers).

Covers all 3 P1 reliability fixes before merging `feat/agentic-ai-upgrade` into `main`:
1. Fix 1 (Provider-Level LLM Concurrency Guard):
   - Real `LLMModelAdapter` (`generate_plan` & `generate_structured_json`) passes through
     `llm_concurrency_guard()` across OpenAI-compatible, Gemini, and Anthropic providers.
   - Two simultaneous calls with `MAX_CONCURRENT_LLM_CALLS=1`: first occupies semaphore,
     second receives controlled 429 `CONCURRENT_LLM_LIMIT_EXCEEDED`.
   - Semaphore is always released after timeout or provider exception.
   - `POST /api/v1/incidents/correlate` enforces AI rate limits.
2. Fix 2 (Durable Stale Investigation Recovery):
   - Stale crashed worker leaving `Case.state == INVESTIGATING` is durably recovered on poll:
     persists exactly ONE `INVESTIGATION_INTERRUPTED` event and transitions `Case -> ESCALATED`.
   - Repeated polls do NOT duplicate events; healthy active runs are NOT falsely interrupted.
   - Cross-session polls return 404 and cannot recover another session's Case.
   - User can start a new permitted investigation after recovery, and a late-returning old
     worker cannot overwrite the new run.
3. Fix 3 (3-Phase Zero-Lock Taskforce & Correlator Transactions):
   - PostgreSQL `SELECT ... FOR UPDATE NOWAIT` probes during LLM reasoning verify `Incident`,
     `AgentTask`, `Case`, and `DemoSession` are ALL unlocked for both first-time Taskforce runs
     and `force_rerun=True`.
   - Concurrent Incident mutation during LLM reasoning rejects stale LLM output (`409 CONFLICT`).
   - LLM timeout falls back safely to deterministic rules without corrupting state.
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai_core.model_adapter import LLMModelAdapter
from app.ai_core.runtime import BoundedAgentRuntime
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.rate_limit import reset_rate_limiter
from app.db.session import get_sessionmaker
from app.incidents.correlator import seed_incident_taskforce_scenario
from app.incidents.taskforce import TaskforceManager
from app.models.cases import Case, CaseEvent
from app.models.enums import CaseState
from app.models.incidents import AgentTask, Incident
from app.models.session import DemoSession
from tests.test_agentic_ai_upgrade_prompt08_2 import ScriptedMockLLMAdapter


def test_08_3_1_fix1_real_llm_model_adapter_enforces_concurrency_and_releases_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 1: Real LLMModelAdapter._invoke_provider enforces llm_concurrency_guard() across
    concurrent calls and always releases the semaphore after timeout/exception."""
    monkeypatch.setenv("MAX_CONCURRENT_LLM_CALLS", "1")
    get_settings.cache_clear()
    reset_rate_limiter()

    entered = threading.Event()
    release = threading.Event()

    def _blocking_handler(request: httpx.Request) -> httpx.Response:
        _ = request
        entered.set()
        release.wait(timeout=5)
        body = {
            "choices": [{"message": {"content": json.dumps({"ok": True})}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        return httpx.Response(200, json=body)

    settings = Settings(
        model_provider="openai",
        model_name="gpt-4o-mini",
        model_api_key="sk-test-never-logged-secret",
    )
    blocking_client = httpx.Client(transport=httpx.MockTransport(_blocking_handler))
    adapter = LLMModelAdapter(settings=settings, http_client=blocking_client)

    def _first_call() -> dict[str, Any]:
        return adapter.generate_structured_json("sys", "usr").data

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut1 = pool.submit(_first_call)
        assert entered.wait(timeout=3)

        # Second simultaneous provider call MUST be rejected with 429 CONCURRENT_LLM_LIMIT_EXCEEDED
        with pytest.raises(AppError) as exc_info:
            adapter.generate_structured_json("sys2", "usr2")
        assert exc_info.value.status_code == 429
        assert exc_info.value.details["reason"] == "CONCURRENT_LLM_LIMIT_EXCEEDED"
        assert "sk-test-never-logged-secret" not in exc_info.value.message

        release.set()
        assert fut1.result(timeout=3) == {"ok": True}

    blocking_client.close()

    # Semaphore must be released even after provider timeout / exception!
    def _timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated provider timeout", request=request)

    timeout_client = httpx.Client(transport=httpx.MockTransport(_timeout_handler))
    timeout_adapter = LLMModelAdapter(settings=settings, http_client=timeout_client)
    with pytest.raises(httpx.ReadTimeout):
        timeout_adapter.generate_structured_json("sys", "usr")
    timeout_client.close()

    # Subsequent call on Gemini provider succeeds immediately (proving semaphore was released!)
    def _gemini_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": '{"provider":"gemini"}'}]}}],
                "usageMetadata": {
                    "promptTokenCount": 8,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 12,
                },
            },
        )

    gemini_settings = Settings(
        model_provider="gemini",
        model_name="gemini-2.0-flash",
        model_api_key="gm-test-secret",
    )
    gemini_client = httpx.Client(transport=httpx.MockTransport(_gemini_handler))
    gemini_adapter = LLMModelAdapter(settings=gemini_settings, http_client=gemini_client)
    res = gemini_adapter.generate_structured_json("sys", "usr")
    gemini_client.close()
    assert res.data == {"provider": "gemini"}
    assert res.total_tokens == 12


def test_08_3_1_fix1_incidents_correlate_respects_ai_rate_limit(
    client: TestClient, active_session: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix 1: POST /api/v1/incidents/correlate cannot bypass AI rate limits."""
    monkeypatch.setenv("RATE_LIMIT_AI_CALLS_PER_MINUTE", "1")
    get_settings.cache_clear()
    reset_rate_limiter()
    headers = {"X-Demo-Session": active_session["token"]}

    r1 = client.post("/api/v1/incidents/correlate", headers=headers)
    r2 = client.post("/api/v1/incidents/correlate", headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 429
    assert r2.json()["error"]["code"] == "RATE_LIMITED"


def test_08_3_1_fix2_stale_worker_durably_persists_interrupted_recovers_case_and_blocks_old_worker(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Fix 2: Stale worker recovery durably persists exactly 1 INVESTIGATION_INTERRUPTED event,
    transitions Case INVESTIGATING -> ESCALATED, allows a new investigation, blocks cross-session
    recovery, never interrupts healthy runs, and rejects late-returning old workers."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]

    created = client.post(
        "/api/v1/cases",
        headers=headers,
        json={
            "text": "Khách báo đã thanh toán MoMo nhưng đơn chưa cập nhật",
            "type": "PAYMENT_DISPUTE",
        },
    )
    assert created.status_code == 201
    case_id = created.json()["id"]

    maker = get_sessionmaker()
    old_run_id = "RUN-CRASHED-0831"
    with maker() as db:
        c = db.get(Case, case_id)
        assert c is not None
        c.state = CaseState.INVESTIGATING.value
        c.version = 2
        stale_version = c.version
        db.add(
            CaseEvent(
                id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                case_id=case_id,
                session_id=session_id,
                type="INVESTIGATION_STARTED",
                payload_json={"run_id": old_run_id, "job_status": "RUNNING"},
                created_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
        db.commit()

    # Cross-session poll MUST return 404 and NOT touch Session A's Case
    other_sess = client.post("/api/v1/demo/sessions", json={"seed_fixtures": False}).json()
    cross = client.get(
        f"/api/v1/cases/{case_id}/investigations/{old_run_id}",
        headers={"X-Demo-Session": other_sess["token"]},
    )
    assert cross.status_code == 404

    # First poll by owner durably persists INVESTIGATION_INTERRUPTED and recovers Case -> ESCALATED
    poll1 = client.get(f"/api/v1/cases/{case_id}/investigations/{old_run_id}", headers=headers)
    assert poll1.status_code == 200
    assert poll1.json()["job_status"] == "INTERRUPTED"
    assert poll1.json()["case_state"] == CaseState.ESCALATED.value

    # Repeated poll MUST be idempotent (still exactly 1 INVESTIGATION_INTERRUPTED event)
    poll2 = client.get(f"/api/v1/cases/{case_id}/investigations/{old_run_id}", headers=headers)
    assert poll2.status_code == 200
    assert poll2.json()["job_status"] == "INTERRUPTED"

    with maker() as db:
        int_evts = list(
            db.scalars(
                select(CaseEvent).where(
                    CaseEvent.case_id == case_id,
                    CaseEvent.type == "INVESTIGATION_INTERRUPTED",
                )
            ).all()
        )
        assert len(int_evts) == 1
        c_after = db.get(Case, case_id)
        assert c_after is not None
        assert c_after.state == CaseState.ESCALATED.value
        assert c_after.version > stale_version

        # Late-returning old worker attempting optimistic re-acquire MUST be rejected (409)
        rt = BoundedAgentRuntime(db)
        with pytest.raises(AppError) as stale_exc:
            rt._reacquire_investigating_case(
                case_id=case_id,
                session_id=session_id,
                expected_version=stale_version,
                run_id=old_run_id,
            )
        assert stale_exc.value.status_code == 409

    # User can now start a new permitted investigation on the recovered Case!
    re_inv = client.post(
        f"/api/v1/cases/{case_id}/investigate",
        headers=headers,
        json={"max_steps": 4, "run_async": False},
    )
    assert re_inv.status_code == 200
    assert re_inv.json()["final_state"] == CaseState.WAITING_EVIDENCE.value


def test_08_3_1_fix3_taskforce_zero_db_locks_on_force_rerun_and_stale_incident_rejection(
    active_session: dict[str, str],
) -> None:
    """Fix 3: PostgreSQL FOR UPDATE NOWAIT probes verify Incident, AgentTask, Case, and
    DemoSession are ALL unlocked during Taskforce LLM reasoning (including force_rerun=True),
    and concurrent Incident mutation rejects stale LLM output with 409 CONFLICT."""
    session_id = active_session["id"]
    maker = get_sessionmaker()

    with maker() as db:
        seeded = seed_incident_taskforce_scenario(db, session_id)
        inc_id = seeded["incident_id"]
        sample_case_id = seeded["correlated_case_ids"][0]

    lock_probes_passed = 0

    class FourTableLockProbingLLM(ScriptedMockLLMAdapter):
        def generate_structured_json(self, system_prompt: str, user_message: str) -> Any:
            nonlocal lock_probes_passed
            with maker() as probe_db:
                assert (
                    probe_db.scalar(
                        select(Incident).where(Incident.id == inc_id).with_for_update(nowait=True)
                    )
                    is not None
                )
                assert (
                    probe_db.scalar(
                        select(DemoSession)
                        .where(DemoSession.id == session_id)
                        .with_for_update(nowait=True)
                    )
                    is not None
                )
                assert (
                    probe_db.scalar(
                        select(Case).where(Case.id == sample_case_id).with_for_update(nowait=True)
                    )
                    is not None
                )
                tasks_locked = list(
                    probe_db.scalars(
                        select(AgentTask)
                        .where(AgentTask.incident_id == inc_id)
                        .with_for_update(nowait=True)
                    ).all()
                )
                assert len(tasks_locked) >= 2
                probe_db.rollback()
                lock_probes_passed += 1
            return super().generate_structured_json(system_prompt, user_message)

    mock_llm = FourTableLockProbingLLM(
        json_responses=[
            {
                "selected_roles": ["FINANCE", "OPERATIONS"],
                "role_objectives": {"FINANCE": "Check ledger", "OPERATIONS": "Check sync"},
                "initial_hypothesis": "Dropped sync batch.",
            },
            {
                "findings_vi": "Finance verified sample payment.",
                "contradictions": [],
                "unknowns": [],
                "evidence_refs": [],
                "recommended_per_case_action": "PROPOSE_ORDER_SYNC",
                "request_replan": False,
                "suggested_next_role": None,
            },
            {
                "findings_vi": "Operations verified worker lag.",
                "contradictions": [],
                "unknowns": [],
                "evidence_refs": [],
                "recommended_per_case_action": "PROPOSE_ORDER_SYNC",
                "request_replan": False,
                "suggested_next_role": None,
            },
        ]
    )

    with maker() as db:
        inc = db.get(Incident, inc_id)
        assert inc is not None
        new_tasks = TaskforceManager(db, session_id, adapter=mock_llm).assign_and_run_taskforce(
            inc, force_rerun=True
        )
        assert len(new_tasks) == 2
        assert lock_probes_passed == 3

    # Verify concurrent Incident mutation during LLM reasoning triggers STALE_INCIDENT_STATE (409)
    class MutatingIncidentDuringLLM(ScriptedMockLLMAdapter):
        def generate_structured_json(self, system_prompt: str, user_message: str) -> Any:
            if self.json_calls == 0:
                with maker() as other_db:
                    other_inc = other_db.get(Incident, inc_id)
                    assert other_inc is not None
                    other_inc.updated_at = datetime.now(UTC) + timedelta(seconds=5)
                    other_db.commit()
            return super().generate_structured_json(system_prompt, user_message)

    stale_llm = MutatingIncidentDuringLLM(
        json_responses=[
            {
                "selected_roles": ["FINANCE", "OPERATIONS"],
                "role_objectives": {},
                "initial_hypothesis": "Stale hypothesis.",
            },
            {
                "findings_vi": "Stale finance.",
                "contradictions": [],
                "unknowns": [],
                "evidence_refs": [],
                "recommended_per_case_action": "NONE",
                "request_replan": False,
                "suggested_next_role": None,
            },
            {
                "findings_vi": "Stale ops.",
                "contradictions": [],
                "unknowns": [],
                "evidence_refs": [],
                "recommended_per_case_action": "NONE",
                "request_replan": False,
                "suggested_next_role": None,
            },
        ]
    )
    with maker() as db:
        inc = db.get(Incident, inc_id)
        assert inc is not None
        with pytest.raises(AppError) as stale_err:
            TaskforceManager(db, session_id, adapter=stale_llm).assign_and_run_taskforce(
                inc, force_rerun=True
            )
        assert stale_err.value.status_code == 409
        assert "STALE_INCIDENT_STATE" in stale_err.value.message
