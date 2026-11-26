"""In-house Bounded Agentic AI Runtime for FinResolve AI.

Hard Domain Invariants:
1. Self-built bounded loop: read context -> request plan -> validate -> run authorized READ tool
   or terminal step (ASK_USER / PROPOSE_ACTION / ESCALATE / STOP) -> persist Evidence & CaseEvent
   -> observe -> replan.
2. Enforces strict step limit (`max_steps=6`), replan limit (`max_replans=2`), wall-clock timeout,
   token budget, `plan.case_id == case.id` verification, and duplicate tool-call loop prevention.
3. Never trusts customer claims or LLM assertions as payment proof:
   Every `PROPOSE_ACTION` is checked against authoritative `OBSERVED_FACT` provenance gathered
   in the current investigation AND verified against PostgreSQL relationships:
   - `payment.order_id == order.id` (an `ORDER_SYNC` or `REFUND_EXECUTE` can NEVER use a payment
     belonging to another order, even if amounts match).
   - Valid merchant relationship (`payment.merchant_id` present and verified), matching amount
     and currency, `payment.status == "SUCCESS"`, and `payment.verified_at IS NOT NULL`.
4. Defensive Argument Validation: Invalid model output or malformed argument types/references
   fail safely into `PLAN_VALIDATION_FAILED` / `UNSAFE_PROPOSAL_BLOCKED`, never HTTP 500.
5. AI only proposes actions (`ActionProposal` with `status=PROPOSED`); it NEVER executes writes
   to `SimOrder`, `SimPayment`, or `SimRefund`, and NEVER transitions a Case to `RESOLVED`.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.ai_core.context import ContextBuilder
from app.ai_core.model_adapter import ModelAdapter, get_model_adapter
from app.ai_core.schemas import (
    Hypothesis,
    InvestigationRunResult,
    InvestigationStepTrace,
    PlanStepKind,
    ToolObservationRecord,
    is_strict_int,
)
from app.ai_core.telemetry import (
    BudgetExhaustedError,
    JobCancelledError,
    JobSupersededError,
    LlmInvocationRecord,
    assert_execution_fencing_in_txn,
    get_execution_context,
)
from app.ai_core.understanding import analyze_vietnamese_complaint
from app.cases.state_machine import can_transition, transition_case
from app.connectors.read_tools import ReadToolRegistry
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.models.actions import ActionProposal
from app.models.cases import Case, CaseEvent, Evidence
from app.models.enums import (
    ActionProposalStatus,
    ActionType,
    CaseState,
    EvidenceKind,
    PaymentStatus,
    RefundStatus,
    RiskCategory,
    RiskLevel,
    RiskResponse,
)
from app.models.risk import RiskSignal
from app.models.simulator import SimOrder, SimPayment, SimRefund

MAX_INVESTIGATIONS_PER_CASE = 5


def recover_stale_investigation(
    db: Session,
    *,
    case_id: str,
    session_id: str,
    run_id: str | None = None,
    stale_after_seconds: int | None = None,
) -> list[str]:
    """Idempotently persist `INVESTIGATION_INTERRUPTED` and recover `Case` from `INVESTIGATING`.

    Requirements (Prompt 08.3.1 Fix 2):
    - Uses short PostgreSQL `FOR UPDATE NOWAIT` row lock only when a stale unfinished run exists.
    - Revalidates `session_id`, `run_id`, Case state, and latest events under lock.
    - Persists AT MOST ONE `INVESTIGATION_INTERRUPTED` terminal event per interrupted run.
    - Moves `Case` from `INVESTIGATING` -> `ESCALATED` (via `transition_case`, bumping `version`
      so any late-returning crashed worker fails optimistic version checks and cannot overwrite).
    - Never marks `Case` as `RESOLVED` or `COMPLETED`, and never interrupts a healthy active run.
    """
    timeout_s = (
        stale_after_seconds
        if stale_after_seconds is not None
        else get_settings().demo_stale_run_timeout_seconds
    )
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=max(1, timeout_s))

    # Fast read-only check before taking any write lock (avoids mutations on healthy polling)
    evts = list(
        db.scalars(
            select(CaseEvent)
            .where(
                CaseEvent.case_id == case_id,
                CaseEvent.session_id == session_id,
                CaseEvent.type.in_(
                    [
                        "INVESTIGATION_QUEUED",
                        "INVESTIGATION_STARTED",
                        "AI_PLAN_GENERATED",
                        "TOOL_READ_COMPLETED",
                        "INVESTIGATION_COMPLETED",
                        "INVESTIGATION_FAILED",
                        "INVESTIGATION_INTERRUPTED",
                    ]
                ),
            )
            .order_by(CaseEvent.created_at.asc(), CaseEvent.id.asc())
        ).all()
    )
    if not evts:
        return []

    last_heartbeat: dict[str, datetime] = {}
    terminal_runs: set[str] = set()
    for ev in evts:
        rid = str(ev.payload_json.get("run_id") or "")
        if not rid:
            continue
        if run_id is not None and rid != run_id:
            continue
        ts = ev.created_at if ev.created_at.tzinfo else ev.created_at.replace(tzinfo=UTC)
        if ev.type in (
            "INVESTIGATION_COMPLETED",
            "INVESTIGATION_FAILED",
            "INVESTIGATION_INTERRUPTED",
        ):
            terminal_runs.add(rid)
        else:
            prev = last_heartbeat.get(rid)
            if prev is None or ts > prev:
                last_heartbeat[rid] = ts

    candidate_stale = [
        rid for rid, hb in last_heartbeat.items() if rid not in terminal_runs and hb <= cutoff
    ]
    if not candidate_stale:
        return []

    # Acquire short row lock on Case to re-verify and atomically recover
    try:
        locked_case = db.scalar(
            select(Case)
            .where(Case.id == case_id, Case.session_id == session_id)
            .with_for_update(nowait=True)
        )
    except OperationalError:
        db.rollback()
        return []

    if locked_case is None:
        db.rollback()
        return []

    # Re-read events under lock for strict idempotency
    locked_evts = list(
        db.scalars(
            select(CaseEvent)
            .where(
                CaseEvent.case_id == locked_case.id,
                CaseEvent.session_id == session_id,
                CaseEvent.type.in_(
                    [
                        "INVESTIGATION_QUEUED",
                        "INVESTIGATION_STARTED",
                        "AI_PLAN_GENERATED",
                        "TOOL_READ_COMPLETED",
                        "INVESTIGATION_COMPLETED",
                        "INVESTIGATION_FAILED",
                        "INVESTIGATION_INTERRUPTED",
                    ]
                ),
            )
            .order_by(CaseEvent.created_at.asc(), CaseEvent.id.asc())
        ).all()
    )
    hb_locked: dict[str, datetime] = {}
    term_locked: set[str] = set()
    for ev in locked_evts:
        rid = str(ev.payload_json.get("run_id") or "")
        if not rid or (run_id is not None and rid != run_id):
            continue
        ts = ev.created_at if ev.created_at.tzinfo else ev.created_at.replace(tzinfo=UTC)
        if ev.type in (
            "INVESTIGATION_COMPLETED",
            "INVESTIGATION_FAILED",
            "INVESTIGATION_INTERRUPTED",
        ):
            term_locked.add(rid)
        else:
            prev = hb_locked.get(rid)
            if prev is None or ts > prev:
                hb_locked[rid] = ts

    recovered_ids: list[str] = []
    for rid, hb in hb_locked.items():
        if rid in term_locked or hb > cutoff:
            continue
        db.add(
            CaseEvent(
                id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                case_id=locked_case.id,
                session_id=session_id,
                type="INVESTIGATION_INTERRUPTED",
                payload_json={
                    "run_id": rid,
                    "job_status": "INTERRUPTED",
                    "reason": "STALE_WORKER_TIMEOUT",
                    "error": (
                        "Phiên điều tra bị gián đoạn (worker timeout). Đã khôi phục Case về "
                        "trạng thái an toàn ESCALATED; không có hành động tài chính nào được ghi."
                    ),
                    "last_heartbeat_at": hb.isoformat(),
                },
                created_at=now,
            )
        )
        recovered_ids.append(rid)

    if not recovered_ids:
        db.rollback()
        return []

    # Recover Case from INVESTIGATING -> ESCALATED (bumping case.version so late old worker fails!)
    if locked_case.state == CaseState.INVESTIGATING.value and can_transition(
        CaseState.INVESTIGATING, CaseState.ESCALATED
    ):
        transition_case(
            db,
            locked_case,
            CaseState.ESCALATED,
            reason=(
                f"Khôi phục tự động sau khi worker điều tra bị gián đoạn ({recovered_ids[0]}). "
                "Cho phép điều tra lại khi hạn mức cho phép."
            ),
            extra_payload={"recovered_run_ids": recovered_ids},
        )

    db.commit()
    return recovered_ids


class BoundedAgentRuntime:
    """Iterative, policy-guarded investigation runtime with hard step/time/token bounds."""

    def __init__(
        self,
        db: Session,
        *,
        adapter: ModelAdapter | None = None,
        max_steps: int = 6,
        max_replans: int = 2,
        max_wall_time_seconds: float = 45.0,
        token_budget: int | None = None,
        manage_transactions: bool = True,
        stale_investigation_timeout_seconds: int = 120,
    ) -> None:
        settings = get_settings()
        self._db = db
        self._adapter = adapter or get_model_adapter(settings)
        self._max_steps = max(1, min(max_steps, 12))
        self._max_replans = max(0, min(max_replans, 5))
        self._max_wall_time_seconds = max(0.05, max_wall_time_seconds)
        self._token_budget = (
            token_budget if token_budget is not None else settings.model_budget_tokens
        )
        self._manage_transactions = manage_transactions
        self._stale_investigation_timeout_seconds = max(1, stale_investigation_timeout_seconds)

    def _maybe_commit(self) -> None:
        """Commit the current short transaction unless an outer caller owns transaction scope."""
        if self._manage_transactions:
            self._db.commit()

    def _reacquire_investigating_case(
        self,
        *,
        case_id: str,
        session_id: str,
        expected_version: int,
        run_id: str,
    ) -> Case:
        """Re-lock Case in a short write transaction after LLM I/O and verify optimistic version."""
        assert_execution_fencing_in_txn(self._db, session_id=session_id)
        try:
            fresh = self._db.scalar(
                select(Case)
                .where(Case.id == case_id, Case.session_id == session_id)
                .with_for_update(nowait=True)
            )
        except OperationalError as err:
            self._db.rollback()
            raise AppError(
                ErrorCode.CONFLICT,
                f"CONCURRENT_CASE_MODIFICATION: Case '{case_id}' is locked by another writer",
                status_code=409,
                details={"case_id": case_id, "run_id": run_id},
            ) from err

        if fresh is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"Case '{case_id}' no longer exists in session '{session_id}'",
                status_code=404,
            )

        if fresh.state != CaseState.INVESTIGATING.value or fresh.version != expected_version:
            self._record_event(
                case=fresh,
                event_type="INVESTIGATION_INTERRUPTED",
                payload={
                    "run_id": run_id,
                    "job_status": "INTERRUPTED",
                    "reason": "CONCURRENT_CASE_STATE_OR_VERSION_CHANGE",
                    "expected_version": expected_version,
                    "actual_version": fresh.version,
                    "actual_state": fresh.state,
                },
            )
            self._maybe_commit()
            raise AppError(
                ErrorCode.CONFLICT,
                f"INVESTIGATION_INTERRUPTED: Case '{case_id}' changed concurrently "
                f"(expected version {expected_version}/INVESTIGATING, got "
                f"{fresh.version}/{fresh.state})",
                status_code=409,
                details={
                    "case_id": case_id,
                    "run_id": run_id,
                    "expected_version": expected_version,
                    "actual_version": fresh.version,
                    "actual_state": fresh.state,
                },
            )
        return fresh

    def investigate_case(
        self,
        *,
        case_id: str,
        session_id: str,
        simulate_timeout: bool = False,
        run_id: str | None = None,
    ) -> InvestigationRunResult:
        """Execute a bounded investigation loop for a session-scoped Case."""
        start_monotonic = time.monotonic()
        effective_run_id = run_id or f"RUN-{uuid.uuid4().hex[:10].upper()}"

        # Prevent overlapping concurrent investigations on the same Case using NOWAIT row lock
        assert_execution_fencing_in_txn(self._db, session_id=session_id)
        try:
            case = self._db.scalar(
                select(Case)
                .where(Case.id == case_id, Case.session_id == session_id)
                .with_for_update(nowait=True)
            )
        except OperationalError as err:
            self._db.rollback()
            raise AppError(
                ErrorCode.CONFLICT,
                f"OVERLAPPING_INVESTIGATION_BLOCKED: Another investigation is currently "
                f"running on Case '{case_id}'",
                status_code=409,
                details={"case_id": case_id},
            ) from err

        if case is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"Case '{case_id}' not found",
                status_code=404,
            )

        # Check for an already-queued or active unfinished investigation run on this Case
        lifecycle_evts = list(
            self._db.scalars(
                select(CaseEvent).where(
                    CaseEvent.case_id == case.id,
                    CaseEvent.session_id == session_id,
                    CaseEvent.type.in_(
                        [
                            "INVESTIGATION_QUEUED",
                            "INVESTIGATION_STARTED",
                            "INVESTIGATION_COMPLETED",
                            "INVESTIGATION_FAILED",
                            "INVESTIGATION_INTERRUPTED",
                        ]
                    ),
                )
            ).all()
        )
        now_utc = datetime.now(UTC)
        stale_cutoff = now_utc - timedelta(seconds=self._stale_investigation_timeout_seconds)
        started_runs_ts: dict[str, datetime] = {}
        queued_runs_ts: dict[str, datetime] = {}
        finished_run_ids: set[str] = set()
        for l_evt in lifecycle_evts:
            r_id = str(l_evt.payload_json.get("run_id") or "")
            if not r_id:
                continue
            ev_ts = (
                l_evt.created_at
                if l_evt.created_at.tzinfo
                else l_evt.created_at.replace(tzinfo=UTC)
            )
            if l_evt.type == "INVESTIGATION_STARTED":
                started_runs_ts[r_id] = ev_ts
            elif l_evt.type == "INVESTIGATION_QUEUED":
                queued_runs_ts[r_id] = ev_ts
            elif l_evt.type in (
                "INVESTIGATION_COMPLETED",
                "INVESTIGATION_FAILED",
                "INVESTIGATION_INTERRUPTED",
            ):
                finished_run_ids.add(r_id)

        # Auto-recover stale/interrupted worker runs so a crashed worker never wedges a Case
        stale_recovered: set[str] = set()
        for cand_run_id, cand_ts in {**queued_runs_ts, **started_runs_ts}.items():
            if cand_run_id in finished_run_ids or cand_run_id == effective_run_id:
                continue
            if cand_ts <= stale_cutoff:
                self._record_event(
                    case=case,
                    event_type="INVESTIGATION_INTERRUPTED",
                    payload={
                        "run_id": cand_run_id,
                        "job_status": "INTERRUPTED",
                        "reason": "STALE_WORKER_INTERRUPTED_RECOVERED",
                    },
                )
                stale_recovered.add(cand_run_id)
                finished_run_ids.add(cand_run_id)

        unfinished_started = set(started_runs_ts.keys()) - finished_run_ids
        unfinished_queued = (set(queued_runs_ts.keys()) - finished_run_ids) - {effective_run_id}
        active_unfinished = unfinished_started | unfinished_queued
        if active_unfinished:
            active_evt_run_id = sorted(active_unfinished)[0]
            raise AppError(
                ErrorCode.CONFLICT,
                f"OVERLAPPING_INVESTIGATION_BLOCKED: Case '{case.id}' already has an "
                f"active investigation run ('{active_evt_run_id}')",
                status_code=409,
                details={"case_id": case.id, "active_run_id": active_evt_run_id},
            )

        # QA T19: Enforce per-case / per-session investigation rate limit
        existing_runs_count = (
            self._db.scalar(
                select(func.count(CaseEvent.id)).where(
                    CaseEvent.case_id == case.id,
                    CaseEvent.session_id == session_id,
                    CaseEvent.type == "INVESTIGATION_STARTED",
                )
            )
            or 0
        )
        if existing_runs_count >= MAX_INVESTIGATIONS_PER_CASE:
            raise AppError(
                ErrorCode.RATE_LIMITED,
                f"Investigation quota exceeded for Case '{case.id}' "
                f"(maximum {MAX_INVESTIGATIONS_PER_CASE} runs per case)",
                status_code=429,
                details={
                    "case_id": case.id,
                    "max_investigations_per_case": MAX_INVESTIGATIONS_PER_CASE,
                    "runs_count": existing_runs_count,
                },
            )

        # Transition Case to INVESTIGATING if not already there
        current_state = CaseState(case.state)
        if (
            current_state == CaseState.INVESTIGATING
            and existing_runs_count > 0
            and not stale_recovered
        ):
            raise AppError(
                ErrorCode.CONFLICT,
                f"OVERLAPPING_INVESTIGATION_BLOCKED: Case '{case.id}' is already in "
                "INVESTIGATING state",
                status_code=409,
                details={"case_id": case.id, "state": case.state},
            )
        if current_state != CaseState.INVESTIGATING:
            if can_transition(current_state, CaseState.INVESTIGATING):
                case = transition_case(
                    self._db,
                    case,
                    CaseState.INVESTIGATING,
                    reason=f"Bắt đầu phiên điều tra AI ({effective_run_id})",
                    extra_payload={
                        "run_id": effective_run_id,
                        "model_mode": self._adapter.mode.value,
                        "adapter_name": self._adapter.adapter_name,
                    },
                )
            else:
                raise AppError(
                    ErrorCode.CONFLICT,
                    f"Cannot start investigation when Case is in state '{case.state}'",
                    status_code=409,
                )

        # Extract Vietnamese intake understanding from initial CLAIM evidence
        claim_ev = self._db.scalar(
            select(Evidence)
            .where(
                Evidence.case_id == case.id,
                Evidence.session_id == session_id,
                Evidence.kind == EvidenceKind.CLAIM.value,
            )
            .order_by(Evidence.observed_at.asc())
            .limit(1)
        )
        intake_text = (
            str(claim_ev.payload_json.get("text") or case.goal)
            if claim_ev is not None
            else case.goal
        )
        understanding = analyze_vietnamese_complaint(intake_text, case_type=case.type)

        self._record_event(
            case=case,
            event_type="INVESTIGATION_STARTED",
            payload={
                "run_id": effective_run_id,
                "job_status": "RUNNING",
                "model_mode": self._adapter.mode.value,
                "adapter_name": self._adapter.adapter_name,
                "is_fallback": self._adapter.mode.value == "DEMO_RULES",
                "max_steps": self._max_steps,
                "max_replans": self._max_replans,
                "understanding": understanding.model_dump(mode="json"),
            },
        )

        if understanding.prompt_injection_detected:
            self._record_event(
                case=case,
                event_type="PROMPT_INJECTION_GUARD_TRIGGERED",
                payload={
                    "run_id": effective_run_id,
                    "patterns": understanding.prompt_injection_patterns,
                    "action": "ISOLATED_AS_UNTRUSTED_DATA",
                },
            )

        # Prompt 08.2: Commit short claim transaction IMMEDIATELY so NO PostgreSQL row lock
        # is held while waiting for external LLM API calls!
        expected_case_version = case.version
        self._maybe_commit()

        context_builder = ContextBuilder(self._db, session_id)
        tool_registry = ReadToolRegistry(
            self._db,
            session_id,
            source_system=case.source_system,
            source_binding_id=case.source_binding_id,
        )

        tool_observations: list[ToolObservationRecord] = []
        step_traces: list[InvestigationStepTrace] = []
        executed_tool_signatures: set[str] = set()
        proposed_action_ids: list[str] = []
        risk_signal_ids: list[str] = []

        steps_executed = 0
        replans_used = 0
        total_tokens_used = 0
        validation_feedback: str | None = None
        last_decision_summary = ""
        last_unknowns: list[str] = []
        last_hypotheses: list[Hypothesis] = []
        stop_reason = "STEP_LIMIT_REACHED"

        while steps_executed < self._max_steps:
            # 1. Check wall-clock time and token budgets (QA T17)
            elapsed_s = time.monotonic() - start_monotonic
            if elapsed_s > self._max_wall_time_seconds:
                stop_reason = "TIME_BUDGET_EXCEEDED"
                last_decision_summary = (
                    "Dừng điều tra do vượt quá giới hạn thời gian "
                    f"({self._max_wall_time_seconds}s)."
                )
                self._record_event(
                    case=case,
                    event_type="BUDGET_EXHAUSTED",
                    payload={
                        "run_id": effective_run_id,
                        "reason": stop_reason,
                        "elapsed_seconds": round(elapsed_s, 3),
                        "max_wall_time_seconds": self._max_wall_time_seconds,
                    },
                )
                case = self._safe_transition(
                    case,
                    CaseState.ESCALATED,
                    reason=last_decision_summary,
                )
                break

            if self._token_budget > 0 and total_tokens_used >= self._token_budget:
                stop_reason = "TOKEN_BUDGET_EXCEEDED"
                last_decision_summary = (
                    "Dừng điều tra do đạt giới hạn token "
                    f"({total_tokens_used}/{self._token_budget})."
                )
                self._record_event(
                    case=case,
                    event_type="BUDGET_EXHAUSTED",
                    payload={
                        "run_id": effective_run_id,
                        "reason": stop_reason,
                        "total_tokens_used": total_tokens_used,
                        "token_budget": self._token_budget,
                    },
                )
                case = self._safe_transition(
                    case,
                    CaseState.ESCALATED,
                    reason=last_decision_summary,
                )
                break

            exec_ctx = get_execution_context()
            if exec_ctx is not None:
                exec_ctx.check_guard(f"before_plan_step_{steps_executed + 1}")
                if steps_executed == 0:
                    exec_ctx.emit_activity(
                        "INVESTIGATION_STARTED",
                        f"Bắt đầu điều tra hồ sơ {case_id}",
                        f"Mục tiêu: {case.goal[:240]}",
                        {"case_id": case_id, "run_id": effective_run_id},
                    )
                elif validation_feedback:
                    exec_ctx.emit_activity(
                        "PLAN_REVISED",
                        f"AI điều chỉnh kế hoạch điều tra (bước {steps_executed + 1})",
                        validation_feedback[:240],
                        {"case_id": case_id, "step": steps_executed + 1},
                    )

            current_step_num = steps_executed + 1
            case_snapshot = self._db.scalar(
                select(Case).where(Case.id == case_id, Case.session_id == session_id)
            )
            if case_snapshot is None:
                raise AppError(
                    ErrorCode.NOT_FOUND,
                    f"Case '{case_id}' no longer exists",
                    status_code=404,
                )
            context = context_builder.build(
                case_snapshot,
                step_number=current_step_num,
                max_steps=self._max_steps,
                tool_observations=tool_observations,
                understanding=understanding,
                validation_error_feedback=validation_feedback,
            )
            # Release any read transaction BEFORE calling external LLM so zero DB locks/txns
            # are held across network I/O.
            if self._manage_transactions:
                self._db.rollback()

            # 2. Request structured Plan from ModelAdapter & verify plan.case_id (QA T16 & Req 3)
            plan_error: str | None = None
            invocation = None
            try:
                invocation = self._adapter.generate_plan(context)
                if not isinstance(invocation.plan.case_id, str) or (
                    invocation.plan.case_id != case_id
                ):
                    raise ValueError(
                        f"Planner case_id mismatch: expected '{case_id}', "
                        f"got '{invocation.plan.case_id}'"
                    )
                if exec_ctx is not None and invocation.is_fallback:
                    exec_ctx.record_llm_outcome(
                        LlmInvocationRecord(
                            provider=invocation.provider,
                            model_name=invocation.model_name,
                            succeeded=True,
                            is_fallback=True,
                            prompt_tokens=0,
                            completion_tokens=0,
                            total_tokens=0,
                            tokens_unknown=False,
                        )
                    )
                validation_feedback = None
            except (JobCancelledError, JobSupersededError, BudgetExhaustedError):
                raise
            except Exception as exc:  # noqa: BLE001
                plan_error = str(exc)[:300]

            if exec_ctx is not None:
                exec_ctx.check_guard(f"after_plan_step_{current_step_num}")

            # Re-acquire Case in a short write transaction and validate optimistic version
            case = self._reacquire_investigating_case(
                case_id=case_id,
                session_id=session_id,
                expected_version=expected_case_version,
                run_id=effective_run_id,
            )

            if plan_error is not None or invocation is None:
                replans_used += 1
                err_msg = plan_error or "Unknown planner validation error"
                validation_feedback = f"Previous plan output was rejected: {err_msg}"
                self._record_event(
                    case=case,
                    event_type="PLAN_VALIDATION_FAILED",
                    payload={
                        "run_id": effective_run_id,
                        "step": current_step_num,
                        "replan_attempt": replans_used,
                        "max_replans": self._max_replans,
                        "error": err_msg,
                    },
                )
                if replans_used > self._max_replans:
                    stop_reason = "MAX_REPLANS_EXCEEDED"
                    last_decision_summary = (
                        "Dừng điều tra an toàn: Đầu ra từ planner không hợp lệ sau "
                        f"{self._max_replans} lần thử lại. Không có công cụ trái phép nào được gọi."
                    )
                    case = self._safe_transition(
                        case,
                        CaseState.ESCALATED,
                        reason=last_decision_summary,
                    )
                    expected_case_version = case.version
                    self._maybe_commit()
                    break
                self._maybe_commit()
                continue

            total_tokens_used += invocation.total_tokens
            if self._token_budget > 0 and total_tokens_used > self._token_budget:
                stop_reason = "TOKEN_BUDGET_EXCEEDED"
                last_decision_summary = (
                    "Dừng điều tra do vượt ngân sách token "
                    f"({total_tokens_used}/{self._token_budget})."
                )
                self._record_event(
                    case=case,
                    event_type="BUDGET_EXHAUSTED",
                    payload={
                        "run_id": effective_run_id,
                        "reason": stop_reason,
                        "total_tokens_used": total_tokens_used,
                        "token_budget": self._token_budget,
                    },
                )
                case = self._safe_transition(
                    case,
                    CaseState.ESCALATED,
                    reason=last_decision_summary,
                )
                break

            plan = invocation.plan
            steps_executed += 1
            last_decision_summary = plan.decision_summary
            last_unknowns = list(plan.unknowns)
            last_hypotheses = list(plan.hypotheses)

            # Persist planner step decision trace (never exposing hidden chain-of-thought)
            self._record_event(
                case=case,
                event_type="AI_PLAN_GENERATED",
                payload={
                    "run_id": effective_run_id,
                    "step": steps_executed,
                    "model_mode": invocation.model_mode.value,
                    "adapter_name": invocation.adapter_name,
                    "is_fallback": invocation.is_fallback,
                    "goal": plan.goal,
                    "known_evidence_refs": plan.known_evidence_refs,
                    "unknowns": plan.unknowns,
                    "hypotheses": [h.model_dump(mode="json") for h in plan.hypotheses],
                    "next_step": plan.next_step.model_dump(mode="json"),
                    "decision_summary": plan.decision_summary,
                    "tokens": {
                        "prompt": invocation.prompt_tokens,
                        "completion": invocation.completion_tokens,
                        "total": invocation.total_tokens,
                    },
                    "latency_ms": invocation.latency_ms,
                },
            )

            # 3. Dispatch NextStep
            step_kind = plan.next_step.kind

            if step_kind == PlanStepKind.READ_TOOL:
                tool_name = str(plan.next_step.tool_name)
                tool_args = dict(plan.next_step.args)
                signature = f"{tool_name}:{json.dumps(tool_args, sort_keys=True, default=str)}"

                # Prevent duplicate tool call loops
                if signature in executed_tool_signatures:
                    replans_used += 1
                    validation_feedback = (
                        f"Tool '{tool_name}' with identical args was already called. "
                        "Choose a different tool or terminal step."
                    )
                    if replans_used > self._max_replans:
                        stop_reason = "DUPLICATE_TOOL_LOOP_PREVENTED"
                        last_decision_summary = (
                            "Dừng vòng lặp: Planner yêu cầu gọi lặp lại cùng một công cụ đọc."
                        )
                        case = self._safe_transition(
                            case,
                            CaseState.ESCALATED,
                            reason=last_decision_summary,
                        )
                        step_traces.append(
                            InvestigationStepTrace(
                                step_number=steps_executed,
                                model_mode=invocation.model_mode,
                                adapter_name=invocation.adapter_name,
                                is_fallback=invocation.is_fallback,
                                plan=plan,
                                tool_observation=None,
                            )
                        )
                        break
                    continue

                if exec_ctx is not None:
                    exec_ctx.check_guard(f"before_tool_{tool_name}")
                    exec_ctx.emit_activity(
                        "TOOL_INVOKED",
                        f"AI gọi công cụ đọc dữ liệu ({tool_name})",
                        f"Đang đối chiếu nguồn xác thực cho hồ sơ {case.id}.",
                        {"tool_name": tool_name, "step": steps_executed},
                    )
                executed_tool_signatures.add(signature)
                tool_result = tool_registry.execute(
                    tool_name,
                    tool_args,
                    simulate_timeout=simulate_timeout,
                )
                if exec_ctx is not None:
                    exec_ctx.check_guard(f"after_tool_{tool_name}")
                    exec_ctx.emit_activity(
                        "TOOL_RESULT_RECEIVED",
                        f"Nhận kết quả công cụ {tool_name} ({tool_result.status})",
                        f"Nguồn: {tool_result.source} ({tool_result.source_ref})",
                        {
                            "tool_name": tool_name,
                            "status": tool_result.status,
                            "source_ref": tool_result.source_ref,
                        },
                    )

                ev_kind = (
                    EvidenceKind.UNKNOWN.value
                    if tool_result.status == "TIMEOUT"
                    else EvidenceKind.OBSERVED_FACT.value
                )
                new_ev = Evidence(
                    id=f"EVD-{uuid.uuid4().hex[:10].upper()}",
                    case_id=case.id,
                    session_id=session_id,
                    kind=ev_kind,
                    source=tool_result.source,
                    source_ref=tool_result.source_ref,
                    payload_json={
                        "tool_name": tool_name,
                        "status": tool_result.status,
                        "source_mode": tool_result.source_mode,
                        "freshness_ms": tool_result.freshness_ms,
                        "data": tool_result.data,
                    },
                    confidence=1.0 if ev_kind == EvidenceKind.OBSERVED_FACT.value else None,
                    observed_at=tool_result.observed_at,
                )
                self._db.add(new_ev)
                self._db.flush()

                obs_record = ToolObservationRecord(
                    step=steps_executed,
                    tool_name=tool_name,
                    args=tool_args,
                    status=tool_result.status,
                    evidence_id=new_ev.id,
                    source=tool_result.source,
                    source_ref=tool_result.source_ref,
                    source_mode=tool_result.source_mode,
                    freshness_ms=tool_result.freshness_ms,
                    observed_at=tool_result.observed_at,
                    data=tool_result.data,
                )
                tool_observations.append(obs_record)
                step_traces.append(
                    InvestigationStepTrace(
                        step_number=steps_executed,
                        model_mode=invocation.model_mode,
                        adapter_name=invocation.adapter_name,
                        is_fallback=invocation.is_fallback,
                        plan=plan,
                        tool_observation=obs_record,
                    )
                )

                self._record_event(
                    case=case,
                    event_type="TOOL_READ_COMPLETED",
                    payload={
                        "run_id": effective_run_id,
                        "step": steps_executed,
                        "tool_name": tool_name,
                        "args": tool_args,
                        "status": tool_result.status,
                        "evidence_id": new_ev.id,
                        "evidence_kind": ev_kind,
                        "source": tool_result.source,
                        "source_ref": tool_result.source_ref,
                        "source_mode": tool_result.source_mode,
                        "freshness_ms": tool_result.freshness_ms,
                    },
                )

                # QA T04: On provider timeout, mark Case UNKNOWN_OUTCOME (no blind retry)
                if tool_result.status == "TIMEOUT":
                    stop_reason = "PROVIDER_TIMEOUT_UNKNOWN"
                    last_decision_summary = (
                        f"Truy vấn công cụ '{tool_name}' bị quá thời gian (TIMEOUT). "
                        "Giữ trạng thái UNKNOWN_OUTCOME, không thực hiện ghi hoặc thử lại mù quáng."
                    )
                    case = self._safe_transition(
                        case,
                        CaseState.UNKNOWN_OUTCOME,
                        reason=last_decision_summary,
                    )
                    expected_case_version = case.version
                    self._maybe_commit()
                    break

                # Commit short step transaction so Evidence & Trace are durable and row lock is
                # released before the next LLM step.
                expected_case_version = case.version
                self._maybe_commit()
                continue

            # Record non-tool step trace
            step_traces.append(
                InvestigationStepTrace(
                    step_number=steps_executed,
                    model_mode=invocation.model_mode,
                    adapter_name=invocation.adapter_name,
                    is_fallback=invocation.is_fallback,
                    plan=plan,
                    tool_observation=None,
                )
            )

            if step_kind == PlanStepKind.ASK_USER:
                stop_reason = "WAITING_FOR_USER_EVIDENCE"
                case = self._safe_transition(
                    case,
                    CaseState.WAITING_EVIDENCE,
                    reason=plan.decision_summary,
                    extra_payload={"question": plan.next_step.args.get("question")},
                )
                break

            if step_kind == PlanStepKind.PROPOSE_ACTION:
                case, stop_reason = self._handle_propose_action(
                    case=case,
                    session_id=session_id,
                    run_id=effective_run_id,
                    action_name=str(plan.next_step.action_type or plan.next_step.tool_name),
                    args=plan.next_step.args,
                    decision_summary=plan.decision_summary,
                    tool_observations=tool_observations,
                    proposed_action_ids=proposed_action_ids,
                    risk_signal_ids=risk_signal_ids,
                )
                break

            if step_kind == PlanStepKind.ESCALATE:
                stop_reason = "ESCALATED_BY_PLANNER"
                self._maybe_record_escalation_risk_signal(
                    case=case,
                    session_id=session_id,
                    args=plan.next_step.args,
                    tool_observations=tool_observations,
                    risk_signal_ids=risk_signal_ids,
                )
                case = self._safe_transition(
                    case,
                    CaseState.ESCALATED,
                    reason=plan.decision_summary,
                )
                break

            if step_kind == PlanStepKind.STOP:
                stop_reason = "PLANNER_STOPPED"
                if case.state == CaseState.INVESTIGATING.value:
                    target = (
                        CaseState.UNKNOWN_OUTCOME
                        if plan.next_step.args.get("reason") == "PROVIDER_TIMEOUT"
                        else CaseState.WAITING_EVIDENCE
                    )
                    case = self._safe_transition(
                        case,
                        target,
                        reason=plan.decision_summary,
                    )
                break

        else:
            # Exited while loop because steps_executed reached self._max_steps (QA T17)
            stop_reason = "STEP_LIMIT_REACHED"
            last_decision_summary = (
                f"Đạt giới hạn số bước điều tra tối đa ({self._max_steps} bước) "
                "mà chưa có kết luận cuối cùng; chuyển sang ESCALATED."
            )
            self._record_event(
                case=case,
                event_type="STEP_BUDGET_REACHED",
                payload={
                    "run_id": effective_run_id,
                    "steps_executed": steps_executed,
                    "max_steps": self._max_steps,
                },
            )
            case = self._safe_transition(
                case,
                CaseState.ESCALATED,
                reason=last_decision_summary,
            )

        # Finalize investigation run event
        all_evidence_ids = list(
            self._db.scalars(
                select(Evidence.id)
                .where(Evidence.case_id == case.id, Evidence.session_id == session_id)
                .order_by(Evidence.observed_at.asc())
            ).all()
        )

        self._record_event(
            case=case,
            event_type="INVESTIGATION_COMPLETED",
            payload={
                "run_id": effective_run_id,
                "job_status": "COMPLETED",
                "final_state": case.state,
                "stop_reason": stop_reason,
                "steps_executed": steps_executed,
                "replans_used": replans_used,
                "total_tokens_used": total_tokens_used,
                "model_mode": self._adapter.mode.value,
                "adapter_name": self._adapter.adapter_name,
                "is_fallback": self._adapter.mode.value == "DEMO_RULES",
                "decision_summary": last_decision_summary,
                "known_evidence_refs": all_evidence_ids,
                "unknowns": last_unknowns,
                "proposed_action_ids": proposed_action_ids,
                "risk_signal_ids": risk_signal_ids,
            },
        )

        self._maybe_commit()
        if self._manage_transactions:
            self._db.refresh(case)

        return InvestigationRunResult(
            run_id=effective_run_id,
            case_id=case.id,
            session_id=session_id,
            final_state=case.state,
            case_version=case.version,
            model_mode=self._adapter.mode,
            adapter_name=self._adapter.adapter_name,
            is_fallback=(self._adapter.mode.value == "DEMO_RULES"),
            steps_executed=steps_executed,
            replans_used=replans_used,
            total_tokens_used=total_tokens_used,
            stop_reason=stop_reason,
            decision_summary=last_decision_summary,
            known_evidence_refs=all_evidence_ids,
            unknowns=last_unknowns,
            hypotheses=last_hypotheses,
            understanding=understanding,
            step_traces=step_traces,
            proposed_action_ids=proposed_action_ids,
            risk_signal_ids=risk_signal_ids,
        )

    @staticmethod
    def _collect_authoritative_provenance(
        tool_observations: list[ToolObservationRecord],
    ) -> tuple[set[str], dict[str, dict[str, Any]], list[str]]:
        """Extract verified entity IDs and provenance from OBSERVED_FACT tool observations.

        Returns `(observed_order_ids, observed_payments_by_id, authoritative_evidence_ids)`.
        Only includes orders that were unambiguously found (`found=True, is_ambiguous=False`)
        and payments from non-ambiguous provider queries (`is_ambiguous=False`).
        """
        observed_order_ids: set[str] = set()
        observed_payments: dict[str, dict[str, Any]] = {}
        evidence_ids: list[str] = []

        for obs in tool_observations:
            if obs.status != "OK":
                continue
            evidence_ids.append(obs.evidence_id)
            if obs.tool_name == "get_order":
                if obs.data.get("found") is True and not obs.data.get("is_ambiguous"):
                    oid = obs.data.get("order_id")
                    if isinstance(oid, str) and oid.strip():
                        observed_order_ids.add(oid.strip())
            elif obs.tool_name == "find_payment_candidates":
                if not obs.data.get("is_ambiguous") and not obs.data.get(
                    "unanchored_amount_search"
                ):
                    cands = obs.data.get("candidates")
                    if isinstance(cands, list) and len(cands) == 1:
                        cand = cands[0]
                        if isinstance(cand, dict):
                            pid = cand.get("payment_id")
                            if isinstance(pid, str) and pid.strip():
                                observed_payments[pid.strip()] = cand
            elif obs.tool_name == "get_payment_record":
                if obs.data.get("found") is True:
                    pid = obs.data.get("payment_id")
                    if isinstance(pid, str) and pid.strip():
                        observed_payments[pid.strip()] = obs.data

        return observed_order_ids, observed_payments, evidence_ids

    def _handle_propose_action(
        self,
        *,
        case: Case,
        session_id: str,
        run_id: str,
        action_name: str,
        args: dict[str, Any],
        decision_summary: str,
        tool_observations: list[ToolObservationRecord],
        proposed_action_ids: list[str],
        risk_signal_ids: list[str],
    ) -> tuple[Case, str]:
        """Deterministic Backend Policy Gate before persisting any AI ActionProposal.

        Hard Invariants:
        1. Never trusts customer claims or LLM assertions.
        2. Verifies argument types defensively (never raises HTTP 500 on malformed model args).
        3. Verifies authoritative `OBSERVED_FACT` provenance: the target `order_id` and
           `payment_id` must have been read from authoritative tools during this Case's run.
        4. Verifies Order / Payment / Merchant relationships in PostgreSQL:
           - `payment.order_id == order.id` (an `ORDER_SYNC` or `REFUND_EXECUTE` can NEVER use a
             successful payment belonging to another order).
           - Valid merchant ID (`payment.merchant_id`), matching `amount` and `currency`,
             `payment.status == "SUCCESS"`, and `payment.verified_at IS NOT NULL`.
        5. Never executes writes directly; only creates an `ActionProposal(status=PROPOSED)`.
        """
        observed_orders, observed_payments, fact_evidence_ids = (
            self._collect_authoritative_provenance(tool_observations)
        )

        normalized_action = (
            ActionType.ORDER_SYNC.value
            if action_name in ("propose_order_sync", "ORDER_SYNC")
            else ActionType.REFUND_EXECUTE.value
            if action_name in ("propose_refund", "REFUND_EXECUTE")
            else ActionType.HOLD_ORDER.value
        )

        # 1. Require authoritative tool observations first
        if not fact_evidence_ids:
            risk = self._create_risk_signal(
                case=case,
                session_id=session_id,
                category=RiskCategory.UNVERIFIED_PAYMENT.value,
                level=RiskLevel.HIGH.value,
                response=RiskResponse.HOLD_ACTION.value,
                evidence_refs=[],
                details={"reason": "AI attempted to propose action without reading any tool facts"},
            )
            risk_signal_ids.append(risk.id)
            self._record_event(
                case=case,
                event_type="UNSAFE_PROPOSAL_BLOCKED",
                payload={
                    "run_id": run_id,
                    "attempted_action": normalized_action,
                    "reason": "NO_OBSERVED_FACT_EVIDENCE",
                    "risk_signal_id": risk.id,
                },
            )
            case = self._safe_transition(
                case,
                CaseState.WAITING_EVIDENCE,
                reason="Chặn đề xuất hành động: Chưa có bằng chứng OBSERVED_FACT từ hệ thống.",
            )
            return case, "UNVERIFIED_PROPOSAL_BLOCKED"

        # 2. Validate ORDER_SYNC proposal
        if normalized_action == ActionType.ORDER_SYNC.value:
            raw_order_id = args.get("order_id")
            raw_payment_id = args.get("payment_id")
            raw_amount = args.get("amount")
            raw_exp_version = args.get("expected_order_version")

            if (
                not isinstance(raw_order_id, str)
                or not raw_order_id.strip()
                or not isinstance(raw_payment_id, str)
                or not raw_payment_id.strip()
                or (raw_amount is not None and (not is_strict_int(raw_amount) or raw_amount <= 0))
                or (
                    raw_exp_version is not None
                    and (not is_strict_int(raw_exp_version) or raw_exp_version < 1)
                )
            ):
                self._record_event(
                    case=case,
                    event_type="UNSAFE_PROPOSAL_BLOCKED",
                    payload={
                        "run_id": run_id,
                        "attempted_action": "ORDER_SYNC",
                        "reason": "INVALID_ACTION_ARGUMENTS",
                    },
                )
                case = self._safe_transition(
                    case,
                    CaseState.ESCALATED,
                    reason="Chặn đề xuất ORDER_SYNC do tham số đầu ra từ model không hợp lệ.",
                )
                return case, "INVALID_ACTION_ARGUMENTS_BLOCKED"

            order_id = raw_order_id.strip()
            payment_id = raw_payment_id.strip()

            # For real external COMMERCE_HTTP cases, validate authoritative MiniStore records
            # observed via HTTP tools (rather than local SimOrder/SimPayment simulator tables).
            if case.source_system == "COMMERCE_HTTP" and case.source_binding_id:
                from app.models.monitoring import CommerceSourceBinding
                from app.monitoring.service import build_connector_for_binding

                binding = self._db.get(CommerceSourceBinding, case.source_binding_id)
                http_order: dict[str, Any] | None = None
                http_payment: dict[str, Any] | None = None
                if binding is not None:
                    try:
                        conn = build_connector_for_binding(binding)
                        http_order = conn.get_order(order_id)["data"]
                        http_payment = conn.get_payment(payment_id)["data"]
                    except Exception:  # noqa: BLE001
                        http_order = None
                        http_payment = None

                http_prov_ok = (
                    (order_id in observed_orders)
                    or (payment_id in observed_payments)
                    or any(
                        t.status == "OK"
                        and (
                            t.args.get("order_id") == order_id
                            or t.args.get("payment_id") == payment_id
                        )
                        for t in tool_observations
                    )
                )
                http_verified = (
                    http_prov_ok
                    and isinstance(http_order, dict)
                    and isinstance(http_payment, dict)
                    and str(http_payment.get("order_id") or "") == order_id
                    and str(http_payment.get("status") or "").upper() in ("SUCCEEDED", "SUCCESS")
                    and int(http_payment.get("amount_vnd") or 0)
                    == int(http_order.get("total_vnd") or -1)
                    and http_order.get("version") is not None
                )
                if not http_verified or http_order is None or http_payment is None:
                    risk = self._create_risk_signal(
                        case=case,
                        session_id=session_id,
                        category=RiskCategory.UNVERIFIED_PAYMENT.value,
                        level=RiskLevel.HIGH.value,
                        response=RiskResponse.HOLD_ACTION.value,
                        evidence_refs=fact_evidence_ids,
                        details={
                            "attempted_action": "ORDER_SYNC",
                            "block_reason": "COMMERCE_HTTP_PAYMENT_NOT_VERIFIED",
                            "order_id": order_id,
                            "payment_id": payment_id,
                        },
                    )
                    risk_signal_ids.append(risk.id)
                    self._record_event(
                        case=case,
                        event_type="UNSAFE_PROPOSAL_BLOCKED",
                        payload={
                            "run_id": run_id,
                            "attempted_action": "ORDER_SYNC",
                            "reason": "COMMERCE_HTTP_PAYMENT_NOT_VERIFIED",
                            "risk_signal_id": risk.id,
                        },
                    )
                    case = self._safe_transition(
                        case,
                        CaseState.WAITING_EVIDENCE,
                        reason="Chặn ORDER_SYNC: dữ liệu Commerce chưa xác nhận thanh toán.",
                    )
                    return case, "UNVERIFIED_PAYMENT_BLOCKED"

                ord_ver = int(http_order["version"])
                proposal = ActionProposal(
                    id=f"PROP-{uuid.uuid4().hex[:10].upper()}",
                    case_id=case.id,
                    session_id=session_id,
                    investigation_run_id=run_id,
                    action_type=ActionType.ORDER_SYNC.value,
                    params_json={
                        "order_id": order_id,
                        "payment_id": payment_id,
                        "merchant_id": binding.merchant_id if binding else "",
                        "amount": int(http_payment.get("amount_vnd") or 0),
                        "currency": "VND",
                        "expected_version": ord_ver,
                        "expected_order_version": ord_ver,
                    },
                    evidence_refs=fact_evidence_ids,
                    expected_version=case.version,
                    risk_level=RiskLevel.LOW.value,
                    status=ActionProposalStatus.PROPOSED.value,
                    created_at=datetime.now(UTC),
                )
                self._db.add(proposal)
                self._db.flush()
                proposed_action_ids.append(proposal.id)
                self._record_event(
                    case=case,
                    event_type="ACTION_PROPOSED",
                    payload={
                        "run_id": run_id,
                        "proposal_id": proposal.id,
                        "action_type": proposal.action_type,
                        "params_json": proposal.params_json,
                        "evidence_refs": proposal.evidence_refs,
                        "status": proposal.status,
                    },
                )
                case = self._safe_transition(
                    case,
                    CaseState.WAITING_APPROVAL,
                    reason=decision_summary,
                    extra_payload={"proposal_id": proposal.id, "action_type": proposal.action_type},
                )
                return case, "ACTION_PROPOSED"

            # Check authoritative evidence provenance: both order_id and payment_id must have
            # been observed in this investigation's tool reads!
            has_provenance = (order_id in observed_orders) and (payment_id in observed_payments)

            order = self._db.scalar(
                select(SimOrder).where(
                    SimOrder.session_id == session_id,
                    SimOrder.id == order_id,
                )
            )
            payment = self._db.scalar(
                select(SimPayment).where(
                    SimPayment.session_id == session_id,
                    SimPayment.id == payment_id,
                )
            )

            matching_payments = list(
                self._db.scalars(
                    select(SimPayment).where(
                        SimPayment.session_id == session_id,
                        SimPayment.order_id == order_id,
                    )
                ).all()
            )

            # HARD INVARIANT 1:
            # - payment.order_id MUST equal order.id (never use another order's payment!)
            # - payment.merchant_id must be valid and non-empty
            # - authoritative evidence provenance must exist
            # - payment must be verified SUCCESS with matching amount and currency
            is_same_order_payment = (
                order is not None
                and payment is not None
                and payment.order_id is not None
                and payment.order_id == order.id
            )
            is_merchant_valid = (
                payment is not None
                and bool(payment.merchant_id and payment.merchant_id.strip())
                and payment.source_mode == (order.source_mode if order else "MOCK")
            )
            is_payment_verified = (
                has_provenance
                and is_same_order_payment
                and is_merchant_valid
                and payment is not None
                and order is not None
                and payment.status == PaymentStatus.SUCCESS.value
                and payment.verified_at is not None
                and payment.result_code == 0
                and payment.amount == order.amount
                and payment.currency == order.currency
                and (raw_amount is None or int(raw_amount) == order.amount)
                and len(matching_payments) == 1
                and matching_payments[0].id == payment.id
            )

            if not is_payment_verified or order is None or payment is None:
                block_reason = (
                    "MISSING_AUTHORITATIVE_EVIDENCE_PROVENANCE"
                    if not has_provenance
                    else "CROSS_ORDER_PAYMENT_MISMATCH"
                    if (payment is not None and order is not None and payment.order_id != order.id)
                    else "PAYMENT_NOT_VERIFIED_SUCCESS"
                )
                risk = self._create_risk_signal(
                    case=case,
                    session_id=session_id,
                    category=RiskCategory.UNVERIFIED_PAYMENT.value,
                    level=RiskLevel.HIGH.value,
                    response=RiskResponse.HOLD_ACTION.value,
                    evidence_refs=fact_evidence_ids,
                    details={
                        "attempted_action": "ORDER_SYNC",
                        "block_reason": block_reason,
                        "order_id": order_id,
                        "payment_id": payment_id,
                        "payment_bound_order_id": payment.order_id if payment else None,
                        "payment_merchant_id": payment.merchant_id if payment else None,
                        "payment_status": payment.status if payment else "MISSING",
                        "has_provenance": has_provenance,
                        "verified_at": (
                            payment.verified_at.isoformat()
                            if (payment and payment.verified_at)
                            else None
                        ),
                    },
                )
                risk_signal_ids.append(risk.id)
                self._record_event(
                    case=case,
                    event_type="UNSAFE_PROPOSAL_BLOCKED",
                    payload={
                        "run_id": run_id,
                        "attempted_action": "ORDER_SYNC",
                        "reason": block_reason,
                        "risk_signal_id": risk.id,
                    },
                )
                case = self._safe_transition(
                    case,
                    CaseState.WAITING_EVIDENCE,
                    reason=(
                        f"Chặn đề xuất ORDER_SYNC ({block_reason}): Bản ghi thanh toán không thuộc "
                        "đơn hàng này, thiếu provenance hoặc chưa được xác thực SUCCESS."
                    ),
                )
                return case, "UNVERIFIED_PAYMENT_BLOCKED"

            proposal = ActionProposal(
                id=f"PROP-{uuid.uuid4().hex[:10].upper()}",
                case_id=case.id,
                session_id=session_id,
                investigation_run_id=run_id,
                action_type=ActionType.ORDER_SYNC.value,
                params_json={
                    "order_id": order.id,
                    "payment_id": payment.id,
                    "merchant_id": payment.merchant_id,
                    "amount": payment.amount,
                    "currency": payment.currency,
                    "expected_version": order.version,
                    "expected_order_version": order.version,
                },
                evidence_refs=fact_evidence_ids,
                expected_version=case.version,
                risk_level=RiskLevel.LOW.value,
                status=ActionProposalStatus.PROPOSED.value,
                created_at=datetime.now(UTC),
            )
            self._db.add(proposal)
            self._db.flush()
            proposed_action_ids.append(proposal.id)

            self._record_event(
                case=case,
                event_type="ACTION_PROPOSED",
                payload={
                    "run_id": run_id,
                    "proposal_id": proposal.id,
                    "action_type": proposal.action_type,
                    "params_json": proposal.params_json,
                    "evidence_refs": proposal.evidence_refs,
                    "status": proposal.status,
                },
            )
            case = self._safe_transition(
                case,
                CaseState.WAITING_APPROVAL,
                reason=decision_summary,
                extra_payload={"proposal_id": proposal.id, "action_type": proposal.action_type},
            )
            return case, "ACTION_PROPOSED"

        # 3. Validate REFUND_EXECUTE proposal against authoritative payment & refund history
        if normalized_action == ActionType.REFUND_EXECUTE.value:
            raw_payment_id = args.get("payment_id")
            raw_order_id = args.get("order_id")
            raw_amount = args.get("amount")

            if (
                not isinstance(raw_payment_id, str)
                or not raw_payment_id.strip()
                or (raw_order_id is not None and not isinstance(raw_order_id, str))
                or not is_strict_int(raw_amount)
                or int(raw_amount) <= 0
            ):
                self._record_event(
                    case=case,
                    event_type="UNSAFE_PROPOSAL_BLOCKED",
                    payload={
                        "run_id": run_id,
                        "attempted_action": "REFUND_EXECUTE",
                        "reason": "INVALID_ACTION_ARGUMENTS",
                    },
                )
                case = self._safe_transition(
                    case,
                    CaseState.ESCALATED,
                    reason="Chặn đề xuất hoàn tiền do tham số đầu ra từ model không hợp lệ.",
                )
                return case, "INVALID_ACTION_ARGUMENTS_BLOCKED"

            payment_id = raw_payment_id.strip()
            order_id = raw_order_id.strip() if isinstance(raw_order_id, str) else ""
            req_amount = int(raw_amount)

            has_provenance = (payment_id in observed_payments) and (
                not order_id or order_id in observed_orders
            )

            payment = self._db.scalar(
                select(SimPayment).where(
                    SimPayment.session_id == session_id,
                    SimPayment.id == payment_id,
                )
            )

            is_order_match = not order_id or (payment is not None and payment.order_id == order_id)

            if (
                not has_provenance
                or not is_order_match
                or payment is None
                or payment.status != PaymentStatus.SUCCESS.value
                or payment.verified_at is None
                or not (payment.merchant_id and payment.merchant_id.strip())
            ):
                risk = self._create_risk_signal(
                    case=case,
                    session_id=session_id,
                    category=RiskCategory.UNVERIFIED_PAYMENT.value,
                    level=RiskLevel.HIGH.value,
                    response=RiskResponse.HOLD_ACTION.value,
                    evidence_refs=fact_evidence_ids,
                    details={
                        "reason": (
                            "Cannot propose refund on unverified, cross-order, "
                            "or unprovenanced payment"
                        ),
                        "payment_id": payment_id,
                        "order_id": order_id,
                        "has_provenance": has_provenance,
                        "is_order_match": is_order_match,
                    },
                )
                risk_signal_ids.append(risk.id)
                self._record_event(
                    case=case,
                    event_type="UNSAFE_PROPOSAL_BLOCKED",
                    payload={
                        "run_id": run_id,
                        "attempted_action": "REFUND_EXECUTE",
                        "reason": "UNVERIFIED_OR_CROSS_ORDER_PAYMENT",
                        "risk_signal_id": risk.id,
                    },
                )
                case = self._safe_transition(
                    case,
                    CaseState.WAITING_EVIDENCE,
                    reason=(
                        "Chặn đề xuất hoàn tiền: Giao dịch gốc chưa xác thực SUCCESS, "
                        "không khớp đơn hàng hoặc thiếu provenance."
                    ),
                )
                return case, "UNVERIFIED_PAYMENT_BLOCKED"

            existing_refunds = self._db.scalars(
                select(SimRefund).where(
                    SimRefund.session_id == session_id,
                    SimRefund.payment_id == payment.id,
                    SimRefund.status.in_(
                        [
                            RefundStatus.COMPLETED.value,
                            RefundStatus.PENDING.value,
                            RefundStatus.UNKNOWN.value,
                        ]
                    ),
                )
            ).all()

            used_balance = sum(
                r.completed_amount
                if r.status == RefundStatus.COMPLETED.value
                else r.requested_amount
                for r in existing_refunds
            )
            remaining_balance = payment.amount - used_balance
            target_amount = req_amount

            if remaining_balance <= 0 or target_amount > remaining_balance:
                cat = (
                    RiskCategory.DUPLICATE_REFUND.value
                    if remaining_balance <= 0
                    else RiskCategory.LIMIT_EXCEEDED.value
                )
                risk = self._create_risk_signal(
                    case=case,
                    session_id=session_id,
                    category=cat,
                    level=RiskLevel.HIGH.value,
                    response=RiskResponse.HOLD_ACTION.value,
                    evidence_refs=fact_evidence_ids,
                    details={
                        "payment_id": payment.id,
                        "payment_amount": payment.amount,
                        "used_balance": used_balance,
                        "remaining_balance": remaining_balance,
                        "requested_amount": target_amount,
                    },
                )
                risk_signal_ids.append(risk.id)
                self._record_event(
                    case=case,
                    event_type="UNSAFE_PROPOSAL_BLOCKED",
                    payload={
                        "run_id": run_id,
                        "attempted_action": "REFUND_EXECUTE",
                        "reason": cat,
                        "risk_signal_id": risk.id,
                    },
                )
                case = self._safe_transition(
                    case,
                    CaseState.ESCALATED,
                    reason=(
                        f"Chặn đề xuất hoàn tiền ({cat}): Số dư khả dụng = "
                        f"{remaining_balance} VND, yêu cầu = {target_amount} VND."
                    ),
                )
                return case, "REFUND_RISK_BLOCKED"

            proposal_risk = (
                RiskLevel.HIGH.value if target_amount > 500000 else RiskLevel.MEDIUM.value
            )
            proposal = ActionProposal(
                id=f"PROP-{uuid.uuid4().hex[:10].upper()}",
                case_id=case.id,
                session_id=session_id,
                investigation_run_id=run_id,
                action_type=ActionType.REFUND_EXECUTE.value,
                params_json={
                    "payment_id": payment.id,
                    "order_id": payment.order_id,
                    "merchant_id": payment.merchant_id,
                    "amount": target_amount,
                    "currency": payment.currency,
                    "reason": str(args.get("reason") or decision_summary),
                },
                evidence_refs=fact_evidence_ids,
                expected_version=case.version,
                risk_level=proposal_risk,
                status=ActionProposalStatus.PROPOSED.value,
                created_at=datetime.now(UTC),
            )
            self._db.add(proposal)
            self._db.flush()
            proposed_action_ids.append(proposal.id)

            self._record_event(
                case=case,
                event_type="ACTION_PROPOSED",
                payload={
                    "run_id": run_id,
                    "proposal_id": proposal.id,
                    "action_type": proposal.action_type,
                    "params_json": proposal.params_json,
                    "evidence_refs": proposal.evidence_refs,
                    "status": proposal.status,
                },
            )
            case = self._safe_transition(
                case,
                CaseState.WAITING_APPROVAL,
                reason=decision_summary,
                extra_payload={"proposal_id": proposal.id, "action_type": proposal.action_type},
            )
            return case, "ACTION_PROPOSED"

        # 4. HOLD_ORDER proposal
        proposal = ActionProposal(
            id=f"PROP-{uuid.uuid4().hex[:10].upper()}",
            case_id=case.id,
            session_id=session_id,
            investigation_run_id=run_id,
            action_type=ActionType.HOLD_ORDER.value,
            params_json=dict(args),
            evidence_refs=fact_evidence_ids,
            expected_version=case.version,
            risk_level=RiskLevel.MEDIUM.value,
            status=ActionProposalStatus.PROPOSED.value,
            created_at=datetime.now(UTC),
        )
        self._db.add(proposal)
        self._db.flush()
        proposed_action_ids.append(proposal.id)
        case = self._safe_transition(
            case,
            CaseState.WAITING_APPROVAL,
            reason=decision_summary,
            extra_payload={"proposal_id": proposal.id, "action_type": proposal.action_type},
        )
        return case, "ACTION_PROPOSED"

    def _maybe_record_escalation_risk_signal(
        self,
        *,
        case: Case,
        session_id: str,
        args: dict[str, Any],
        tool_observations: list[ToolObservationRecord],
        risk_signal_ids: list[str],
    ) -> None:
        """Record a scoped RiskSignal (REVIEW / HOLD_ACTION) on refund or payment risk."""
        fact_ids = [obs.evidence_id for obs in tool_observations]
        raw_cat = str(args.get("risk_category") or "").strip().upper()

        has_refund_obs = any(obs.tool_name == "get_refund_history" for obs in tool_observations)
        if not raw_cat and not has_refund_obs:
            return

        if raw_cat in ("DUPLICATE_REFUND", "PENDING_REFUND_CONFLICT"):
            category = RiskCategory.DUPLICATE_REFUND.value
            response = RiskResponse.HOLD_ACTION.value
            level = RiskLevel.HIGH.value
        elif raw_cat == "LIMIT_EXCEEDED":
            category = RiskCategory.LIMIT_EXCEEDED.value
            response = RiskResponse.HOLD_ACTION.value
            level = RiskLevel.HIGH.value
        else:
            category = RiskCategory.ANOMALOUS_PATTERN.value
            response = RiskResponse.REVIEW.value
            level = RiskLevel.MEDIUM.value

        risk = self._create_risk_signal(
            case=case,
            session_id=session_id,
            category=category,
            level=level,
            response=response,
            evidence_refs=fact_ids,
            details={
                "note": (
                    "Tín hiệu rủi ro phục vụ kiểm soát nghiệp vụ (REVIEW/HOLD_ACTION); "
                    "không phải kết luận gian lận của khách hàng."
                ),
                **args,
            },
        )
        risk_signal_ids.append(risk.id)

    def _create_risk_signal(
        self,
        *,
        case: Case,
        session_id: str,
        category: str,
        level: str,
        response: str,
        evidence_refs: list[str],
        details: dict[str, Any],
    ) -> RiskSignal:
        risk = RiskSignal(
            id=f"RSK-{uuid.uuid4().hex[:10].upper()}",
            case_id=case.id,
            session_id=session_id,
            category=category,
            level=level,
            evidence_refs=evidence_refs,
            response=response,
            details_json=details,
            created_at=datetime.now(UTC),
        )
        self._db.add(risk)
        self._db.flush()
        return risk

    def _record_event(
        self,
        *,
        case: Case,
        event_type: str,
        payload: dict[str, Any],
    ) -> CaseEvent:
        evt = CaseEvent(
            id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
            case_id=case.id,
            session_id=case.session_id,
            type=event_type,
            payload_json=payload,
            created_at=datetime.now(UTC),
        )
        self._db.add(evt)
        self._db.flush()
        return evt

    def _safe_transition(
        self,
        case: Case,
        target_state: CaseState,
        *,
        reason: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> Case:
        current_enum = CaseState(case.state)
        if current_enum == target_state:
            return case
        if can_transition(current_enum, target_state):
            return transition_case(
                self._db,
                case,
                target_state,
                reason=reason,
                extra_payload=extra_payload,
            )
        return case
