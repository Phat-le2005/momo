"""Prompt 14.1 Phase 3 Runtime Integration Test: Continuous Monitor + Open-World Detection.

Proves that the actual `run_binding_scan(...)` background monitoring path:
1. Consumes real telemetry from `/api/v1/service/telemetry` and persists `BusinessObservation`.
2. Builds adaptive `BehavioralBaseline` during normal scans without false positives.
3. Automatically detects novel degradation (`UNKNOWN_OPERATIONAL_ANOMALY`) WITHOUT any D01-D08
   detector firing and WITHOUT manually calling `run_open_world_hybrid_detection()`.
4. Automatically creates a `Case` and `SupervisorJob` containing only legitimate observations.
5. Requires sustained healthy windows (>= 2) before marking an unknown anomaly `RECOVERED`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_sessionmaker
from app.models.cases import Case, Evidence
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.open_world import BusinessObservation
from app.models.supervisor import SupervisorJob
from app.monitoring.service import run_binding_scan


@pytest.fixture
def db(clean_database: None) -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def test_monitor_scan_automatically_detects_novel_anomaly_and_verifies_sustained_recovery(
    db: Session, active_session: dict[str, str]
) -> None:
    """Background monitor path: warm-up -> novel degradation -> Case+Job -> sustained recovery."""
    sid = active_session["id"]
    binding = CommerceSourceBinding(
        id=f"BIND-OW-{uuid.uuid4().hex[:8].upper()}",
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

    # Mutable live telemetry state returned by MiniStore `/api/v1/service/telemetry`
    telem_state: dict[str, Any] = {
        "rps": 2.0,
        "p95": 35.0,
        "error_rate": 0.0,
        "request_count": 20,
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/service/events":
            return httpx.Response(200, json={"items": [], "next_cursor": None, "has_more": False})
        if path == "/api/v1/service/orders":
            return httpx.Response(200, json=[])
        if path == "/api/v1/service/payments":
            return httpx.Response(200, json=[])
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
                    "total_orders": 10,
                    "pending_payment_orders": 0,
                    "unsynced_payments": 0,
                    "reconciliation_required_orders": 0,
                    "outbox_lag_count": 0,
                    "open_complaints": 0,
                },
            )
        if path == "/api/v1/service/telemetry":
            win = {
                "window_seconds": 60,
                "has_samples": True,
                "truncated": False,
                "last_sample_age_ms": 120.0,
                "request_count": telem_state["request_count"],
                "rps": telem_state["rps"],
                "error_count": int(telem_state["request_count"] * telem_state["error_rate"]),
                "error_rate": telem_state["error_rate"],
                "latency_ms": {
                    "p50": telem_state["p95"] * 0.6,
                    "p95": telem_state["p95"],
                    "p99": telem_state["p95"] * 1.1,
                    "max": telem_state["p95"] * 1.2,
                },
            }
            return httpx.Response(
                200,
                json={
                    "windows": {"10s": win, "60s": win, "300s": win},
                    "measured_from_real_requests": True,
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)

    # 1. Establish normal baseline across 5 normal background scans -> ZERO Cases!
    for i in range(5):
        telem_state["p95"] = 34.0 + float(i % 2)
        telem_state["error_rate"] = 0.0
        summary = run_binding_scan(
            db,
            binding=binding,
            transport=transport,
            service_key="test-read-key",
            auto_create_cases=True,
        )
        assert summary.connectivity_status == "CONNECTED"
        assert summary.cases_created == 0

    obs_rows = list(
        db.scalars(select(BusinessObservation).where(BusinessObservation.session_id == sid)).all()
    )
    assert len(obs_rows) >= 5
    assert binding.last_scan_coverage_json.get("open_world", {}).get("telemetry_available") is True

    # 2. Introduce novel operational degradation (latency 650ms + 35% error rate, zero D01-D08!)
    telem_state["p95"] = 650.0
    telem_state["error_rate"] = 0.35
    degraded_summary = run_binding_scan(
        db,
        binding=binding,
        transport=transport,
        service_key="test-read-key",
        auto_create_cases=True,
    )
    assert degraded_summary.anomalies_detected >= 1
    assert degraded_summary.cases_created == 1

    anoms = list(
        db.scalars(
            select(AnomalyObservation).where(
                AnomalyObservation.session_id == sid,
                AnomalyObservation.detector_code.in_(["OPEN_STAT", "OPEN_MV"]),
            )
        ).all()
    )
    assert len(anoms) == 1
    anom = anoms[0]
    assert anom.anomaly_type in ("UNKNOWN_OPERATIONAL_ANOMALY", "UNKNOWN_BUSINESS_ANOMALY")
    assert anom.linked_case_id is not None

    case_obj = db.get(Case, anom.linked_case_id)
    assert case_obj is not None
    assert case_obj.source_system == "COMMERCE_HTTP"

    # Verify automatic SupervisorJob creation
    jobs = list(
        db.scalars(
            select(SupervisorJob).where(
                SupervisorJob.session_id == sid,
                SupervisorJob.target_case_id == case_obj.id,
            )
        ).all()
    )
    assert len(jobs) == 1

    # Verify Agent receives legitimate observations (no scenario/challenge metadata)
    ev_rows = list(db.scalars(select(Evidence).where(Evidence.case_id == case_obj.id)).all())
    assert len(ev_rows) >= 1
    assert "challenge_code" not in str(ev_rows[0].payload_json)
    assert "expected_root_cause" not in str(ev_rows[0].payload_json)

    # 3. Recovery verification requires sustained healthy windows (>= 2 consecutive scans)
    telem_state["p95"] = 35.0
    telem_state["error_rate"] = 0.0
    rec1 = run_binding_scan(
        db,
        binding=binding,
        transport=transport,
        service_key="test-read-key",
        auto_create_cases=True,
    )
    assert rec1.recoveries_verified == 0
    db.refresh(anom)
    assert anom.lifecycle_state != "RECOVERED"

    # 2nd consecutive healthy window -> sustained VERIFIED_RECOVERY!
    rec2 = run_binding_scan(
        db,
        binding=binding,
        transport=transport,
        service_key="test-read-key",
        auto_create_cases=True,
    )
    assert rec2.recoveries_verified == 1
    db.refresh(anom)
    assert anom.lifecycle_state == "RECOVERED"
    assert anom.verification_verdict == "VERIFIED_RECOVERY"
