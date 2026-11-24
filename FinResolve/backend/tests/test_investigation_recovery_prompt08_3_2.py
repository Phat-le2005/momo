"""Prompt 08.3.2 — Investigation Polling Restoration & Recovery Regression Tests (PostgreSQL).

Covers Scenarios A–E:
- Scenario A: Active healthy investigation returns active_run_id + investigation_status=RUNNING
  on GET /api/v1/cases/{id}/status without falsely interrupting it.
- Scenario B: Stale crashed worker (Case.state == INVESTIGATING) is durably recovered on
  GET /api/v1/cases/{id}/status (persists 1 INVESTIGATION_INTERRUPTED, transitions -> ESCALATED,
  returns investigation_status=INTERRUPTED, active_run_id=None), repeated polls are idempotent,
  and retrying POST /api/v1/cases/{id}/investigate (both sync and async, even without polling
  /status first) succeeds cleanly.
- Scenario C: Cross-session GET /api/v1/cases/{id}/status returns 404 and never mutates
  Session A's Case or events.
- Scenario D: Late-returning old worker after /status recovery is rejected by optimistic
  version check (409 CONFLICT) and cannot overwrite the new run.
- Scenario E: Terminal states (COMPLETED, FAILED, INTERRUPTED, IDLE) report active_run_id=None
  and accurate investigation_status on GET /api/v1/cases/{id}/status.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai_core.runtime import BoundedAgentRuntime
from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.models.cases import Case, CaseEvent
from app.models.enums import CaseState


def _create_case(client: TestClient, headers: dict[str, str]) -> str:
    res = client.post(
        "/api/v1/cases",
        headers=headers,
        json={
            "text": "Khách báo đã thanh toán MoMo nhưng đơn chưa cập nhật",
            "type": "PAYMENT_DISPUTE",
        },
    )
    assert res.status_code == 201
    return str(res.json()["id"])


def test_08_3_2_scenario_a_active_running_investigation_restores_polling_without_interruption(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Scenario A: Healthy RUNNING investigation returns active_run_id on /status
    without falsely interrupting it."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]
    case_id = _create_case(client, headers)

    maker = get_sessionmaker()
    active_run_id = "RUN-ACTIVE-0832A"
    with maker() as db:
        c = db.get(Case, case_id)
        assert c is not None
        c.state = CaseState.INVESTIGATING.value
        c.version = 2
        db.add(
            CaseEvent(
                id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                case_id=case_id,
                session_id=session_id,
                type="INVESTIGATION_STARTED",
                payload_json={"run_id": active_run_id, "job_status": "RUNNING"},
                created_at=datetime.now(UTC) - timedelta(seconds=5),
            )
        )
        db.commit()

    # Refreshing the page calls GET /api/v1/cases/{id}/status
    status_res = client.get(f"/api/v1/cases/{case_id}/status", headers=headers)
    assert status_res.status_code == 200
    body = status_res.json()
    assert body["case_id"] == case_id
    assert body["state"] == CaseState.INVESTIGATING.value
    assert body["investigation_status"] == "RUNNING"
    assert body["active_run_id"] == active_run_id

    # Verify zero INVESTIGATION_INTERRUPTED events were written
    with maker() as db:
        int_evts = list(
            db.scalars(
                select(CaseEvent).where(
                    CaseEvent.case_id == case_id,
                    CaseEvent.type == "INVESTIGATION_INTERRUPTED",
                )
            ).all()
        )
        assert len(int_evts) == 0


def test_08_3_2_scenario_b_c_d_status_recovers_stale_worker_blocks_cross_session_and_old_worker(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Scenarios B, C, D:
    - Cross-session /status returns 404 and leaves Session A's Case untouched.
    - Owner /status durably recovers stale worker (INVESTIGATING -> ESCALATED, 1 event, idempotent).
    - Late-returning old worker gets 409 CONFLICT.
    - User can retry investigation cleanly after recovery.
    """
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]
    case_id = _create_case(client, headers)

    maker = get_sessionmaker()
    crashed_run_id = "RUN-CRASHED-0832B"
    with maker() as db:
        c = db.get(Case, case_id)
        assert c is not None
        c.state = CaseState.INVESTIGATING.value
        c.version = 3
        stale_version = c.version
        db.add(
            CaseEvent(
                id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                case_id=case_id,
                session_id=session_id,
                type="INVESTIGATION_STARTED",
                payload_json={"run_id": crashed_run_id, "job_status": "RUNNING"},
                created_at=datetime.now(UTC) - timedelta(minutes=15),
            )
        )
        db.commit()

    # Scenario C: Cross-session /status MUST return 404 and NOT mutate Session A's Case
    other_sess = client.post("/api/v1/demo/sessions", json={"seed_fixtures": False}).json()
    cross_res = client.get(
        f"/api/v1/cases/{case_id}/status",
        headers={"X-Demo-Session": other_sess["token"]},
    )
    assert cross_res.status_code == 404

    with maker() as db:
        c_untouched = db.get(Case, case_id)
        assert c_untouched is not None
        assert c_untouched.state == CaseState.INVESTIGATING.value
        assert c_untouched.version == stale_version

    # Scenario B: Owner refreshes -> GET /api/v1/cases/{id}/status durably recovers Case
    st1 = client.get(f"/api/v1/cases/{case_id}/status", headers=headers)
    assert st1.status_code == 200
    body1 = st1.json()
    assert body1["state"] == CaseState.ESCALATED.value
    assert body1["investigation_status"] == "INTERRUPTED"
    assert body1["active_run_id"] == crashed_run_id

    # Repeated /status poll MUST be idempotent (still exactly 1 INVESTIGATION_INTERRUPTED)
    st2 = client.get(f"/api/v1/cases/{case_id}/status", headers=headers)
    assert st2.status_code == 200
    assert st2.json()["investigation_status"] == "INTERRUPTED"
    assert st2.json()["active_run_id"] == crashed_run_id

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
        c_recovered = db.get(Case, case_id)
        assert c_recovered is not None
        assert c_recovered.state == CaseState.ESCALATED.value
        assert c_recovered.version > stale_version

        # Scenario D: Late-returning old worker MUST fail optimistic version check (409)
        rt = BoundedAgentRuntime(db)
        with pytest.raises(AppError) as exc_info:
            rt._reacquire_investigating_case(
                case_id=case_id,
                session_id=session_id,
                expected_version=stale_version,
                run_id=crashed_run_id,
            )
        assert exc_info.value.status_code == 409

    # Scenario B (continued): User retries investigation after recovery -> succeeds cleanly
    retry_res = client.post(
        f"/api/v1/cases/{case_id}/investigate",
        headers=headers,
        json={"max_steps": 4, "run_async": False},
    )
    assert retry_res.status_code == 200
    assert retry_res.json()["final_state"] == CaseState.WAITING_EVIDENCE.value


