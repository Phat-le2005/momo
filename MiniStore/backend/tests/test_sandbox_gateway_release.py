"""Offline regression for the standalone public gateway."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import gateway
from app.core import rate_limit
from app.core.errors import CommerceError

SHOP = "https://shop.example.test"
IPN = "https://ipn.example.test" + gateway.IPN_PATH


@pytest.fixture(autouse=True)
def clean_commerce_db() -> None:
    """Override the suite's DB fixture because these tests use only ASGI and fake upstreams."""


@pytest.fixture
def public_gateway_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        public_gateway_enabled=True,
        public_ipn_host="ipn.example.test",
        public_base_url=SHOP,
        is_vnpay_configured=False,
        max_request_body_bytes=1024,
    )
    monkeypatch.setattr(gateway, "get_settings", lambda: settings)


def test_public_gateway_rejects_noncanonical_paths_and_methods(
    public_gateway_settings: None,
) -> None:
    seen: list[tuple[str, str]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200)

    transport = httpx.MockTransport(upstream)
    app = gateway.create_gateway_app(api_transport=transport, web_transport=transport)
    with TestClient(app, base_url=SHOP) as client:
        for method, url in [
            ("GET", "/products/example%3Fcolor"),
            ("GET", "/products/%61"),
            ("GET", "/products/example/"),
            ("GET", "/_next//static/chunks/test.js"),
            ("GET", "/api/v1/store/%63art"),
            ("DELETE", "/api/v1/store/cart"),
            ("PATCH", "/api/v1/store/cart/items"),
            ("GET", IPN.rsplit("/", 1)[0] + "/%69pn"),
            ("POST", IPN),
            ("HEAD", IPN),
            ("GET", gateway.IPN_PATH),
            ("GET", "https://ipn.example.test/api/v1/store/bootstrap"),
            ("GET", "https://not-our-shop.example.test/"),
        ]:
            response = client.request(method, url, follow_redirects=False)
            assert response.status_code == 403, (method, url)
            assert response.headers["cache-control"] == "no-store"
        duplicate_host = client.get("/", headers=[("host", "shop.example.test"), ("host", "other")])
        assert duplicate_host.status_code == 403
        assert seen == []
        assert client.get("/products/a").status_code == 200
        assert client.patch("/api/v1/store/cart/items/SKU-1").status_code == 200


def test_public_gateway_preserves_ipn_query_and_filters_headers_and_cache(
    public_gateway_settings: None, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            headers={
                "cache-control": "public, max-age=120",
                "connection": "X-Local-Hop",
                "X-Local-Hop": "discard",
            },
        )

    transport = httpx.MockTransport(upstream)
    app = gateway.create_gateway_app(api_transport=transport, web_transport=transport)
    request_headers = {
        "authorization": "Bearer synthetic",
        "cookie": "test=synthetic",
        "x-forwarded-for": "192.0.2.1",
        "cf-connecting-ip": "192.0.2.2",
        "x-customer-token": "synthetic",
    }
    with TestClient(app, base_url=SHOP) as client:
        query = "vnp_Test=a+b%2f%23&vnp_Test=exact"
        with caplog.at_level(logging.INFO):
            response = client.get(f"{IPN}?{query}", headers=request_headers)
        assert response.status_code == 200
        assert seen[-1].url.query == query.encode()
        assert "Gateway forward GET vnpay-ipn" in caplog.text
        assert query not in caplog.text
        assert set(request_headers).isdisjoint(seen[-1].headers)
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "x-local-hop" not in response.headers
        body_result = client.request("GET", IPN, content=b"body")
        assert body_result.status_code == 400
        assert len(seen) == 1

        response = client.get("/profile", headers=request_headers)
        assert response.headers["cache-control"] == "no-store"
        assert set(request_headers).isdisjoint(seen[-1].headers)
        response = client.get("/api/v1/store/customers/me", headers=request_headers)
        assert seen[-1].headers["x-customer-token"] == "synthetic"
        assert "cf-connecting-ip" not in seen[-1].headers
        assert "x-forwarded-for" not in seen[-1].headers
        response = client.get("/_next/static/chunks/test.js")
        assert response.headers["cache-control"] == "public, max-age=120"


def test_storefront_saturation_does_not_take_ipn_admission(
    public_gateway_settings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway, "MAX_STOREFRONT_INFLIGHT", 1)
    monkeypatch.setattr(gateway, "MAX_IPN_INFLIGHT", 1)

    async def exercise() -> None:
        storefront_started = asyncio.Event()
        release_storefront = asyncio.Event()

        async def upstream(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/store/bootstrap":
                storefront_started.set()
                await release_storefront.wait()
            return httpx.Response(200)

        app = gateway.create_gateway_app(api_transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=SHOP
        ) as client:
            async with asyncio.TaskGroup() as tasks:
                first = tasks.create_task(client.get("/api/v1/store/bootstrap"))
                await asyncio.wait_for(storefront_started.wait(), timeout=2)
                try:
                    overloaded = await client.get("/api/v1/store/cart")
                    assert overloaded.status_code == 503
                    assert overloaded.headers["retry-after"] == "1"
                    assert (await client.get(IPN)).status_code == 200
                finally:
                    release_storefront.set()
            assert first.result().status_code == 200
            assert (await client.get("/api/v1/store/cart")).status_code == 200

    asyncio.run(exercise())


def test_verified_ipn_has_separate_payment_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        rate_limit,
        "get_settings",
        lambda: SimpleNamespace(rate_limit_payment_per_minute=1),
    )
    rate_limit.reset_rate_limiter()
    request = Request({"type": "http", "client": ("192.0.2.1", 8000)})
    try:
        rate_limit.enforce_payment_rate_limit(request)
        with pytest.raises(CommerceError) as denied_mock:
            rate_limit.enforce_payment_rate_limit(request)
        assert denied_mock.value.status_code == 429
        rate_limit.enforce_vnpay_ipn_rate_limit(request)
        with pytest.raises(CommerceError) as denied_ipn:
            rate_limit.enforce_vnpay_ipn_rate_limit(request)
        assert denied_ipn.value.status_code == 429
    finally:
        rate_limit.reset_rate_limiter()
