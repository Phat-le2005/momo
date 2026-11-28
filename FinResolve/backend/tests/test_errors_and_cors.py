from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from app.core.config import ModelMode, Settings
from app.core.errors import AppError, ErrorCode, register_error_handlers
from app.core.request_id import RequestIdMiddleware


def test_unknown_route_uses_error_envelope(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["request_id"]


def test_method_not_allowed_envelope(client: TestClient) -> None:
    response = client.post("/healthz")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_malicious_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Request-ID": "bad id\twith spaces"})
    assert response.headers["X-Request-ID"] != "bad id\twith spaces"


class Payload(BaseModel):
    amount: int


def _app_with_test_routes() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)

    @app.post("/validate")
    async def validate(payload: Payload) -> dict[str, int]:
        return {"amount": payload.amount}

    @app.get("/business-error")
    async def business_error() -> None:
        raise AppError(ErrorCode.CONFLICT, "Version conflict", status_code=409, details={"v": 2})

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    return app


def test_validation_error_envelope() -> None:
    with TestClient(_app_with_test_routes()) as c:
        response = c.post("/validate", json={"amount": "not-a-number"})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"][0]["loc"] == ["body", "amount"]


def test_typed_business_error_envelope() -> None:
    with TestClient(_app_with_test_routes()) as c:
        response = c.get("/business-error")
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "CONFLICT",
        "message": "Version conflict",
        "details": {"v": 2},
        "request_id": response.headers["X-Request-ID"],
    }


def test_unhandled_error_does_not_leak_internals() -> None:
    with TestClient(_app_with_test_routes(), raise_server_exceptions=False) as c:
        response = c.get("/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in response.text


def test_cors_allows_configured_origin_only(client: TestClient) -> None:
    allowed = client.options(
        "/healthz",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
    )
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:3000"

    denied = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.parametrize(
    ("origin", "requested_headers", "expected_status"),
    [
        ("http://localhost:3000", "content-type,x-demo-session,x-operator-token", 200),
        ("https://evil.example", "content-type,x-demo-session,x-operator-token", 400),
        ("http://localhost:3000", "x-untrusted-admin", 400),
    ],
)
def test_operator_bootstrap_cors_preflight(
    client: TestClient,
    origin: str,
    requested_headers: str,
    expected_status: int,
) -> None:
    response = client.options(
        "/api/v1/experience/bootstrap",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": requested_headers,
        },
    )

    assert response.status_code == expected_status
    if expected_status == 200:
        assert response.headers["access-control-allow-origin"] == origin
        allowed_headers = {
            header.strip().lower()
            for header in response.headers["access-control-allow-headers"].split(",")
        }
        assert {"content-type", "x-demo-session", "x-operator-token"} <= allowed_headers
    if origin == "https://evil.example":
        assert "access-control-allow-origin" not in response.headers


def test_wildcard_origin_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(web_origin="*")


def test_non_mock_source_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(source_mode="MOMO_SANDBOX")


def test_model_mode_requires_provider_name_and_key() -> None:
    assert Settings(model_provider="x", model_name="m", model_api_key="").model_mode == (
        ModelMode.DEMO_RULES
    )
    assert Settings(model_provider="x", model_name="m", model_api_key="k").model_mode == (
        ModelMode.LLM_API
    )
