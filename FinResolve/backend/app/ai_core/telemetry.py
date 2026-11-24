"""Execution guard, cooperative cancellation, fencing, and Model Adapter telemetry."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session


class JobCancelledError(RuntimeError):
    """Raised when a running SupervisorJob has cooperative cancellation requested."""


class JobSupersededError(RuntimeError):
    """Raised when a running job loses fencing ownership, world_epoch, or active session."""


class BudgetExhaustedError(RuntimeError):
    """Raised when a session or investigation exhausts its configured LLM call or token budget."""


@dataclass(frozen=True)
class LlmInvocationRecord:
    """Exact telemetry recorded at the ModelAdapter invocation boundary."""

    provider: str
    model_name: str
    succeeded: bool
    timed_out: bool = False
    is_fallback: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    tokens_unknown: bool = False
    error_summary: str | None = None


@dataclass
class RuntimeExecutionContext:
    """Context-local hooks injected by the Autonomous Supervisor worker during job execution."""

    job_id: str
    session_id: str
    claim_token: str
    world_epoch: int
    check_guard: Callable[[str], None]
    before_llm_call: Callable[[], None]
    record_llm_outcome: Callable[[LlmInvocationRecord], None]
    emit_activity: Callable[[str, str, str | None, dict[str, Any] | None], None]
    release_llm_reservation: Callable[[], None] | None = None


_CURRENT_EXECUTION_CONTEXT: ContextVar[RuntimeExecutionContext | None] = ContextVar(
    "finresolve_supervisor_exec_ctx",
    default=None,
)


def get_execution_context() -> RuntimeExecutionContext | None:
    return _CURRENT_EXECUTION_CONTEXT.get()


def set_execution_context(ctx: RuntimeExecutionContext | None) -> Any:
    return _CURRENT_EXECUTION_CONTEXT.set(ctx)


def reset_execution_context(token: Any) -> None:
    _CURRENT_EXECUTION_CONTEXT.reset(token)


def assert_execution_fencing_in_txn(db: Session, *, session_id: str) -> None:
    """Verify claim_token, job status, world_epoch, and active DemoSession inside the SAME
    database transaction right before committing consequential business writes.

    Consistent lock ordering to prevent deadlocks:
        DemoSession -> SupervisorState -> SupervisorJob -> (domain rows)
    """
    ctx = get_execution_context()
    if ctx is None or not ctx.job_id or not ctx.claim_token:
        return
    if ctx.session_id != session_id:
        raise JobSupersededError("Execution context session_id mismatch during fenced write")

    from app.models.enums import SupervisorJobStatus
    from app.models.session import DemoSession
    from app.models.supervisor import SupervisorJob, SupervisorState

    # Lock ONLY SupervisorJob FOR UPDATE inside the writer transaction:
    # - Serializes with request_job_cancellation, recover_stale_supervisor_jobs,
    #   _JobHeartbeatThread, and bump_world_epoch_on_reset (which updates SupervisorJob).
    # - Avoids locking SupervisorState FOR UPDATE inside a writer txn that may invoke
    #   out-of-band telemetry/activity commits in a separate DB session (no self-deadlock).
    job = db.scalar(select(SupervisorJob).where(SupervisorJob.id == ctx.job_id).with_for_update())
    if job is None or job.claim_token != ctx.claim_token:
        raise JobSupersededError(
            "FENCING_VIOLATION: Worker lost claim_token ownership before write commit"
        )
    if (
        job.status in (SupervisorJobStatus.CANCELLING.value, SupervisorJobStatus.CANCELLED.value)
        or job.cancel_requested_at is not None
    ):
        raise JobCancelledError("Job cancellation detected inside fenced write transaction")
    if job.status != SupervisorJobStatus.RUNNING.value:
        raise JobSupersededError(f"Job is no longer RUNNING (status={job.status})")

    sess = db.scalar(select(DemoSession).where(DemoSession.id == session_id))
    if sess is None or not sess.is_active:
        raise JobSupersededError("DemoSession expired or inactive before fenced write commit")

    state = db.scalar(select(SupervisorState).where(SupervisorState.session_id == session_id))
    if state is None or state.world_epoch != ctx.world_epoch:
        raise JobSupersededError("world_epoch changed (session reset) before fenced write commit")


def sanitize_provider_error(exc: Exception) -> str:
    """Return a short, safe error summary without API keys, bearer tokens, or headers."""
    name = type(exc).__name__
    msg = str(exc)
    for marker in ("Bearer ", "x-goog-api-key", "x-api-key", "api_key", "AIza", "AQ.", "sk-"):
        if marker.lower() in msg.lower():
            return f"{name}: provider call failed (sensitive details redacted)"
    clean = " ".join(msg.split())
    return f"{name}: {clean[:220]}" if clean else name
