"""Prompt 14 Automated Tests — Real Rolling-Window Operational Telemetry & Freeform Controls."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.telemetry import get_telemetry_collector


def test_real_operational_telemetry_and_freeform_traffic_control(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Middleware measures real HTTP latency; freeform traffic load & scoped restore work."""
    monkeypatch.setenv("COMMERCE_CHAOS_ENABLED", "true")
    get_settings.cache_clear()
    get_telemetry_collector().reset()
    s = get_settings()

    svc_h = {
        "X-Commerce-Service-Key": s.service_api_key.get_secret_value(),
        "X-Merchant-Id": "MERCHANT_FINSHOP_01",
        "X-Commerce-Session-Id": "CSESS_FINSHOP_DEFAULT_01",
        "X-World-Epoch": "1",
    }
    chaos_h = {"X-Chaos-Admin-Key": s.chaos_admin_key.get_secret_value()}

    # Execute real freeform concurrent traffic experiment
    load = client.post(
        "/api/v1/chaos/controls/traffic-load",
        headers=chaos_h,
        json={"request_count": 10, "concurrency": 4, "endpoint": "/api/v1/store/products"},
    )
    assert load.status_code == 200, load.text
    load_data = load.json()
    assert load_data["requests_executed"] == 10
    assert load_data["status_codes"].get("200", 0) == 10

    # Verify authenticated /api/v1/service/telemetry reports real measured window stats
    tel = client.get("/api/v1/service/telemetry", headers=svc_h)
    assert tel.status_code == 200
    body = tel.json()
    assert body["measured_from_real_requests"] is True
    assert body["windows"]["60s"]["request_count"] >= 10
    assert body["windows"]["60s"]["latency_ms"]["p95"] >= 0.0

    # Scoped restore rejects unscoped calls (400) and succeeds for scoped order_id
    assert (
        client.post("/api/v1/chaos/controls/restore-scoped", headers=chaos_h, json={}).status_code
        == 400
    )
    r_sc = client.post(
        "/api/v1/chaos/controls/restore-scoped",
        headers=chaos_h,
        json={"order_id": "ORD-NONEXISTENT"},
    )
    assert r_sc.status_code == 200
    assert r_sc.json()["outbox_resumed"] == 0
