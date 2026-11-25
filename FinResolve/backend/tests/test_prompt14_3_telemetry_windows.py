"""Prompt 14.3 Fix 4 Tests: Independent Non-Overlapping Telemetry Windows & Interval Metrics."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from app.db.session import get_sessionmaker
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.monitoring.service import (
    _count_records_in_interval,
    build_connector_for_binding,
    verify_anomaly_recovery,
)


@pytest.fixture
def db(clean_database: None) -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def test_interval_record_counting_excludes_historical_records() -> None:
    """Order/refund throughput MUST reflect interval activity, NOT cumulative table size."""
    now = datetime.now(UTC)
    records = [
        {"id": "OLD-1", "created_at": (now - timedelta(hours=2)).isoformat()},
        {"id": "OLD-2", "created_at": (now - timedelta(minutes=5)).isoformat()},
        {"id": "NEW-1", "created_at": (now - timedelta(seconds=30)).isoformat()},
        {"id": "NEW-2", "created_at": (now - timedelta(seconds=5)).isoformat()},
    ]
    assert _count_records_in_interval(records, now=now, window_seconds=60) == 2.0


def test_open_world_recovery_rejects_missing_window_id_and_overlapping_windows(
    db: Session, active_session: dict[str, str]
) -> None:
    """Prove OPEN_STAT recovery requires 2 non-overlapping independent windows with valid IDs."""
    sid = active_session["id"]
    t0 = datetime.now(UTC) - timedelta(minutes=5)

    binding = CommerceSourceBinding(
        id=f"BIND-WIN-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        base_url="http://127.0.0.1:8100",
        merchant_id="MERCHANT_FINSHOP_01",
        commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
        commerce_world_epoch=1,
        is_enabled=True,
    )
    db.add(binding)
    db.flush()

    obs = AnomalyObservation(
        id=f"ANOM-WIN-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        source_binding_id=binding.id,
        merchant_id="MERCHANT_FINSHOP_01",
        detector_code="OPEN_STAT",
        anomaly_type="LATENCY_SPIKE",
        severity="HIGH",
        fingerprint=f"fp-win-{uuid.uuid4().hex[:8]}",
        rationale="P95 latency spike observed",
        lifecycle_state="INVESTIGATING",
    )
    db.add(obs)
    db.commit()

    current_window: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/service/telemetry":
            return httpx.Response(200, json={"windows": {"fixed_60s": current_window}})
        if request.url.path == "/api/v1/service/metrics":
            return httpx.Response(
                200,
                json={
                    "outbox_lag_count": 0,
                    "unsynced_payments": 0,
                    "total_orders": 10,
                    "total_payments": 10,
                    "pending_refunds": 0,
                },
            )
        return httpx.Response(404)

    conn = build_connector_for_binding(binding, transport=httpx.MockTransport(_handler))

    # 1. Missing window_id -> INSUFFICIENT_EVIDENCE (never advances streak!)
    current_window = {
        "window_id": "",
        "window_start": t0.isoformat(),
        "window_end": (t0 + timedelta(seconds=60)).isoformat(),
        "has_samples": True,
        "request_count": 20,
        "truncated": False,
        "last_sample_age_ms": 100.0,
        "latency_ms": {"p95": 40.0},
        "error_rate": 0.0,
    }
    rec0, detail0 = verify_anomaly_recovery(db, obs=obs, connector=conn)
    assert rec0 is False
    assert "missing telemetry window_id" in detail0

    # 2. First healthy fixed window [t0, t0+60s] -> streak 1/2
    current_window = {
        "window_id": "FIXED60:1000",
        "window_start": t0.isoformat(),
        "window_end": (t0 + timedelta(seconds=60)).isoformat(),
        "has_samples": True,
        "request_count": 20,
        "truncated": False,
        "last_sample_age_ms": 100.0,
        "latency_ms": {"p95": 40.0},
        "error_rate": 0.0,
    }
    rec1, _ = verify_anomaly_recovery(db, obs=obs, connector=conn)
    assert rec1 is False
    assert obs.verification_json["consecutive_healthy_windows"] == 1

    # 3. Overlapping window [t0+30s, t0+90s] with different window_id -> REJECTED!
    current_window = {
        "window_id": "W60:21:OVERLAP",
        "window_start": (t0 + timedelta(seconds=30)).isoformat(),
        "window_end": (t0 + timedelta(seconds=90)).isoformat(),
        "has_samples": True,
        "request_count": 21,
        "truncated": False,
        "last_sample_age_ms": 50.0,
        "latency_ms": {"p95": 35.0},
        "error_rate": 0.0,
    }
    rec_ov, detail_ov = verify_anomaly_recovery(db, obs=obs, connector=conn)
    assert rec_ov is False
    assert "overlapping telemetry window" in detail_ov
    assert obs.verification_json["consecutive_healthy_windows"] == 1

    # 4. Truly non-overlapping window [t0+60s, t0+120s] -> VERIFIED_RECOVERY!
    current_window = {
        "window_id": "FIXED60:1060",
        "window_start": (t0 + timedelta(seconds=60)).isoformat(),
        "window_end": (t0 + timedelta(seconds=120)).isoformat(),
        "has_samples": True,
        "request_count": 25,
        "truncated": False,
        "last_sample_age_ms": 40.0,
        "latency_ms": {"p95": 38.0},
        "error_rate": 0.0,
    }
    rec2, _ = verify_anomaly_recovery(db, obs=obs, connector=conn)
    assert rec2 is True
    assert obs.lifecycle_state == "RECOVERED"


def test_prompt14_4_baseline_window_dedupe_and_incomplete_window_rejection(
    db: Session, active_session: dict[str, str]
) -> None:
    """Prove:
    1. Repeated scans of the SAME completed window increase baseline sample count ONLY ONCE.
    2. Persisted last_learned_window_id prevents replay across Monitor restarts.
    3. Incomplete windows (is_complete=False) cannot establish sustained recovery.
    """
    from sqlalchemy import select

    from app.models.open_world import BehavioralBaseline
    from app.monitoring.service import run_binding_scan

    sid = active_session["id"]
    t0 = datetime.now(UTC) - timedelta(minutes=3)

    binding = CommerceSourceBinding(
        id=f"BIND-DEDUPE-{uuid.uuid4().hex[:8].upper()}",
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

    fixed_completed_win = {
        "window_id": "FIXED60:2000",
        "window_start": t0.isoformat(),
        "window_end": (t0 + timedelta(seconds=60)).isoformat(),
        "is_complete": True,
        "has_samples": True,
        "request_count": 15,
        "rps": 0.25,
        "truncated": False,
        "last_sample_age_ms": 200.0,
        "latency_ms": {"p95": 42.0},
        "error_rate": 0.0,
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/v1/service/events":
            return httpx.Response(200, json={"events": [], "has_more": False})
        if p == "/api/v1/service/orders":
            return httpx.Response(200, json=[])
        if p == "/api/v1/service/payments":
            return httpx.Response(200, json=[])
        if p == "/api/v1/service/refunds":
            return httpx.Response(200, json=[])
        if p == "/api/v1/service/inventory":
            return httpx.Response(200, json=[])
        if p == "/api/v1/service/metrics":
            return httpx.Response(
                200,
                json={
                    "outbox_lag_count": 0,
                    "unsynced_payments": 0,
                    "total_orders": 0,
                    "total_payments": 0,
                    "pending_refunds": 0,
                },
            )
        if p == "/api/v1/service/telemetry":
            return httpx.Response(
                200,
                json={
                    "windows": {"completed_fixed_60s": fixed_completed_win},
                    "measured_from_real_requests": True,
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)

    # Scan 1: learns window FIXED60:2000 once -> sample_count == 1
    run_binding_scan(db, binding=binding, transport=transport, auto_create_cases=False)
    bl = db.scalar(
        select(BehavioralBaseline).where(
            BehavioralBaseline.session_id == sid,
            BehavioralBaseline.metric_name == "p95_latency_ms",
        )
    )
    assert bl is not None
    assert bl.sample_count == 1
    assert binding.last_learned_window_id == "FIXED60:2000"

    # Scan 2 (same window repeated / Monitor restart) -> sample_count STAYS 1!
    run_binding_scan(db, binding=binding, transport=transport, auto_create_cases=False)
    db.refresh(bl)
    assert bl.sample_count == 1

    # Incomplete window MUST NOT advance sustained recovery streak
    obs = AnomalyObservation(
        id=f"ANOM-INC-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        source_binding_id=binding.id,
        merchant_id="MERCHANT_FINSHOP_01",
        detector_code="OPEN_STAT",
        anomaly_type="LATENCY_SPIKE",
        severity="HIGH",
        fingerprint=f"fp-inc-{uuid.uuid4().hex[:8]}",
        rationale="P95 spike",
        lifecycle_state="INVESTIGATING",
    )
    db.add(obs)
    db.commit()

    def _inc_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/service/telemetry":
            return httpx.Response(
                200,
                json={
                    "windows": {
                        "fixed_60s": {
                            "window_id": "FIXED60:2060",
                            "window_start": (t0 + timedelta(seconds=60)).isoformat(),
                            "window_end": (t0 + timedelta(seconds=120)).isoformat(),
                            "is_complete": False,
                            "has_samples": True,
                            "request_count": 10,
                            "truncated": False,
                            "last_sample_age_ms": 100.0,
                            "latency_ms": {"p95": 35.0},
                            "error_rate": 0.0,
                        }
                    }
                },
            )
        return _handler(request)

    conn_inc = build_connector_for_binding(binding, transport=httpx.MockTransport(_inc_handler))
    rec_inc, det_inc = verify_anomaly_recovery(db, obs=obs, connector=conn_inc)
    assert rec_inc is False
    assert "incomplete" in det_inc
