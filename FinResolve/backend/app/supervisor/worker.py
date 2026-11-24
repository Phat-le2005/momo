"""Standalone and embedded background worker for the Autonomous Agent Supervisor (Prompt 09).

Usage (standalone worker process):
    python -m app.supervisor.worker
"""

from __future__ import annotations

import logging
import threading
import time

from app.supervisor.service import process_next_supervisor_job

logger = logging.getLogger("finresolve.supervisor.worker")

_WORKER_STOP_EVENT = threading.Event()
_WORKER_WAKE_EVENT = threading.Event()
_WORKER_THREAD: threading.Thread | None = None
_WORKER_LOCK = threading.Lock()


def notify_supervisor_worker() -> None:
    """Wake the background Supervisor worker immediately when a new event is queued."""
    _WORKER_WAKE_EVENT.set()


def _embedded_worker_loop(poll_interval_seconds: float = 1.5) -> None:
    while not _WORKER_STOP_EVENT.is_set():
        try:
            res = process_next_supervisor_job(worker_id="sup-embedded")
            if res is not None:
                # Immediately check if more work is queued before sleeping
                continue
        except Exception as exc:  # pragma: no cover - defensive daemon guard
            logger.warning("Supervisor worker iteration error: %s", exc)
        _WORKER_WAKE_EVENT.wait(timeout=poll_interval_seconds)
        _WORKER_WAKE_EVENT.clear()


def start_embedded_supervisor_worker() -> None:
    """Start the lightweight background thread worker inside the API process."""
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
    """Stop the embedded background thread worker cleanly."""
    with _WORKER_LOCK:
        _WORKER_STOP_EVENT.set()
        _WORKER_WAKE_EVENT.set()


def run_standalone_worker(poll_interval_seconds: float = 1.0) -> None:
    """Entrypoint for running a dedicated Supervisor worker (`python -m app.supervisor.worker`)."""
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting FinResolve Autonomous Agent Supervisor standalone worker...")
    try:
        while True:
            res = process_next_supervisor_job(worker_id="sup-standalone")
            if res is not None:
                logger.info("Processed Supervisor job: %s", res)
                continue
            time.sleep(poll_interval_seconds)
    except KeyboardInterrupt:
        logger.info("Stopping standalone Supervisor worker.")


if __name__ == "__main__":
    run_standalone_worker()
