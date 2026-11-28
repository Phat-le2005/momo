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
from threading import BoundedSemaphore
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint

from app.core.config import get_settings

logger = logging.getLogger("uvicorn.error").getChild("finshop.gateway")
# The standalone gateway handles signed URLs. HTTPX logs full URLs at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
IPN_PATH = "/api/v1/payments/vnpay/ipn"
CANONICAL_PATH = re.compile(r"/[A-Za-z0-9_./~-]*")
MAX_STOREFRONT_INFLIGHT = 24
MAX_IPN_INFLIGHT = 8
VNPAY_ONLY_BLOCKED = re.compile(
    r"^/api/v1/store/(live/[^/]+/comments|orders/[^/]+/(complaints|refund-requests))$"
)
FORWARD_HEADERS = frozenset(
    {
        "accept",
        "accept-language",
        "authorization",
        "content-type",
        "cookie",
        "origin",
        "rsc",
        "next-router-state-tree",
        "next-router-prefetch",
        "next-url",
        "x-customer-session",
        "x-customer-token",
        "x-idempotency-key",
    }
)
CLIENT_CREDENTIAL_HEADERS = frozenset(
    {"authorization", "cookie", "x-customer-session", "x-customer-token", "x-idempotency-key"}
)

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
    (frozenset({"GET"}), re.compile(r"^/api/v1/store/cart$")),
    (frozenset({"POST"}), re.compile(r"^/api/v1/store/cart/items$")),
    (frozenset({"PATCH", "DELETE"}), re.compile(r"^/api/v1/store/cart/items/[^/]+$")),
    (frozenset({"POST", "DELETE"}), re.compile(r"^/api/v1/store/cart/voucher$")),
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
    re.compile(r"^/_next/static/.+$"),
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
        "trailer",
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


def _is_canonical_path(path: str, raw_path: bytes) -> bool:
    return bool(
        CANONICAL_PATH.fullmatch(path)
        and raw_path == path.encode("ascii")
        and (path == "/" or all(part not in {"", ".", ".."} for part in path.split("/")[1:]))
    )


def _connection_tokens(values: list[str]) -> set[str]:
    return {token.strip().lower() for value in values for token in value.split(",")}


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
        redirect_slashes=False,
    )
    storefront_slots = BoundedSemaphore(MAX_STOREFRONT_INFLIGHT)
    ipn_slots = BoundedSemaphore(MAX_IPN_INFLIGHT)

    @gw.middleware("http")
    async def public_response_policy(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        path = request.scope["path"]
        public_asset = path.startswith("/_next/static/") or path == "/favicon.ico"
        if not public_asset or response.status_code >= 400 or "set-cookie" in response.headers:
            response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @gw.get("/healthz")
    def gateway_healthz(request: Request) -> dict[str, Any]:
        settings = get_settings()
        hosts = request.headers.getlist("host")
        if not _is_canonical_path("/healthz", request.scope.get("raw_path", b"")) or (
            settings.public_ipn_host
            and (len(hosts) != 1 or hosts[0] not in {"127.0.0.1", "127.0.0.1:8200"})
        ):
            raise HTTPException(status_code=403)
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

        path = "/" + full_path
        allowed, upstream_kind = is_route_allowed(request.method, path)
        is_ipn = path == IPN_PATH
        if settings.public_ipn_host:
            hosts = request.headers.getlist("host")
            shop_host = urlsplit(settings.public_base_url).hostname
            route_host = settings.public_ipn_host.lower() if is_ipn else shop_host
            allowed = (
                allowed
                and bool(route_host)
                and len(hosts) == 1
                and hosts[0].lower() in {route_host, f"{route_host}:8200"}
            )
        if settings.public_ipn_host and VNPAY_ONLY_BLOCKED.fullmatch(path):
            allowed = False
        if not allowed or not _is_canonical_path(path, request.scope.get("raw_path", b"")):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "FORBIDDEN_PUBLIC_ROUTE",
                        "message": "This route is not exposed on the public gateway.",
                    }
                },
            )

        # Preserve raw query string verbatim so VNPAY vnp_SecureHash is NEVER altered!
        query = request.scope.get("query_string", b"")
        if len(query) > 8192:
            return JSONResponse(status_code=414, content={"error": {"code": "QUERY_TOO_LONG"}})
        if any(char < 33 or char > 126 for char in query) or b"#" in query:
            return JSONResponse(status_code=400, content={"error": {"code": "INVALID_QUERY"}})
        if is_ipn and (
            request.headers.get("content-length", "0") != "0"
            or "transfer-encoding" in request.headers
        ):
            return JSONResponse(status_code=400, content={"error": {"code": "INVALID_IPN_BODY"}})

        slots = ipn_slots if is_ipn else storefront_slots
        if not slots.acquire(blocking=False):
            return JSONResponse(
                status_code=503,
                content={"error": {"code": "GATEWAY_BUSY"}},
                headers={"Retry-After": "1"},
            )
        try:
            return await forward_request(
                request, path, query, upstream_kind, settings.max_request_body_bytes
            )
        finally:
            slots.release()

    async def forward_request(
        request: Request, path: str, query: bytes, upstream_kind: str, max_body_bytes: int
    ) -> Response:
        body = bytearray()
        if path != IPN_PATH:
            async for chunk in request.stream():
                if len(body) + len(chunk) > max_body_bytes:
                    return JSONResponse(
                        status_code=413, content={"error": {"code": "PAYLOAD_TOO_LARGE"}}
                    )
                body.extend(chunk)

        target_base = api_base if upstream_kind == "api" else web_base
        base_url = httpx.URL(target_base)
        target_url = base_url.copy_with(
            path=base_url.path.rstrip("/") + path, query=query or None, fragment=None
        )

        request_headers = FORWARD_HEADERS - _connection_tokens(
            request.headers.getlist("connection")
        )
        fwd_headers = {k: v for k, v in request.headers.items() if k.lower() in request_headers}
        if path == IPN_PATH:
            fwd_headers.clear()
        elif upstream_kind == "web":
            for name in CLIENT_CREDENTIAL_HEADERS:
                fwd_headers.pop(name, None)

        route_class = "vnpay-ipn" if path == IPN_PATH else upstream_kind
        logger.info("Gateway forward %s %s", request.method, route_class)

        transport = api_transport if upstream_kind == "api" else web_transport
        try:
            async with httpx.AsyncClient(
                timeout=15.0, transport=transport, trust_env=False
            ) as client:
                upstream_resp = await client.request(
                    method=request.method,
                    url=target_url,
                    headers=fwd_headers,
                    content=bytes(body),
                    follow_redirects=False,
                )
        except httpx.HTTPError:
            return JSONResponse(
                status_code=502, content={"error": {"code": "UPSTREAM_UNAVAILABLE"}}
            )

        response = Response(content=upstream_resp.content, status_code=upstream_resp.status_code)
        stripped_headers = HOP_BY_HOP_HEADERS | _connection_tokens(
            upstream_resp.headers.get_list("connection")
        )
        for name, value in upstream_resp.headers.multi_items():
            if name not in stripped_headers and name != "content-encoding":
                response.headers.append(name, value)
        return response

    return gw


gateway_app = create_gateway_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("COMMERCE_GATEWAY_PORT", "8200"))
    uvicorn.run(gateway_app, host="127.0.0.1", port=port, access_log=False, proxy_headers=False)
