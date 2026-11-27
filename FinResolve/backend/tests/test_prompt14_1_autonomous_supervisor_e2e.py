"""Prompt 14.1 Phase 4 Required Autonomous Recovery E2E Test (Section 15).

Proves the COMPLETE runtime loop without manually creating a Case or calling the recovery planner:
  run_binding_scan() -> Case + SupervisorJob -> process_next_supervisor_job()
  -> Agent Investigation -> Policy RAG + Experience Memory -> PolicyEngine
  -> Durable RecoveryCommand -> Commerce API -> Independent Verifier -> Case RESOLVED.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_sessionmaker
from app.models.cases import Case, CaseEvent
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.open_world import IncidentExperience, RecoveryCommand
from app.monitoring.service import run_binding_scan
from app.supervisor.service import process_next_supervisor_job


@pytest.fixture
def db(clean_database: None) -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def test_full_autonomous_supervisor_recovery_pipeline_e2e(
    db: Session, active_session: dict[str, str]
) -> None:
    """Monitor -> Case -> SupervisorJob -> PolicyEngine -> Commerce API -> Verifier -> RESOLVED."""
    sid = active_session["id"]
    now = datetime.now(UTC)

    binding = CommerceSourceBinding(
        id=f"BIND-AUTO-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        base_url="http://127.0.0.1:8100",
        merchant_id="MERCHANT_FINSHOP_01",
        commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
        commerce_world_epoch=1,
        is_enabled=True,
    )
    db.add(binding)
    db.commit()

    # Authoritative Commerce state: legitimate LOCAL_MOCK payment succeeded, sync disrupted
    order_state: dict[str, Any] = {
        "id": "ORD-AUTO-E2E-01",
        "status": "PENDING_PAYMENT",
        "version": 1,
        "total_vnd": 350000,
        "reconciliation_status": "CLEAR",
    }
    payment_state: dict[str, Any] = {
        "id": "PAY-AUTO-E2E-01",
        "payment_id": "PAY-AUTO-E2E-01",
        "order_id": "ORD-AUTO-E2E-01",
        "provider": "LOCAL_MOCK",
        "status": "SUCCEEDED",
        "is_synced_to_order": False,
        "reconciliation_required": False,
        "amount_vnd": 350000,
        "confirmed_at": (now - timedelta(minutes=5)).isoformat(),
    }
    exec_requests: list[dict[str, Any]] = []

    def _commerce_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/service/events":
            return httpx.Response(200, json={"items": [], "next_cursor": None, "has_more": False})
        if path == "/api/v1/service/orders":
            return httpx.Response(200, json=[dict(order_state)])
        if path == "/api/v1/service/orders/ORD-AUTO-E2E-01":
            return httpx.Response(200, json=dict(order_state))
        if path == "/api/v1/service/orders/ORD-AUTO-E2E-01/timeline":
            return httpx.Response(200, json=[])
        if path == "/api/v1/service/payments":
            return httpx.Response(200, json=[dict(payment_state)])
        if path == "/api/v1/service/payments/PAY-AUTO-E2E-01":
            return httpx.Response(200, json=dict(payment_state))
        if path == "/api/v1/service/refunds":
            return httpx.Response(200, json=[])
        if path == "/api/v1/service/inventory":
            return httpx.Response(200, json=[])
        if path == "/api/v1/service/metrics":
            return httpx.Response(
                200,
                json={
                    "merchant_id": binding.merchant_id,
                    "commerce_session_id": binding.commerce_session_id,
                    "orders_total": 1,
                    "pending_orders": 1 if order_state["status"] == "PENDING_PAYMENT" else 0,
                    "reconciliation_required_orders": 0,
                    "succeeded_payments": 1,
                    "unsynced_payments": 0 if payment_state["is_synced_to_order"] else 1,
                    "outbox_lag_count": 0,
                },
            )
        if path == "/api/v1/service/telemetry":
            win = {
                "window_seconds": 60,
                "has_samples": True,
                "request_count": 10,
                "rps": 1.0,
                "error_count": 0,
                "error_rate": 0.0,
                "latency_ms": {"p50": 15.0, "p95": 25.0, "p99": 30.0, "max": 35.0},
            }
            return httpx.Response(
                200,
                json={
                    "windows": {"10s": win, "60s": win, "300s": win},
                    "measured_from_real_requests": True,
                },
            )
        if request.method == "POST" and path == "/api/v1/service/recovery/execute":
            exec_requests.append({"headers": dict(request.headers)})
            order_state["status"] = "CONFIRMED"
            order_state["version"] = 2
            payment_state["is_synced_to_order"] = True
            from tests.helpers_recovery import mock_receiver_response

            return httpx.Response(
                200,
                json=mock_receiver_response(
                    request,
                    status="SYNCED",
                    extra={
                        "order_status": order_state["status"],
                        "order_version": order_state["version"],
                        "is_synced_to_order": True,
                    },
                ),
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_commerce_handler)

    # 1. Background monitor detects D01 and automatically creates Case + SupervisorJob
    scan = run_binding_scan(
        db,
        binding=binding,
        transport=transport,
        service_key="test-read-key",
        auto_create_cases=True,
    )
    assert scan.cases_created == 1

    obs = db.scalar(
        select(AnomalyObservation).where(
            AnomalyObservation.session_id == sid,
            AnomalyObservation.affected_order_id == "ORD-AUTO-E2E-01",
        )
    )
    assert obs is not None
    assert obs.linked_case_id is not None

    # 2. Supervisor worker claims and executes the queued job (NO manual planner/executor call!)
    job_res = process_next_supervisor_job(
        session_id=sid,
        force_when_paused=True,
        commerce_transport=transport,
    )
    assert job_res is not None
    assert job_res["status"] == "COMPLETED"

    # 3. Verify PolicyEngine authorized, Commerce executed, Verifier verified, Case RESOLVED!
    assert len(exec_requests) == 1
    assert len(exec_requests[0]["headers"].get("x-authorization-digest", "")) == 64
    assert order_state["status"] == "CONFIRMED"
    assert payment_state["is_synced_to_order"] is True

    db.expire_all()
    case_obj = db.get(Case, obs.linked_case_id)
    assert case_obj is not None
    assert case_obj.state == "RESOLVED"

    cmd = db.scalar(
        select(RecoveryCommand).where(
            RecoveryCommand.session_id == sid,
            RecoveryCommand.target_order_id == "ORD-AUTO-E2E-01",
        )
    )
    assert cmd is not None
    assert cmd.state == "VERIFIED"
    assert cmd.policy_decision.startswith("ALLOW_POLICY_ENGINE")

    events = list(db.scalars(select(CaseEvent).where(CaseEvent.case_id == case_obj.id)).all())
    event_types = {e.type for e in events}
    assert "AUTONOMOUS_RECOVERY_PLANNED" in event_types
    assert "AUTONOMOUS_RECOVERY_VERIFIED" in event_types

    exp = db.scalar(
        select(IncidentExperience).where(
            IncidentExperience.session_id == sid,
            IncidentExperience.case_id == case_obj.id,
        )
    )
    assert exp is not None
    assert exp.is_verified is True
