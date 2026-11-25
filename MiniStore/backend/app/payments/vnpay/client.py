"""Injectable HTTP client for VNPAY Sandbox Server-to-Server APIs (QueryDr & Refund).

Automated tests inject a mocked `httpx.BaseTransport` so zero automated traffic is ever
sent to VNPAY Sandbox servers.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.config import get_settings


class VNPayHttpClient:
    def __init__(
        self,
        *,
        api_url: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self.api_url = (api_url or settings.vnpay_api_url).strip()
        self.timeout_seconds = timeout_seconds or settings.vnpay_timeout_seconds
        self._transport = transport

    def post_transaction(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """POST JSON payload to VNPAY Merchant WebAPI (`querydr` or `refund`)."""
        with httpx.Client(
            timeout=self.timeout_seconds,
            transport=self._transport,
        ) as client:
            resp = client.post(
                self.api_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            data = resp.json() if resp.content else {}
            return resp.status_code, data if isinstance(data, dict) else {"raw": data}
