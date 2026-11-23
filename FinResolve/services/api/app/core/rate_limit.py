"""In-memory sliding-window rate limiter and LLM concurrency guard (Prompt 08.3).

Hard Security Invariants:
1. Uses `request.client.host` directly by default; NEVER trusts client-spoofed `X-Forwarded-For`
   unless BOTH `TRUST_PROXY_HEADERS=true` AND `request.client.host` is in `TRUSTED_PROXY_IPS`.
2. Applies across sessions per client IP so creating new guest sessions in a loop cannot
   bypass AI investigation or session-creation rate limits.
3. Enforces a bounded concurrency limit on expensive concurrent LLM invocations via
   `llm_concurrency_guard()` so burst requests cannot exhaust worker threads or token budgets.
4. Deployment scope note: In-memory buckets are scoped to a single API worker process (ideal
   for single-worker modular monolith demo deployments without Redis). For multi-worker
   production deployments, align reverse-proxy/API-gateway rate limits with these same ceilings.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager

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
_llm_sem_lock = threading.Lock()
_llm_sem_limit: int = 0
_llm_semaphore: threading.BoundedSemaphore | None = None


def reset_rate_limiter() -> None:
    """Reset in-memory rate-limit buckets and LLM concurrency semaphore (for tests)."""
    global _llm_sem_limit, _llm_semaphore
    _rate_limiter.reset()
    with _llm_sem_lock:
        _llm_sem_limit = 0
        _llm_semaphore = None


def _get_llm_semaphore() -> threading.BoundedSemaphore:
    global _llm_sem_limit, _llm_semaphore
    target_limit = get_settings().max_concurrent_llm_calls
    with _llm_sem_lock:
        if _llm_semaphore is None or _llm_sem_limit != target_limit:
            _llm_sem_limit = target_limit
            _llm_semaphore = threading.BoundedSemaphore(target_limit)
        return _llm_semaphore


@contextmanager
def llm_concurrency_guard() -> Iterator[None]:
    """Bound concurrent expensive LLM calls across the API worker without external Redis."""
    sem = _get_llm_semaphore()
    acquired = sem.acquire(blocking=False)
    if not acquired:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            "Too many concurrent LLM requests in flight; please retry shortly.",
            status_code=429,
            details={"reason": "CONCURRENT_LLM_LIMIT_EXCEEDED"},
        )
    try:
        yield
    finally:
        sem.release()


def resolve_client_ip(request: Request) -> str:
    """Resolve client IP safely.

    Only honors `X-Forwarded-For` when `TRUST_PROXY_HEADERS=true` AND the direct socket peer
    (`request.client.host`) is explicitly listed in `TRUSTED_PROXY_IPS`.
    """
    settings = get_settings()
    peer_ip = request.client.host if (request.client and request.client.host) else "unknown"
    if settings.trust_proxy_headers and peer_ip in settings.trusted_proxy_ip_set:
        xff = request.headers.get("x-forwarded-for", "").strip()
        if xff:
            first_hop = xff.split(",")[0].strip()
            if first_hop:
                return first_hop
    return peer_ip


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
