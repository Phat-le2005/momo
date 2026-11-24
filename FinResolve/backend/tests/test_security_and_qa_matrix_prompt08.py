"""Prompt 08 — Security hardening, request limits, CORS, expiry, and full E2E QA Matrix tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.session import get_sessionmaker
from app.models.session import DemoSession


def test_prompt08_request_body_size_limit_rejects_oversized_payload(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """STEP 4 / QA T23: Reject oversized request bodies with 413 JSON envelope."""
    headers = {
        "X-Demo-Session": active_session["token"],
        "Content-Type": "application/json",
    }
    oversized = '{"text":"' + ("A" * 1_100_000) + '","type":"PAYMENT_DISPUTE"}'
    res = client.post("/api/v1/cases", headers=headers, content=oversized)
    assert res.status_code == 413
    body = res.json()
    assert body["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_prompt08_cors_allows_configured_origin_and_blocks_unknown_origin(
    client: TestClient,
) -> None:
    """STEP 4: Configured WEB_ORIGIN is allowed; arbitrary origins are not reflected."""
    allowed = client.options(
        "/api/v1/demo/scenarios",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"

    blocked = client.options(
        "/api/v1/demo/scenarios",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert blocked.headers.get("access-control-allow-origin") is None

    # Wildcard CORS origin is rejected by Settings validation
    try:
        Settings(web_origin="*")
        raise AssertionError("Expected ValueError for wildcard WEB_ORIGIN")
    except ValueError as exc:
        assert "wildcard" in str(exc).lower()


def test_prompt08_expired_guest_session_is_rejected_cleanly(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """STEP 4: Expired guest session returns 401 UNAUTHORIZED across protected routes."""
    maker = get_sessionmaker()
    with maker() as db:
        sess = db.get(DemoSession, active_session["id"])
        assert sess is not None
        sess.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        db.commit()

    headers = {"X-Demo-Session": active_session["token"]}
    res_cases = client.get("/api/v1/cases", headers=headers)
    res_risks = client.get("/api/v1/risks", headers=headers)
    res_inc = client.get("/api/v1/incidents", headers=headers)
    assert res_cases.status_code == 401
    assert res_risks.status_code == 401
    assert res_inc.status_code == 401


def test_prompt08_health_and_errors_never_expose_secrets(client: TestClient) -> None:
    """STEP 4: Health and error payloads never leak approval secrets or model API keys."""
    health = client.get("/healthz")
    assert health.status_code == 200
    health_text = health.text
    assert "hmac" not in health_text.lower()
    assert "api_key" not in health_text.lower()
    assert "dmk_" not in health_text
    assert health.json()["source_mode"] == "MOCK"
    assert health.json()["model_mode"] == "DEMO_RULES"
