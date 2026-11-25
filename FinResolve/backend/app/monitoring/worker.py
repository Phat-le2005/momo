"""Standalone Autonomous Continuous Commerce Monitor Worker (`python -m app.monitoring.worker`).

Multi-worker safe via PostgreSQL lease claiming (`FOR UPDATE SKIP LOCKED`, `lease_token`,
`locked_until`), heartbeat, graceful shutdown, and world-epoch fencing.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.monitoring import CommerceSourceBinding
from app.models.supervisor import SupervisorState
from app.monitoring.service import ScanSummary, run_binding_scan

logger = logging.getLogger("finresolve.monitor.worker")


def run_monitor_tick(
    db: Session,
    *,
    worker_id: str | None = None,
    lease_seconds: int | None = None,
    limit: int = 10,
    transport: httpx.BaseTransport | None = None,
    service_key: str | None = None,
) -> list[ScanSummary]:
    """Claim due `CommerceSourceBinding` leases, run scans outside locks, and release leases."""
    settings = get_settings()
    wid = worker_id or f"monitor-{socket.gethostname()}-{os.getpid()}"
    lease_s = lease_seconds or settings.monitor_lease_seconds
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=max(5, lease_s))

    # Phase 1: short transaction to claim unlocked/expired bindings via FOR UPDATE SKIP LOCKED
    claim_stmt = (
        select(CommerceSourceBinding)
        .where(
            CommerceSourceBinding.is_enabled.is_(True),
            CommerceSourceBinding.base_url != "",
            or_(
                CommerceSourceBinding.locked_until.is_(None),
                CommerceSourceBinding.locked_until <= now,
            ),
        )
        .order_by(CommerceSourceBinding.last_successful_scan_at.asc().nullsfirst())
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    claimed = list(db.scalars(claim_stmt).all())
    if not claimed:
        return []

    claimed_tokens: list[tuple[str, str]] = []
    for b in claimed:
        token = f"MLEASE-{uuid.uuid4().hex[:12].upper()}"
        b.worker_id = wid
        b.lease_token = token
        b.locked_until = lease_until
        b.last_heartbeat_at = now
        b.updated_at = now
        claimed_tokens.append((b.id, token))
    db.commit()

    summaries: list[ScanSummary] = []

    # Phase 2 & 3: scan outside row lock, then release lease only if fencing token still matches
    for binding_id, token in claimed_tokens:
        binding = db.get(CommerceSourceBinding, binding_id)
        if binding is None or binding.lease_token != token:
            continue

        # World-epoch fencing: sync binding.world_epoch with SupervisorState.world_epoch
        sup_state = db.get(SupervisorState, binding.session_id)
        if sup_state is not None and binding.world_epoch != sup_state.world_epoch:
            binding.world_epoch = sup_state.world_epoch
            binding.last_event_cursor = None
            binding.last_event_occurred_at = None
            db.commit()

        summary = run_binding_scan(
            db,
            binding=binding,
            transport=transport,
            service_key=service_key,
        )
        summaries.append(summary)

        # Release lease under fencing token check
        locked = db.scalar(
            select(CommerceSourceBinding)
            .where(CommerceSourceBinding.id == binding_id)
            .with_for_update()
        )
        if locked is not None and locked.lease_token == token:
            locked.lease_token = None
            locked.locked_until = None
            locked.last_heartbeat_at = datetime.now(UTC)
            locked.updated_at = datetime.now(UTC)
        db.commit()

    return summaries


def run_worker_forever() -> None:
    """Blocking worker loop for `python -m app.monitoring.worker`."""
    settings = get_settings()
    worker_id = f"monitor-{socket.gethostname()}-{os.getpid()}"
    stop = False

    def _handle_signal(signum: int, _frame: object) -> None:
        nonlocal stop
        logger.info(
            "Continuous monitor worker %s received signal %s; shutting down", worker_id, signum
        )
        stop = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info(
        "Starting FinResolve Continuous Monitor Worker %s (enabled=%s, interval=%.1fs)",
        worker_id,
        settings.continuous_monitor_enabled,
        settings.monitor_poll_interval_seconds,
    )
    while not stop:
        try:
            if settings.continuous_monitor_enabled:
                with get_sessionmaker()() as db:
                    run_monitor_tick(db, worker_id=worker_id)
        except Exception:  # noqa: BLE001
            logger.exception("Continuous monitor tick failed")
        time.sleep(settings.monitor_poll_interval_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_worker_forever()
