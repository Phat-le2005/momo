"""Dedicated standalone and optional embedded Supervisor worker with heartbeats & graceful shutdown.

Primary execution mechanism (Prompt 09.1):
    python -m app.supervisor.worker
"""

from __future__ import annotations

import logging
import signal
import threading
import uuid

from app.db.session import get_sessionmaker
from app.supervisor.service import process_next_supervisor_job, upsert_worker_heartbeat

logger = logging.getLogger("finresolve.supervisor.worker")

_WORKER_STOP_EVENT = threading.Event()
_WORKER_WAKE_EVENT = threading.Event()
_WORKER_THREAD: threading.Thread | None = None
_WORKER_LOCK = threading.Lock()


def notify_supervisor_worker() -> None:
    """Wake the local embedded worker (if enabled) immediately when a new job is queued."""
    _WORKER_WAKE_EVENT.set()


def _record_worker_stop(worker_id: str, worker_mode: str) -> None:
    try:
        maker = get_sessionmaker()
        with maker() as db:
            upsert_worker_heartbeat(
                db,
                worker_id=worker_id,
                worker_mode=worker_mode,
                status="STOPPED",
                active_job_id=None,
            )
            db.commit()
    except Exception as exc:  # pragma: no cover - best-effort shutdown marker
        logger.debug("Ignoring worker shutdown heartbeat error: %s", exc)


def _embedded_worker_loop(poll_interval_seconds: float = 1.5) -> None:
    worker_id = f"sup-emb-{uuid.uuid4().hex[:8]}"
    while not _WORKER_STOP_EVENT.is_set():
        try:
            res = process_next_supervisor_job(
                worker_id=worker_id,
                worker_mode="EMBEDDED",
            )
            if res is not None:
                continue
        except Exception as exc:  # pragma: no cover - defensive daemon guard
            logger.warning("Embedded Supervisor worker error: %s", exc)
        _WORKER_WAKE_EVENT.wait(timeout=poll_interval_seconds)
        _WORKER_WAKE_EVENT.clear()
    _record_worker_stop(worker_id, "EMBEDDED")


def start_embedded_supervisor_worker() -> None:
    """Start the optional embedded worker thread (disabled by default to avoid duplication)."""
    global _WORKER_THREAD
    with _WORKER_LOCK:
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return
        _WORKER_STOP_EVENT.clear()
        _WORKER_THREAD = threading.Thread(
            target=_embedded_worker_loop,
            name="finresolve-supervisor-worker",
            daemon=True,
        )
        _WORKER_THREAD.start()


def stop_embedded_supervisor_worker() -> None:
    """Stop the embedded worker thread cleanly."""
    with _WORKER_LOCK:
        _WORKER_STOP_EVENT.set()
        _WORKER_WAKE_EVENT.set()


def run_standalone_worker(poll_interval_seconds: float = 1.0) -> None:
    """Primary standalone Autonomous Supervisor worker (`python -m app.supervisor.worker`)."""
    logging.basicConfig(level=logging.INFO)
    worker_id = f"sup-wrk-{uuid.uuid4().hex[:8]}"
    stop_flag = threading.Event()

    def _handle_signal(_signum: int, _frame: object) -> None:
        logger.info("Shutdown signal received for worker %s", worker_id)
        stop_flag.set()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("Starting FinResolve Autonomous Supervisor standalone worker (%s)...", worker_id)
    try:
        while not stop_flag.is_set():
            try:
                res = process_next_supervisor_job(
                    worker_id=worker_id,
                    worker_mode="STANDALONE",
                )
                if res is not None:
                    logger.info("Worker %s processed job: %s", worker_id, res)
                    continue
            except Exception as exc:
                logger.warning("Worker %s iteration error: %s", worker_id, exc)
            stop_flag.wait(timeout=poll_interval_seconds)
    finally:
        _record_worker_stop(worker_id, "STANDALONE")
        logger.info("Standalone Supervisor worker %s stopped cleanly.", worker_id)


if __name__ == "__main__":
    run_standalone_worker()
