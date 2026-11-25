"""Authenticated Read-Only HTTP Connector for MiniStore Commerce (Prompt 12).

Security & Boundary Invariants:
- FinResolve NEVER connects directly to MiniStore's PostgreSQL database.
- All reads go exclusively through MiniStore's authenticated `/api/v1/service/*` endpoints
  using server-side credentials (`X-Commerce-Service-Key`, `X-Merchant-Id`,
  `X-Commerce-Session-Id`, `X-World-Epoch`).
- URLs are strictly built from the server-configured `CommerceSourceBinding.base_url`;
  LLMs can NEVER supply arbitrary URLs or override host/headers.
- Strict timeouts, bounded retries, Pydantic response validation, explicit provenance &
  `observed_at` freshness timestamps, and ZERO silent fallback to the local Simulator!
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field


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


def _validate_base_url(base_url: str) -> str:
    cleaned = base_url.strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CommerceConnectorError(
            f"Invalid commerce service base_url: {base_url!r}", retryable=False
        )
    return cleaned


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
        self.base_url = _validate_base_url(base_url)
        self._service_key = service_key
        self.merchant_id = merchant_id
        self.commerce_session_id = commerce_session_id
        self.world_epoch = world_epoch
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, min(max_retries, 3))
        self._transport = transport

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
        if not path.startswith("/api/v1/service/"):
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
                if resp.status_code in (400, 401, 403, 404, 409, 422):
                    raise CommerceConnectorError(
                        f"Commerce API {path} returned HTTP {resp.status_code}: {resp.text[:240]}",
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
                    f"Commerce API {path} timed out after {self.timeout_seconds}s: {exc}",
                    retryable=True,
                )
            except Exception as exc:  # noqa: BLE001
                last_err = CommerceConnectorError(
                    f"Commerce API {path} transport error: {str(exc)[:240]}",
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

    def list_orders(
        self,
        *,
        status: str | None = None,
        reconciliation_status: str | None = None,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], datetime]:
        params: dict[str, Any] = {"limit": max(1, min(limit, 200))}
        if status:
            params["status"] = status
        if reconciliation_status:
            params["reconciliation_status"] = reconciliation_status
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
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], datetime]:
        params: dict[str, Any] = {"limit": max(1, min(limit, 200))}
        if status:
            params["status"] = status
        if is_synced is not None:
            params["is_synced"] = str(is_synced).lower()
        if reconciliation_required is not None:
            params["reconciliation_required"] = str(reconciliation_required).lower()
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
