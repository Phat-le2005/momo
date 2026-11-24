"""Execution guard, cooperative cancellation, and Model Adapter telemetry (Prompt 09.1)."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


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


def sanitize_provider_error(exc: Exception) -> str:
    """Return a short, safe error summary without API keys, bearer tokens, or headers."""
    name = type(exc).__name__
    msg = str(exc)
    for marker in ("Bearer ", "x-goog-api-key", "x-api-key", "api_key", "AIza", "AQ.", "sk-"):
        if marker.lower() in msg.lower():
            return f"{name}: provider call failed (sensitive details redacted)"
    clean = " ".join(msg.split())
    return f"{name}: {clean[:220]}" if clean else name
