from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import session as db_session


def test_healthz_ok_with_reachable_database(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "finresolve-api"
    assert body["database"] == {"status": "ok"}
    assert body["source_mode"] == "MOCK"
    # No key configured -> deterministic fallback, must be labeled honestly.
    assert body["model_mode"] == "DEMO_RULES"
    assert response.headers.get("X-Request-ID")


def test_healthz_degraded_when_database_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db_session, "check_database", lambda: False)
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == {"status": "unavailable"}


def test_readyz_returns_503_envelope_when_database_down(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db_session, "check_database", lambda: False)
    response = client.get("/readyz", headers={"X-Request-ID": "req-123"})
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "SERVICE_UNAVAILABLE"
    assert error["request_id"] == "req-123"


def test_readyz_ok(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["database"]["status"] == "ok"


def test_api_v1_meta(client: TestClient) -> None:
    response = client.get("/api/v1")
    assert response.status_code == 200
    body = response.json()
    assert body["api_version"] == "v1"
    assert body["source_mode"] == "MOCK"
    assert body["synthetic_data_only"] is True


def test_openapi_served_under_api_v1(client: TestClient) -> None:
    response = client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/healthz" in paths
    assert "/api/v1" in paths
