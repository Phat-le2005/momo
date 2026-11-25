"""Standalone Commerce background worker (`python -m app.worker`) with isolated loops.

Prompt 10.1 Worker Separation:
- Core Business Loop (`run_business_tick`): Order sync after payment confirmation + expired
  inventory reservation release. Runs independently on its own thread & DB session.
- Outbox Delivery Loop (`run_outbox_tick`): Optional FinResolve event outbox dispatch. Runs on
  a separate thread & DB session so slow or offline FinResolve NEVER blocks order sync or
  reservation expiry.
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


def run_business_tick() -> dict[str, int]:
    """Execute one bounded iteration of order sync and reservation expiry."""
    maker = get_sessionmaker()
    with maker() as db:
        synced = sync_confirmed_payments_to_orders(db)
        expired = expire_stale_reservations(db)
    return {"synced_orders": synced, "expired_reservations": expired}


def run_outbox_tick() -> int:
    """Execute one bounded iteration of FinResolve outbox delivery (isolated from core sync)."""
    maker = get_sessionmaker()
    with maker() as db:
        return dispatch_pending_outbox_events(db)


def run_worker_tick() -> dict[str, int]:
    """Convenience helper for tests: runs business tick and outbox tick in separate sessions."""
    biz = run_business_tick()
    delivered = run_outbox_tick()
    return {
        "synced_orders": biz["synced_orders"],
        "expired_reservations": biz["expired_reservations"],
        "outbox_delivered": delivered,
    }


def _outbox_loop(stop_flag: threading.Event, poll_s: float) -> None:
    logger.info("FinShop LIVE Outbox delivery thread started (poll=%.1fs)", poll_s)
    while not stop_flag.is_set():
        try:
            delivered = run_outbox_tick()
            if delivered > 0:
                logger.info("Commerce outbox delivered=%d", delivered)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Commerce outbox iteration error: %s", exc)
        stop_flag.wait(timeout=poll_s)
    logger.info("FinShop LIVE Outbox delivery thread stopped.")


def run_worker_forever() -> None:
    logging.basicConfig(level=logging.INFO)
    stop_flag = threading.Event()
    settings = get_settings()
    biz_poll_s = settings.worker_poll_seconds
    outbox_poll_s = settings.outbox_poll_seconds

    def _on_signal(_signum: int, _frame: object) -> None:
        logger.info("Shutdown signal received; stopping FinShop Commerce worker...")
        stop_flag.set()

    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _on_signal)

    outbox_thread = threading.Thread(
        target=_outbox_loop,
        args=(stop_flag, outbox_poll_s),
        name="finshop-outbox-worker",
        daemon=True,
    )
    outbox_thread.start()

    logger.info("FinShop LIVE Core Business worker started (poll=%.1fs)", biz_poll_s)
    while not stop_flag.is_set():
        try:
            stats = run_business_tick()
            if any(stats.values()):
                logger.info("Commerce business worker tick: %s", stats)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Commerce business worker iteration error: %s", exc)
        stop_flag.wait(timeout=biz_poll_s)

    outbox_thread.join(timeout=5.0)
    logger.info("FinShop LIVE Commerce worker stopped cleanly.")


if __name__ == "__main__":
    run_worker_forever()
