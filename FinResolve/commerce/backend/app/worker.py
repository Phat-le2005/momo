"""Lightweight standalone Commerce background worker (`python -m app.worker`).

Runs three durable business loops:
1. Order synchronization after Payment confirmation (`sync_confirmed_payments_to_orders`).
2. Expired inventory reservation release (`expire_stale_reservations`).
3. Optional EventOutbox delivery to FinResolve when configured (`dispatch_pending_outbox_events`).
"""

from __future__ import annotations

import logging
import signal
import threading

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.services.events import dispatch_pending_outbox_events
from app.services.sync import expire_stale_reservations, sync_confirmed_payments_to_orders

logger = logging.getLogger("finshop.commerce.worker")


def run_worker_tick() -> dict[str, int]:
    """Execute one bounded iteration of sync, expiry, and outbox processing."""
    maker = get_sessionmaker()
    with maker() as db:
        synced = sync_confirmed_payments_to_orders(db)
        expired = expire_stale_reservations(db)
        delivered = dispatch_pending_outbox_events(db)
    return {"synced_orders": synced, "expired_reservations": expired, "outbox_delivered": delivered}


def run_worker_forever() -> None:
    logging.basicConfig(level=logging.INFO)
    stop_flag = threading.Event()
    poll_s = get_settings().worker_poll_seconds

    def _on_signal(_signum: int, _frame: object) -> None:
        logger.info("Shutdown signal received; stopping FinShop Commerce worker...")
        stop_flag.set()

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)

    logger.info("FinShop LIVE Commerce background worker started (poll=%.1fs)", poll_s)
    while not stop_flag.is_set():
        try:
            stats = run_worker_tick()
            if any(stats.values()):
                logger.info("Commerce worker tick: %s", stats)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Commerce worker iteration error: %s", exc)
        stop_flag.wait(timeout=poll_s)
    logger.info("FinShop LIVE Commerce worker stopped cleanly.")


if __name__ == "__main__":
    run_worker_forever()
