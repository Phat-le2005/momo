"""Real Rolling-Window Operational Telemetry Collector for MiniStore (Prompt 14 Section 9).

Measures actual HTTP request counts, status distributions, error rates, and P50/P95/P99
latency quantiles across rolling windows (10s, 60s, 300s) using a bounded thread-safe
deque — ZERO hardcoded synthetic latency formulas and ZERO unbounded metric labels.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

MAX_SAMPLES = 5000


@dataclass(frozen=True)
class _RequestSample:
    ts: float
    route_group: str
    status_code: int
    duration_ms: float


def _normalize_route_group(path: str) -> str:
    """Collapse high-cardinality IDs into bounded route groups."""
    if path.startswith("/api/v1/store/checkout"):
        return "store.checkout"
    if path.startswith("/api/v1/store/orders"):
        return "store.orders"
    if path.startswith("/api/v1/store/products"):
        return "store.products"
    if path.startswith("/api/v1/store/"):
        return "store.other"
    if path.startswith("/api/v1/payments/"):
        return "payments"
    if path.startswith("/api/v1/merchant/"):
        return "merchant"
    if path.startswith("/api/v1/service/"):
        return "service"
    return "other"


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = int(round((pct / 100.0) * (len(sorted_vals) - 1)))
    idx = max(0, min(idx, len(sorted_vals) - 1))
    return round(sorted_vals[idx], 2)


class OperationalTelemetryCollector:
    """Thread-safe bounded rolling-window HTTP & worker telemetry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._samples: deque[_RequestSample] = deque(maxlen=MAX_SAMPLES)

    def record_http_request(self, *, path: str, status_code: int, duration_ms: float) -> None:
        # Exclude monitoring/health-check traffic from business operational telemetry
        if (
            path in ("/health", "/api/v1/health", "/metrics")
            or path.startswith("/api/v1/service/")
            or path.startswith("/api/v1/chaos/")
        ):
            return
        sample = _RequestSample(
            ts=time.monotonic(),
            route_group=_normalize_route_group(path),
            status_code=status_code,
            duration_ms=max(0.0, float(duration_ms)),
        )
        with self._lock:
            self._samples.append(sample)

    def snapshot_window(self, window_seconds: int) -> dict[str, Any]:
        now_mono = time.monotonic()
        now_utc = datetime.now(UTC)
        win_start = now_utc - timedelta(seconds=window_seconds)
        cutoff = now_mono - float(window_seconds)
        with self._lock:
            total_buffered = len(self._samples)
            rows = [s for s in self._samples if s.ts >= cutoff]

        total = len(rows)
        if total == 0:
            return {
                "window_id": f"W{window_seconds}:EMPTY",
                "window_seconds": window_seconds,
                "window_start": win_start.isoformat(),
                "window_end": now_utc.isoformat(),
                "collected_at": now_utc.isoformat(),
                "has_samples": False,
                "truncated": total_buffered >= MAX_SAMPLES,
                "last_sample_age_ms": None,
                "request_count": 0,
                "rps": 0.0,
                "error_count": 0,
                "error_rate": 0.0,
                "status_distribution": {},
                "latency_ms": {"p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0},
                "by_route_group": {},
            }

        durations = sorted(s.duration_ms for s in rows)
        errors = sum(1 for s in rows if s.status_code >= 500 or s.status_code == 429)
        last_age_ms = round(max(0.0, (now_mono - rows[-1].ts) * 1000.0), 1)
        last_ts_bucket = int(rows[-1].ts * 1000)
        window_id = f"W{window_seconds}:{total}:{last_ts_bucket}"
        status_dist: dict[str, int] = {}
        route_counts: dict[str, int] = {}
        for s in rows:
            bucket = f"{s.status_code // 100}xx"
            status_dist[bucket] = status_dist.get(bucket, 0) + 1
            route_counts[s.route_group] = route_counts.get(s.route_group, 0) + 1

        return {
            "window_id": window_id,
            "window_seconds": window_seconds,
            "window_start": win_start.isoformat(),
            "window_end": now_utc.isoformat(),
            "collected_at": now_utc.isoformat(),
            "has_samples": True,
            "truncated": total_buffered >= MAX_SAMPLES,
            "last_sample_age_ms": last_age_ms,
            "request_count": total,
            "rps": round(total / max(1.0, float(window_seconds)), 3),
            "error_count": errors,
            "error_rate": round(errors / total, 4),
            "status_distribution": status_dist,
            "latency_ms": {
                "p50": _percentile(durations, 50.0),
                "p95": _percentile(durations, 95.0),
                "p99": _percentile(durations, 99.0),
                "max": round(durations[-1], 2),
            },
            "by_route_group": route_counts,
        }

    def snapshot_all(self) -> dict[str, Any]:
        return {
            "windows": {
                "10s": self.snapshot_window(10),
                "60s": self.snapshot_window(60),
                "300s": self.snapshot_window(300),
            },
            "measured_from_real_requests": True,
        }

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()


_TELEMETRY = OperationalTelemetryCollector()


def get_telemetry_collector() -> OperationalTelemetryCollector:
    return _TELEMETRY
