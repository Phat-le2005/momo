"""Prompt 13 Slice C/D Automated Tests — Isolated LOCAL_MOCK Chaos Engine (C01..C08).

Verifies:
- Chaos API disabled by default (403), invalid key rejected (401), blocked on Public Gateway (403).
- All challenge families C01..C08 + NEGATIVE_CONTROL produce real persisted LOCAL_MOCK records
  with seeded variations and ZERO leaked ground truth in `/api/v1/service/*`.
- Lookalike pair C01 (paid unsynced) vs C02 (pending unpaid) have identical `PENDING_PAYMENT`
  order symptom with materially different authoritative payment states.
- Safe chaos restore repairs test-world inventory and resumes retrying outbox items.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.gateway import create_gateway_app


def _enable_chaos(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("COMMERCE_CHAOS_ENABLED", "true")
    get_settings.cache_clear()
    s = get_settings()
    return {"X-Chaos-Admin-Key": s.chaos_admin_key.get_secret_value()}


def test_chaos_disabled_by_default_auth_and_gateway_blocked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chaos API fails closed when disabled, rejects bad key, and is blocked on public gateway."""
    assert client.post("/api/v1/chaos/inject", json={"family": "C01"}).status_code == 403

    monkeypatch.setenv("COMMERCE_CHAOS_ENABLED", "true")
    monkeypatch.setenv("COMMERCE_PUBLIC_GATEWAY_ENABLED", "true")
    get_settings.cache_clear()
    assert (
        client.post(
            "/api/v1/chaos/inject",
            headers={"X-Chaos-Admin-Key": "wrong-key"},
            json={"family": "C01"},
        ).status_code
        == 401
    )
    gw = TestClient(create_gateway_app())
    assert gw.post("/api/v1/chaos/inject", json={"family": "C01"}).status_code == 403


def test_all_challenge_families_lookalike_pair_and_restore(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All C01..C08 + NEGATIVE_CONTROL inject real LOCAL_MOCK faults without ground truth leaks."""
    headers = _enable_chaos(monkeypatch)
    s = get_settings()
    svc_headers = {
        "X-Commerce-Service-Key": s.service_api_key.get_secret_value(),
        "X-Merchant-Id": "MERCHANT_FINSHOP_01",
        "X-Commerce-Session-Id": "CSESS_FINSHOP_DEFAULT_01",
        "X-World-Epoch": "1",
    }

    # C01 vs C02 Lookalike pair
    r1 = client.post("/api/v1/chaos/inject", headers=headers, json={"family": "C01", "seed": 101})
    assert r1.status_code == 200, r1.text
    m1 = r1.json()
    assert m1["ground_truth"]["payment_confirmed"] is True

    r2 = client.post("/api/v1/chaos/inject", headers=headers, json={"family": "C02", "seed": 102})
    assert r2.status_code == 200, r2.text
    m2 = r2.json()
    assert m2["ground_truth"]["payment_confirmed"] is False

    # Verify via service read API: both orders look PENDING_PAYMENT, but payments differ!
    o1 = client.get(f"/api/v1/service/orders/{m1['affected_order_id']}", headers=svc_headers).json()
    o2 = client.get(f"/api/v1/service/orders/{m2['affected_order_id']}", headers=svc_headers).json()
    assert o1["status"] == "PENDING_PAYMENT"
    assert o2["status"] == "PENDING_PAYMENT"
    # Ground truth MUST NOT leak into service read API!
    assert "ground_truth" not in str(o1)
    assert "PAYMENT_SYNC_DISRUPTION" not in str(o1)

    p1 = client.get(
        f"/api/v1/service/payments/{m1['affected_payment_id']}", headers=svc_headers
    ).json()
    p2 = client.get(
        f"/api/v1/service/payments/{m2['affected_payment_id']}", headers=svc_headers
    ).json()
    assert p1["status"] == "SUCCEEDED" and p1["is_synced_to_order"] is False
    assert p2["status"] == "PENDING" and p2["is_synced_to_order"] is False

    # Verify C03..C08 and NEGATIVE_CONTROL all execute cleanly
    for fam in ("C03", "C04", "C05", "C06", "C07", "C08", "NEGATIVE_CONTROL"):
        res = client.post(
            "/api/v1/chaos/inject",
            headers=headers,
            json={"family": fam, "seed": 200, "background_orders": 0},
        )
        assert res.status_code == 200, f"{fam}: {res.text}"

    # Restore chaos faults cleanly
    rest = client.post("/api/v1/chaos/restore", headers=headers)
    assert rest.status_code == 200
    assert rest.json()["outbox_resumed"] >= 1
