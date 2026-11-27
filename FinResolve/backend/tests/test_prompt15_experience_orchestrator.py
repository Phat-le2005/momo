"""Prompt 15 Tests: Unified Experience Session Orchestrator, Isolation, Idempotency & Security."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_experience_bootstrap_idempotency_isolation_and_zero_secrets(client: TestClient) -> None:
    """Prove:
    1. Bootstrap creates a fresh session without requiring manager/admin tokens.
    2. Re-calling bootstrap with X-Demo-Session restores the exact same session idempotently.
    3. Two separate sessions are strictly isolated (resetting Session A does not affect Session B).
    4. Response NEVER leaks signing secrets, write keys, manager tokens, or LLM keys.
    5. Overview returns truthful session-scoped data and honest baseline warming state.
    """
    # 1. Bootstrap Session A
    r1 = client.post("/api/v1/experience/bootstrap", json={})
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["restored"] is False
    assert b1["sandbox"]["vnpay_enabled"] is False
    tok_a = b1["session"]["token"]
    sid_a = b1["session"]["session_id"]

    # Ensure zero secrets leaked in bootstrap response
    raw_text = r1.text.lower()
    for forbidden in ("signing_secret", "write_key", "model_api_key", "manager_token", "dmk_"):
        assert forbidden not in raw_text

    # 2. Idempotent restore of Session A
    r1_restore = client.post(
        "/api/v1/experience/bootstrap",
        headers={"X-Demo-Session": tok_a},
        json={},
    )
    assert r1_restore.status_code == 200
    b1_r = r1_restore.json()
    assert b1_r["restored"] is True
    assert b1_r["session"]["session_id"] == sid_a

    # 3. Bootstrap independent Session B
    r2 = client.post("/api/v1/experience/bootstrap", json={})
    assert r2.status_code == 200
    b2 = r2.json()
    tok_b = b2["session"]["token"]
    sid_b = b2["session"]["session_id"]
    assert sid_b != sid_a

    # 4. Overview requires valid session token (401 without token)
    r_unauth = client.get("/api/v1/experience/overview")
    assert r_unauth.status_code == 401

    r_ov_a = client.get("/api/v1/experience/overview", headers={"X-Demo-Session": tok_a})
    assert r_ov_a.status_code == 200
    ov_a = r_ov_a.json()
    assert ov_a["session_id"] == sid_a
    assert ov_a["business_health"]["baseline_status"] == "WARMING_UP"
    assert ov_a["kpis"]["active_anomalies"] == 0

    # 5. Resetting Session A increments ONLY Session A's world_epoch (Session B untouched!)
    r_reset = client.post("/api/v1/experience/reset", headers={"X-Demo-Session": tok_a})
    assert r_reset.status_code == 200
    assert r_reset.json()["world_epoch"] == 2

    r_ov_b = client.get("/api/v1/experience/overview", headers={"X-Demo-Session": tok_b})
    assert r_ov_b.status_code == 200
    assert r_ov_b.json()["world_epoch"] == 1
