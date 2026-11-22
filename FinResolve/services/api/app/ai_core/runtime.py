"""In-house Bounded Agentic AI Runtime for FinResolve AI.

Hard Domain Invariants:
1. Self-built bounded loop: read context -> request plan -> validate -> run authorized READ tool
   or terminal step (ASK_USER / PROPOSE_ACTION / ESCALATE / STOP) -> persist Evidence & CaseEvent
   -> observe -> replan.
2. Enforces strict step limit (`max_steps=6`), replan limit (`max_replans=2`), wall-clock timeout,
   token budget, and duplicate tool-call loop prevention.
3. Never trusts customer claims or LLM assertions as payment proof:
   Every `PROPOSE_ACTION` is checked against authoritative `OBSERVED_FACT` records in PostgreSQL.
4. AI only proposes actions (`ActionProposal` with `status=PROPOSED`); it NEVER executes writes
   to `SimOrder`, `SimPayment`, or `SimRefund`, and NEVER transitions a Case to `RESOLVED`.
5. No hidden chain-of-thought is stored or exposed; only concise structured `decision_summary`,
   `known_evidence_refs`, `unknowns`, `hypotheses`, and tool provenance metadata are persisted.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai_core.context import ContextBuilder
from app.ai_core.model_adapter import ModelAdapter, get_model_adapter
from app.ai_core.schemas import (
    Hypothesis,
    InvestigationRunResult,
    InvestigationStepTrace,
    PlanStepKind,
    ToolObservationRecord,
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


class BoundedAgentRuntime:
    """Iterative, policy-guarded investigation runtime with hard step/time/token bounds."""

    def __init__(
        self,
        db: Session,
        *,
        adapter: ModelAdapter | None = None,
        max_steps: int = 6,
        max_replans: int = 2,
        max_wall_time_seconds: float = 15.0,
        token_budget: int | None = None,
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

    def investigate_case(
        self,
        *,
        case_id: str,
        session_id: str,
        simulate_timeout: bool = False,
    ) -> InvestigationRunResult:
        """Execute a bounded investigation loop for a session-scoped Case."""
        start_monotonic = time.monotonic()
        run_id = f"RUN-{uuid.uuid4().hex[:10].upper()}"

        case = self._db.scalar(
            select(Case).where(Case.id == case_id, Case.session_id == session_id)
        )
        if case is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"Case '{case_id}' not found",
                status_code=404,
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
        if current_state != CaseState.INVESTIGATING:
            if can_transition(current_state, CaseState.INVESTIGATING):
                case = transition_case(
                    self._db,
                    case,
                    CaseState.INVESTIGATING,
                    reason=f"Bắt đầu phiên điều tra AI ({run_id})",
                    extra_payload={
                        "run_id": run_id,
                        "model_mode": self._adapter.mode.value,
                        "adapter_name": self._adapter.adapter_name,
                    },
                )
            else:
                raise AppError(
                    ErrorCode.CONFLICT,
                    f"Cannot start investigation when Case is in terminal state '{case.state}'",
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
                "run_id": run_id,
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
                    "run_id": run_id,
                    "patterns": understanding.prompt_injection_patterns,
                    "action": "ISOLATED_AS_UNTRUSTED_DATA",
                },
            )

        context_builder = ContextBuilder(self._db, session_id)
        tool_registry = ReadToolRegistry(self._db, session_id)

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
                        "run_id": run_id,
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
                        "run_id": run_id,
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

            current_step_num = steps_executed + 1
            context = context_builder.build(
                case,
                step_number=current_step_num,
                max_steps=self._max_steps,
                tool_observations=tool_observations,
                understanding=understanding,
                validation_error_feedback=validation_feedback,
            )

            # 2. Request structured Plan from ModelAdapter (QA T16)
            try:
                invocation = self._adapter.generate_plan(context)
                validation_feedback = None
            except Exception as exc:  # noqa: BLE001
                replans_used += 1
                err_msg = str(exc)[:300]
                validation_feedback = f"Previous plan output was rejected: {err_msg}"
                self._record_event(
                    case=case,
                    event_type="PLAN_VALIDATION_FAILED",
                    payload={
                        "run_id": run_id,
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
                    break
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
                        "run_id": run_id,
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
                    "run_id": run_id,
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
                signature = f"{tool_name}:{json.dumps(tool_args, sort_keys=True)}"

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

                executed_tool_signatures.add(signature)
                tool_result = tool_registry.execute(
                    tool_name,
                    tool_args,
                    simulate_timeout=simulate_timeout,
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
                        "run_id": run_id,
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
                    break

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
                    run_id=run_id,
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
                    "run_id": run_id,
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
                "run_id": run_id,
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

        self._db.commit()
        self._db.refresh(case)

        return InvestigationRunResult(
            run_id=run_id,
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

        Invariants:
        - Never trusts customer claims or LLM assertions.
        - Verifies authoritative DB records and OBSERVED_FACT evidence before creating a proposal.
        - Never executes writes directly; only creates an ActionProposal(status=PROPOSED).
        """
        fact_evidence_ids = [obs.evidence_id for obs in tool_observations if obs.status == "OK"]
        normalized_action = (
            ActionType.ORDER_SYNC.value
            if action_name in ("propose_order_sync", "ORDER_SYNC")
            else ActionType.REFUND_EXECUTE.value
            if action_name in ("propose_refund", "REFUND_EXECUTE")
            else ActionType.HOLD_ORDER.value
        )

        # 1. Require that the planner actually read authoritative evidence first
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

        # 2. Validate ORDER_SYNC proposal against authoritative DB state
        if normalized_action == ActionType.ORDER_SYNC.value:
            order_id = str(args.get("order_id") or "").strip()
            payment_id = str(args.get("payment_id") or "").strip()

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

            # Check for ambiguous multiple payments on this order/amount
            matching_payments = list(
                self._db.scalars(
                    select(SimPayment).where(
                        SimPayment.session_id == session_id,
                        SimPayment.order_id == order_id,
                    )
                ).all()
            )

            is_payment_verified = (
                payment is not None
                and payment.status == PaymentStatus.SUCCESS.value
                and payment.verified_at is not None
                and order is not None
                and payment.amount == order.amount
                and payment.currency == order.currency
                and len(matching_payments) <= 1
            )

            if not is_payment_verified or order is None or payment is None:
                risk = self._create_risk_signal(
                    case=case,
                    session_id=session_id,
                    category=RiskCategory.UNVERIFIED_PAYMENT.value,
                    level=RiskLevel.HIGH.value,
                    response=RiskResponse.HOLD_ACTION.value,
                    evidence_refs=fact_evidence_ids,
                    details={
                        "attempted_action": "ORDER_SYNC",
                        "order_id": order_id,
                        "payment_id": payment_id,
                        "payment_status": payment.status if payment else "MISSING",
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
                        "reason": "PAYMENT_NOT_VERIFIED_SUCCESS",
                        "risk_signal_id": risk.id,
                    },
                )
                case = self._safe_transition(
                    case,
                    CaseState.WAITING_EVIDENCE,
                    reason=(
                        "Chặn đề xuất ORDER_SYNC: Bản ghi thanh toán chưa được xác thực SUCCESS "
                        "hoặc không khớp với đơn hàng."
                    ),
                )
                return case, "UNVERIFIED_PAYMENT_BLOCKED"

            proposal = ActionProposal(
                id=f"PROP-{uuid.uuid4().hex[:10].upper()}",
                case_id=case.id,
                session_id=session_id,
                action_type=ActionType.ORDER_SYNC.value,
                params_json={
                    "order_id": order.id,
                    "payment_id": payment.id,
                    "amount": payment.amount,
                    "currency": payment.currency,
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
            payment_id = str(args.get("payment_id") or "").strip()
            order_id = str(args.get("order_id") or "").strip()
            req_amount = int(args.get("amount") or 0)

            payment = self._db.scalar(
                select(SimPayment).where(
                    SimPayment.session_id == session_id,
                    SimPayment.id == payment_id,
                )
            )
            if payment is None and order_id:
                payment = self._db.scalar(
                    select(SimPayment).where(
                        SimPayment.session_id == session_id,
                        SimPayment.order_id == order_id,
                    )
                )

            if (
                payment is None
                or payment.status != PaymentStatus.SUCCESS.value
                or payment.verified_at is None
            ):
                risk = self._create_risk_signal(
                    case=case,
                    session_id=session_id,
                    category=RiskCategory.UNVERIFIED_PAYMENT.value,
                    level=RiskLevel.HIGH.value,
                    response=RiskResponse.HOLD_ACTION.value,
                    evidence_refs=fact_evidence_ids,
                    details={"reason": "Cannot propose refund on unverified or missing payment"},
                )
                risk_signal_ids.append(risk.id)
                case = self._safe_transition(
                    case,
                    CaseState.WAITING_EVIDENCE,
                    reason="Chặn đề xuất hoàn tiền: Giao dịch gốc chưa xác thực SUCCESS.",
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
            target_amount = req_amount if req_amount > 0 else payment.amount

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
                action_type=ActionType.REFUND_EXECUTE.value,
                params_json={
                    "payment_id": payment.id,
                    "order_id": payment.order_id,
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
