"""Authenticated Read-Only HTTP Connector for MiniStore Commerce (Prompt 12 + 12.1 P0 Hardening).

Security & Boundary Invariants:
- FinResolve NEVER connects directly to MiniStore's PostgreSQL database.
- Destination URLs come ONLY from server-registered connectors (`resolve_registered_connector_url`)
  and are strictly validated against approved hosts, schemes, ports, and path allowlist.
- Rejects URL userinfo, query/fragment on base URLs, arbitrary hosts (SSRF protection),
  and HTTP 3xx redirects (`follow_redirects=False`).
- Never leaks `X-Commerce-Service-Key` in exceptions or logs.
- Strict timeouts, bounded retries, Pydantic validation, explicit `observed_at` provenance,
  and ZERO silent fallback to the local Simulator!
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, StrictBool

from app.core.config import get_settings

APPROVED_PORTS: frozenset[int | None] = frozenset({None, 80, 443, 8000, 8100, 8101, 3100})
TEST_ONLY_HOSTS: frozenset[str] = frozenset({"commerce-mock.local", "testserver"})


class CommerceConnectorError(RuntimeError):
    """Raised when the MiniStore Commerce HTTP connector fails or times out."""

    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class CommerceEventItem(BaseModel):
    id: str
    event_id: str
    event_type: str
    source: str = "MINI_STORE_COMMERCE"
    source_mode: str = "MOCK"
    world_epoch: int | None = None
    order_id: str | None = None
    payment_id: str | None = None
    correlation_id: str | None = None
    occurred_at: str
    payload: dict[str, Any] = Field(default_factory=dict)


class CommerceEventsPage(BaseModel):
    items: list[CommerceEventItem] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False


class CommerceMetricsSnapshot(BaseModel):
    merchant_id: str = ""
    commerce_session_id: str = ""
    orders_total: int = 0
    pending_orders: int = 0
    reconciliation_required_orders: int = 0
    succeeded_payments: int = 0
    unsynced_payments: int = 0
    outbox_lag_count: int = 0


class CommerceVnpayCapability(BaseModel):
    vnpay_configured: StrictBool | None = None
    vnpay_querydr_available: StrictBool | None = None
    vnpay_evidence_sources: list[Literal["VNPAY_IPN", "VNPAY_QUERYDR"]] | None = None


def validate_approved_commerce_url(base_url: str) -> str:
    """Strictly validate Commerce connector destination URL against SSRF & credential-leak rules."""
    cleaned = base_url.strip().rstrip("/")
    if not cleaned:
        raise CommerceConnectorError("Commerce service base_url is empty", retryable=False)
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https"):
        raise CommerceConnectorError(
            f"Unsupported Commerce connector scheme: {parsed.scheme!r}", retryable=False
        )
    if parsed.username is not None or parsed.password is not None:
        raise CommerceConnectorError(
            "URL userinfo (credentials in URL) is strictly forbidden", retryable=False
        )
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise CommerceConnectorError(
            "Commerce base_url must be a clean origin (scheme://host[:port]) without path/query",
            retryable=False,
        )
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise CommerceConnectorError("Commerce base_url missing hostname", retryable=False)

    settings = get_settings()
    allowed_hosts = {
        h.strip().lower() for h in settings.commerce_allowed_hosts.split(",") if h.strip()
    }
    if settings.app_env.strip().lower() == "test":
        allowed_hosts |= TEST_ONLY_HOSTS

    if host not in allowed_hosts:
        raise CommerceConnectorError(
            f"CONNECTOR_HOST_FORBIDDEN: host '{host}' is not in approved server allowlist",
            status_code=403,
            retryable=False,
        )
    if parsed.port not in APPROVED_PORTS:
        raise CommerceConnectorError(
            f"CONNECTOR_PORT_FORBIDDEN: port {parsed.port} is not in approved port allowlist",
            status_code=403,
            retryable=False,
        )
    return cleaned


def resolve_registered_connector_url(connector_id: str) -> str:
    """Resolve a server-registered connector ID to its approved destination URL."""
    settings = get_settings()
    norm_id = connector_id.strip()
    if norm_id != settings.commerce_connector_id:
        raise CommerceConnectorError(
            f"UNKNOWN_CONNECTOR_ID: '{norm_id}' is not a registered server connector",
            status_code=400,
            retryable=False,
        )
    if not settings.commerce_service_base_url.strip():
        raise CommerceConnectorError(
            "CONNECTOR_UNCONFIGURED: COMMERCE_SERVICE_BASE_URL is not configured on server",
            status_code=400,
            retryable=False,
        )
    return validate_approved_commerce_url(settings.commerce_service_base_url)


class CommerceReadConnector:
    """Server-side authenticated read-only HTTP client for MiniStore `/api/v1/service/*`."""

    def __init__(
        self,
        *,
        base_url: str,
        service_key: str,
        merchant_id: str,
        commerce_session_id: str,
        world_epoch: int = 1,
        timeout_seconds: float = 5.0,
        max_retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = validate_approved_commerce_url(base_url)
        self._service_key = service_key
        self.merchant_id = merchant_id
        self.commerce_session_id = commerce_session_id
        self.world_epoch = world_epoch
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, min(max_retries, 3))
        self._transport = transport

    def _scrub(self, text: str) -> str:
        if self._service_key and self._service_key in text:
            return text.replace(self._service_key, "[REDACTED]")
        return text

    def _headers(self) -> dict[str, str]:
        return {
            "X-Commerce-Service-Key": self._service_key,
            "X-Merchant-Id": self.merchant_id,
            "X-Commerce-Session-Id": self.commerce_session_id,
            "X-World-Epoch": str(self.world_epoch),
            "Accept": "application/json",
        }

    def _request_json(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> tuple[Any, datetime]:
        if not path.startswith("/api/v1/service/") or ".." in path or "@" in path:
            raise CommerceConnectorError(
                f"Blocked non-service connector path: {path}", retryable=False
            )
        url = f"{self.base_url}{path}"
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(
                    timeout=self.timeout_seconds,
                    transport=self._transport,
                    follow_redirects=False,
                ) as client:
                    resp = client.get(url, headers=self._headers(), params=params)
                observed_at = datetime.now(UTC)
                if resp.status_code == 200:
                    return resp.json(), observed_at
                if 300 <= resp.status_code < 400:
                    raise CommerceConnectorError(
                        f"CONNECTOR_REDIRECT_BLOCKED: Commerce API {path} returned HTTP "
                        f"{resp.status_code}; redirects are strictly forbidden",
                        status_code=resp.status_code,
                        retryable=False,
                    )
                if resp.status_code in (400, 401, 403, 404, 409, 422):
                    raise CommerceConnectorError(
                        self._scrub(
                            f"Commerce API {path} returned HTTP {resp.status_code}: "
                            f"{resp.text[:200]}"
                        ),
                        status_code=resp.status_code,
                        retryable=False,
                    )
                last_err = CommerceConnectorError(
                    f"Commerce API {path} returned HTTP {resp.status_code}",
                    status_code=resp.status_code,
                    retryable=True,
                )
            except CommerceConnectorError:
                raise
            except httpx.TimeoutException as exc:
                last_err = CommerceConnectorError(
                    self._scrub(
                        f"Commerce API {path} timed out after {self.timeout_seconds}s: {exc}"
                    ),
                    retryable=True,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = CommerceConnectorError(
                    self._scrub(f"Commerce API {path} transport error: {str(exc)[:200]}"),
                    retryable=True,
                )
            if attempt >= self.max_retries:
                break
        assert last_err is not None  # noqa: S101
        raise last_err

    def _envelope(self, data: Any, observed_at: datetime, *, endpoint: str) -> dict[str, Any]:
        return {
            "source_system": "MINI_STORE_COMMERCE_HTTP",
            "merchant_id": self.merchant_id,
            "commerce_session_id": self.commerce_session_id,
            "world_epoch": self.world_epoch,
            "endpoint": endpoint,
            "observed_at": observed_at.isoformat(),
            "data": data,
        }

    def get_events(
        self,
        *,
        after_id: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> tuple[CommerceEventsPage, datetime]:
        params: dict[str, Any] = {"limit": max(1, min(limit, 200))}
        if since is not None:
            params["since"] = since.isoformat()
        elif after_id:
            params["after_id"] = after_id
        raw, observed_at = self._request_json("/api/v1/service/events", params=params)
        return CommerceEventsPage.model_validate(raw), observed_at

    def get_metrics(self) -> tuple[CommerceMetricsSnapshot, datetime]:
        raw, observed_at = self._request_json("/api/v1/service/metrics")
        return CommerceMetricsSnapshot.model_validate(raw), observed_at

    def get_vnpay_capability(self) -> CommerceVnpayCapability:
        raw, _ = self._request_json("/api/v1/service/payment-capabilities")
        return CommerceVnpayCapability.model_validate(raw)

    def get_telemetry(self) -> tuple[dict[str, Any], datetime]:
        raw, observed_at = self._request_json("/api/v1/service/telemetry")
        if not isinstance(raw, dict):
            raise CommerceConnectorError(
                "Invalid /service/telemetry response type", retryable=False
            )
        return raw, observed_at

    def list_orders(
        self,
        *,
        status: str | None = None,
        reconciliation_status: str | None = None,
        after_id: str | None = None,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], datetime]:
        params: dict[str, Any] = {"limit": max(1, min(limit, 200))}
        if status:
            params["status"] = status
        if reconciliation_status:
            params["reconciliation_status"] = reconciliation_status
        if after_id:
            params["after_id"] = after_id
        raw, observed_at = self._request_json("/api/v1/service/orders", params=params)
        if not isinstance(raw, list):
            raise CommerceConnectorError("Invalid /service/orders response type", retryable=False)
        return raw, observed_at

    def get_order(self, order_id: str) -> dict[str, Any]:
        raw, observed_at = self._request_json(f"/api/v1/service/orders/{order_id}")
        return self._envelope(raw, observed_at, endpoint=f"/api/v1/service/orders/{order_id}")

    def list_payments(
        self,
        *,
        status: str | None = None,
        is_synced: bool | None = None,
        reconciliation_required: bool | None = None,
        after_id: str | None = None,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], datetime]:
        params: dict[str, Any] = {"limit": max(1, min(limit, 200))}
        if status:
            params["status"] = status
        if is_synced is not None:
            params["is_synced"] = str(is_synced).lower()
        if reconciliation_required is not None:
            params["reconciliation_required"] = str(reconciliation_required).lower()
        if after_id:
            params["after_id"] = after_id
        raw, observed_at = self._request_json("/api/v1/service/payments", params=params)
        if not isinstance(raw, list):
            raise CommerceConnectorError("Invalid /service/payments response type", retryable=False)
        return raw, observed_at

    def get_payment(self, payment_id: str) -> dict[str, Any]:
        raw, observed_at = self._request_json(f"/api/v1/service/payments/{payment_id}")
        return self._envelope(raw, observed_at, endpoint=f"/api/v1/service/payments/{payment_id}")

    def get_payment_refunds(self, payment_id: str) -> dict[str, Any]:
        raw, observed_at = self._request_json(f"/api/v1/service/payments/{payment_id}/refunds")
        return self._envelope(
            raw, observed_at, endpoint=f"/api/v1/service/payments/{payment_id}/refunds"
        )

    def get_payment_queries(self, payment_id: str) -> dict[str, Any]:
        raw, observed_at = self._request_json(f"/api/v1/service/payments/{payment_id}/queries")
        return self._envelope(
            raw, observed_at, endpoint=f"/api/v1/service/payments/{payment_id}/queries"
        )

    def list_refunds(
        self, *, status: str | None = None, limit: int = 100
    ) -> tuple[list[dict[str, Any]], datetime]:
        params: dict[str, Any] = {"limit": max(1, min(limit, 200))}
        if status:
            params["status"] = status
        raw, observed_at = self._request_json("/api/v1/service/refunds", params=params)
        if not isinstance(raw, list):
            raise CommerceConnectorError("Invalid /service/refunds response type", retryable=False)
        return raw, observed_at

    def get_inventory(
        self, *, variant_id: str | None = None
    ) -> tuple[list[dict[str, Any]], datetime]:
        params = {"variant_id": variant_id} if variant_id else None
        raw, observed_at = self._request_json("/api/v1/service/inventory", params=params)
        if not isinstance(raw, list):
            raise CommerceConnectorError(
                "Invalid /service/inventory response type", retryable=False
            )
        return raw, observed_at

    def get_order_reservations(self, order_id: str) -> tuple[list[dict[str, Any]], datetime]:
        raw, observed_at = self._request_json(f"/api/v1/service/orders/{order_id}/reservations")
        if not isinstance(raw, list):
            raise CommerceConnectorError(
                "Invalid /service/orders/{id}/reservations response type", retryable=False
            )
        return raw, observed_at

    def get_order_timeline(self, order_id: str) -> dict[str, Any]:
        raw, observed_at = self._request_json(f"/api/v1/service/orders/{order_id}/timeline")
        return self._envelope(
            raw, observed_at, endpoint=f"/api/v1/service/orders/{order_id}/timeline"
        )

    def get_voucher_redemptions(self) -> dict[str, Any]:
        raw, observed_at = self._request_json("/api/v1/service/vouchers/redemptions")
        return self._envelope(raw, observed_at, endpoint="/api/v1/service/vouchers/redemptions")

    def get_live_sessions(self) -> dict[str, Any]:
        raw, observed_at = self._request_json("/api/v1/service/live-sessions")
        return self._envelope(raw, observed_at, endpoint="/api/v1/service/live-sessions")
