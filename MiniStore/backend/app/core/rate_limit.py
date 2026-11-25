"""Thread-safe sliding-window rate limiter for FinShop LIVE Commerce API."""

from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import Request

from app.core.config import get_settings
from app.core.errors import CommerceError, ErrorCode


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = {}

    def check(self, key: str, limit: int, window_seconds: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            q = self._buckets.setdefault(key, deque())
            while q and q[0] <= cutoff:
                q.popleft()
            if len(q) >= limit:
                retry_after = max(1, int(window_seconds - (now - q[0])) + 1)
                return False, retry_after
            q.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


_limiter = SlidingWindowRateLimiter()


def reset_rate_limiter() -> None:
    _limiter.reset()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client and request.client.host else "unknown"


def enforce_checkout_rate_limit(request: Request) -> None:
    limit = get_settings().rate_limit_checkout_per_minute
    ok, retry = _limiter.check(f"checkout:{_client_ip(request)}", limit=limit)
    if not ok:
        raise CommerceError(
            ErrorCode.RATE_LIMITED,
            f"Too many checkout requests; retry in {retry}s",
            status_code=429,
            details={"retry_after_seconds": retry},
        )


def enforce_auth_rate_limit(request: Request) -> None:
    limit = get_settings().rate_limit_auth_per_minute
    ok, retry = _limiter.check(f"auth:{_client_ip(request)}", limit=limit)
    if not ok:
        raise CommerceError(
            ErrorCode.RATE_LIMITED,
            f"Too many authentication attempts; retry in {retry}s",
            status_code=429,
            details={"retry_after_seconds": retry},
        )


def enforce_payment_rate_limit(request: Request) -> None:
    limit = get_settings().rate_limit_payment_per_minute
    ok, retry = _limiter.check(f"payment:{_client_ip(request)}", limit=limit)
    if not ok:
        raise CommerceError(
            ErrorCode.RATE_LIMITED,
            f"Too many payment requests; retry in {retry}s",
            status_code=429,
            details={"retry_after_seconds": retry},
        )