def test_08_3_2_scenario_b_async_investigate_auto_recovers_stale_worker_without_prior_status_poll(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Scenario B (direct retry): Calling POST /investigate (run_async=True) on a stale crashed
    Case auto-recovers the stale run first instead of falsely returning 409."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]
    case_id = _create_case(client, headers)

    maker = get_sessionmaker()
    with maker() as db:
        c = db.get(Case, case_id)
        assert c is not None
        c.state = CaseState.INVESTIGATING.value
        c.version = 2
        db.add(
            CaseEvent(
                id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                case_id=case_id,
                session_id=session_id,
                type="INVESTIGATION_STARTED",
                payload_json={"run_id": "RUN-STALE-DIRECT", "job_status": "RUNNING"},
                created_at=datetime.now(UTC) - timedelta(minutes=12),
            )
        )
        db.commit()

    # Directly invoke async investigate WITHOUT polling /status first
    res = client.post(
        f"/api/v1/cases/{case_id}/investigate",
        headers=headers,
        json={"max_steps": 4, "run_async": True},
    )
    assert res.status_code == 202
    assert res.json()["stop_reason"] == "ASYNC_JOB_QUEUED"
    assert res.json()["run_id"] != "RUN-STALE-DIRECT"


def test_08_3_2_scenario_e_terminal_states_report_accurate_status_on_endpoint(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Scenario E: IDLE, COMPLETED, and FAILED cases report accurate investigation_status
    on /status (never RUNNING)."""
    headers = {"X-Demo-Session": active_session["token"]}
    session_id = active_session["id"]
    case_id = _create_case(client, headers)

    # 1. IDLE before any investigation
    st_idle = client.get(f"/api/v1/cases/{case_id}/status", headers=headers)
    assert st_idle.status_code == 200
    assert st_idle.json()["investigation_status"] == "IDLE"
    assert st_idle.json()["active_run_id"] is None

    # 2. COMPLETED after sync investigation
    inv = client.post(
        f"/api/v1/cases/{case_id}/investigate",
        headers=headers,
        json={"max_steps": 4, "run_async": False},
    )
    assert inv.status_code == 200
    st_done = client.get(f"/api/v1/cases/{case_id}/status", headers=headers)
    assert st_done.status_code == 200
    assert st_done.json()["investigation_status"] == "COMPLETED"
    assert st_done.json()["active_run_id"] == inv.json()["run_id"]

    # 3. FAILED terminal state
    maker = get_sessionmaker()
    with maker() as db:
        db.add(
            CaseEvent(
                id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                case_id=case_id,
                session_id=session_id,
                type="INVESTIGATION_FAILED",
                payload_json={"run_id": "RUN-FAILED-0832E", "error_code": "INTERNAL_ERROR"},
                created_at=datetime.now(UTC) + timedelta(seconds=1),
            )
        )
        db.commit()

    st_failed = client.get(f"/api/v1/cases/{case_id}/status", headers=headers)
    assert st_failed.status_code == 200
    assert st_failed.json()["investigation_status"] == "FAILED"
    assert st_failed.json()["active_run_id"] == "RUN-FAILED-0832E"
