"""In-memory sliding-window rate limiter for guest session creation and AI invocations.

Hard Security Invariants (Prompt 08.2 STEP 6):
1. Uses `request.client.host` directly by default; NEVER blindly trusts client-spoofed
   `X-Forwarded-For` headers unless `TRUST_PROXY_HEADERS=true` is explicitly configured.
2. Applies across sessions per client IP so creating new guest sessions in a loop cannot
   bypass AI investigation or session-creation rate limits.
3. Zero external infrastructure required (fits modular monolith architecture).
"""

from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import Request

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode


class SlidingWindowRateLimiter:
    """Thread-safe per-key sliding-window rate limiter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = {}

    def check_and_record(
        self,
        *,
        bucket_key: str,
        limit: int,
        window_seconds: float = 60.0,
    ) -> tuple[bool, int]:
        """Return `(allowed, retry_after_seconds)`."""
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            q = self._buckets.setdefault(bucket_key, deque())
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


_rate_limiter = SlidingWindowRateLimiter()


def reset_rate_limiter() -> None:
    """Reset in-memory rate-limit buckets (used by test fixtures)."""
    _rate_limiter.reset()


def resolve_client_ip(request: Request) -> str:
    """Resolve client IP safely without trusting arbitrary client `X-Forwarded-For` headers."""
    settings = get_settings()
    if settings.trust_proxy_headers:
        xff = request.headers.get("x-forwarded-for", "").strip()
        if xff:
            # Take the right-most untrusted hop or first entry when behind a trusted reverse proxy
            first_hop = xff.split(",")[0].strip()
            if first_hop:
                return first_hop
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def enforce_session_creation_rate_limit(request: Request) -> None:
    """Rate limit guest session creation per client IP to prevent quota bypass."""
    settings = get_settings()
    ip = resolve_client_ip(request)
    allowed, retry_after = _rate_limiter.check_and_record(
        bucket_key=f"session_create:{ip}",
        limit=settings.rate_limit_sessions_per_minute,
        window_seconds=60.0,
    )
    if not allowed:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            f"Too many guest sessions created from this client (limit "
            f"{settings.rate_limit_sessions_per_minute}/min). Try again in {retry_after}s.",
            status_code=429,
            details={"retry_after_seconds": retry_after},
        )


def enforce_ai_investigation_rate_limit(request: Request) -> None:
    """Rate limit AI investigation invocations per client IP across all sessions."""
    settings = get_settings()
    ip = resolve_client_ip(request)
    allowed, retry_after = _rate_limiter.check_and_record(
        bucket_key=f"ai_investigate:{ip}",
        limit=settings.rate_limit_ai_calls_per_minute,
        window_seconds=60.0,
    )
    if not allowed:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            f"AI investigation rate limit exceeded for this client (limit "
            f"{settings.rate_limit_ai_calls_per_minute}/min). Try again in {retry_after}s.",
            status_code=429,
            details={"retry_after_seconds": retry_after},
        )
