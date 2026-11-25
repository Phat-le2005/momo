"""Restricted Public HTTPS Reverse Proxy / IPN Gateway (`python -m app.gateway`).

Designed to sit behind Cloudflare Tunnel (`cloudflared`) or `ngrok` so VNPAY Sandbox can reach
`GET /api/v1/payments/vnpay/ipn` and customers can reach the storefront + ReturnURL over HTTPS
WITHOUT exposing privileged merchant, service, API docs, database, or MOCK admin routes.

Security Invariants (Prompt 11 Section 15):
- Fails closed (`503`) unless `COMMERCE_PUBLIC_GATEWAY_ENABLED=true`.
- Strictly allowlists public storefront routes, ReturnURL, and `GET /api/v1/payments/vnpay/ipn`.
- Explicitly blocks (`403`) `/docs`, `/redoc`, `/openapi.json`, `/merchant`, `/api/v1/merchant/*`,
  `/api/v1/service/*`, `/api/v1/test-payments/*`, and `/api/v1/store/orders/*/mock-pay`.
- Preserves raw query string verbatim when forwarding IPN so `vnp_SecureHash` stays intact.
- Never logs full signed callback URLs or secrets.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.rate_limit import enforce_payment_rate_limit

logger = logging.getLogger("finshop.commerce.gateway")

# Explicitly blocked sensitive path prefixes / patterns
BLOCKED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/(docs|redoc|openapi\.json)(/.*)?$"),
    re.compile(r"^/merchant(/.*)?$"),
    re.compile(r"^/api/v1/merchant(/.*)?$"),
    re.compile(r"^/api/v1/service(/.*)?$"),
    re.compile(r"^/api/v1/chaos(/.*)?$"),
    re.compile(r"^/api/v1/test-payments(/.*)?$"),
    re.compile(r"^/api/v1/store/orders/[^/]+/mock-pay$"),
    re.compile(r"^/api/v1/(judge|supervisor|cases|actions)(/.*)?$"),
)

# Strictly allowlisted API routes forwarded to Commerce API (8100)
ALLOWED_API_PATTERNS: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = (
    (frozenset({"GET"}), re.compile(r"^/api/v1/payments/vnpay/ipn$")),
    (frozenset({"GET"}), re.compile(r"^/api/v1/store/payments/vnpay/return-verify$")),
    (frozenset({"GET"}), re.compile(r"^/api/v1/store/bootstrap$")),
    (frozenset({"GET"}), re.compile(r"^/api/v1/store/products(/[^/]+)?$")),
    (frozenset({"GET"}), re.compile(r"^/api/v1/store/vouchers$")),
    (frozenset({"GET"}), re.compile(r"^/api/v1/store/live$")),
    (frozenset({"POST"}), re.compile(r"^/api/v1/store/live/[^/]+/comments$")),
    (frozenset({"POST"}), re.compile(r"^/api/v1/store/customers/(guest|session|logout)$")),
    (frozenset({"GET"}), re.compile(r"^/api/v1/store/customers/me$")),
    (
        frozenset({"GET", "POST", "PATCH", "DELETE"}),
        re.compile(r"^/api/v1/store/cart(/(items(/[^/]+)?|voucher))?$"),
    ),
    (frozenset({"POST"}), re.compile(r"^/api/v1/store/checkout$")),
    (frozenset({"GET"}), re.compile(r"^/api/v1/store/orders(/[^/]+)?$")),
    (
        frozenset({"POST"}),
        re.compile(r"^/api/v1/store/orders/[^/]+/(cancel|complaints|refund-requests)$"),
    ),
)

# Strictly allowlisted frontend routes forwarded to Commerce Web (3100)
ALLOWED_WEB_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/$"),
    re.compile(r"^/(live|cart|checkout|orders|profile)$"),
    re.compile(r"^/products/[^/]+$"),
    re.compile(r"^/payment/vnpay/return$"),
    re.compile(r"^/_next/.*$"),
    re.compile(r"^/products/[^/]+\.(jpg|png|svg|webp)$"),
    re.compile(r"^/favicon\.ico$"),
)

HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


def is_route_allowed(method: str, path: str) -> tuple[bool, str]:
    """Return `(allowed, target_upstream)` where `target_upstream` is `'api'`, `'web'`, or `''`."""
    norm_method = method.upper()
    for pat in BLOCKED_PATTERNS:
        if pat.match(path):
            return False, ""
    if path.startswith("/api/"):
        for methods, pat in ALLOWED_API_PATTERNS:
            if norm_method in methods and pat.match(path):
                return True, "api"
        return False, ""
    if norm_method in {"GET", "HEAD"}:
        for pat in ALLOWED_WEB_PATTERNS:
            if pat.match(path):
                return True, "web"
    return False, ""


def create_gateway_app(
    *,
    api_upstream: str | None = None,
    web_upstream: str | None = None,
    api_transport: httpx.AsyncBaseTransport | None = None,
    web_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    api_base = (
        api_upstream or os.environ.get("COMMERCE_GATEWAY_API_UPSTREAM", "http://127.0.0.1:8100")
    ).rstrip("/")
    web_base = (
        web_upstream or os.environ.get("COMMERCE_GATEWAY_WEB_UPSTREAM", "http://127.0.0.1:3100")
    ).rstrip("/")

    gw = FastAPI(
        title="FinShop LIVE Restricted Public Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @gw.get("/healthz")
    def gateway_healthz() -> dict[str, Any]:
        settings = get_settings()
        return {
            "status": "ok",
            "service": "finshop-public-gateway",
            "public_gateway_enabled": settings.public_gateway_enabled,
            "vnpay_configured": settings.is_vnpay_configured,
        }

    @gw.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def proxy_request(full_path: str, request: Request) -> Response:
        settings = get_settings()
        if not settings.public_gateway_enabled:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "GATEWAY_DISABLED",
                        "message": (
                            "Public gateway is disabled (set COMMERCE_PUBLIC_GATEWAY_ENABLED=true "
                            "to enable restricted public exposure)."
                        ),
                    }
                },
            )

        path = "/" + full_path.lstrip("/")
        allowed, upstream_kind = is_route_allowed(request.method, path)
        if not allowed:
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "FORBIDDEN_PUBLIC_ROUTE",
                        "message": "This route is not exposed on the public gateway.",
                    }
                },
            )

        if path == "/api/v1/payments/vnpay/ipn":
            enforce_payment_rate_limit(request)

        # Enforce request body size limit
        body = await request.body()
        if len(body) > settings.max_request_body_bytes:
            return JSONResponse(
                status_code=413,
                content={"error": {"code": "PAYLOAD_TOO_LARGE", "message": "Body too large"}},
            )

        # Preserve raw query string verbatim so VNPAY vnp_SecureHash is NEVER altered!
        raw_qs = request.scope.get("query_string", b"").decode("latin-1")
        target_base = api_base if upstream_kind == "api" else web_base
        target_url = f"{target_base}{path}" + (f"?{raw_qs}" if raw_qs else "")

        fwd_headers = {
            k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
        }
        client_host = request.client.host if request.client else "127.0.0.1"
        prior_xff = fwd_headers.get("x-forwarded-for", "")
        fwd_headers["x-forwarded-for"] = f"{prior_xff}, {client_host}" if prior_xff else client_host
        fwd_headers["x-forwarded-proto"] = request.headers.get("x-forwarded-proto", "https")

        # Log path ONLY — never log raw signed query strings!
        logger.info("Gateway forward %s %s -> %s", request.method, path, upstream_kind)

        transport = api_transport if upstream_kind == "api" else web_transport
        async with httpx.AsyncClient(timeout=15.0, transport=transport) as client:
            upstream_resp = await client.request(
                method=request.method,
                url=target_url,
                headers=fwd_headers,
                content=body if body else None,
                follow_redirects=False,
            )

        resp_headers = {
            k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
        }
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
        )

    return gw


gateway_app = create_gateway_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("COMMERCE_GATEWAY_PORT", "8200"))
    uvicorn.run(gateway_app, host="127.0.0.1", port=port)
