"""In-House TaskforceManager allocating 2-3 bounded logical roles with typed JSON handoffs.

Hard Domain Invariants (Prompt 05 & Prompt 05.1 Hardening):
1. Evidence-Derived Findings Only (No Hardcoded Positive Conclusions):
   - Conclusions in `TaskHandoffPayload` (`findings_vi`, `has_in_flight_refund`,
     `observed_payment_statuses`, `recommended_per_case_action`) are derived strictly from the
     actual `ToolResult.data` returned by `ScopedRoleToolExecutor`.
   - If an inspected payment is `FAILED`/`PENDING`/`UNKNOWN` or has an in-flight `PENDING`/`UNKNOWN`
     refund, the handoff accurately reports the anomaly and recommends holding/reconciling.
2. Honest Sample Tracking (`checked_case_ids` vs `unchecked_case_ids` & `UNKNOWN`):
   - `checked_case_ids` lists ONLY the Case IDs for which the role actually invoked read tools.
   - `unchecked_case_ids` lists all remaining Case IDs in the Incident that were NOT inspected
     by tool calls, and `per_case_verification_status` marks every unchecked Case as `"UNKNOWN"`.
   - Never claims all incident Cases were verified when only sample Cases were inspected.
3. Honest Deterministic vs LLM Runtime Mode Reporting:
   - `TaskforceManager` runs deterministic in-house role orchestration (`llm_invoked=False`,
     `runtime_mode="DEMO_RULES"`, `taskforce_execution_mode="DETERMINISTIC_TASKFORCE_RULES"`).
   - Even if `Settings.model_mode` is configured as `LLM_API`, taskforce handoffs never claim
     `LLM_API` reasoning unless an LLM model call was actually executed.
4. Conditional Role Selection (2-3 Bounded Roles):
   - Dynamically selects 2 or 3 roles from `{FINANCE, OPERATIONS, RISK, SUPPORT}` based on
     incident evidence, payment/refund anomalies, unknowns, and blast radius while preserving
     each role's strict `CapabilityRegistry` read-tool allowlist.
5. Never Auto-Verifies or Auto-Resolves Child Cases (QA I03):
   - Child Cases remain in their independent states and require per-case PolicyEngine, Approval,
     ActionExecutor, and post-commit Verifier before transitioning to `RESOLVED`.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.ai_core.model_adapter import ModelAdapter, get_model_adapter
from app.ai_core.telemetry import (
    BudgetExhaustedError,
    JobCancelledError,
    JobSupersededError,
    assert_execution_fencing_in_txn,
)
from app.connectors.read_tools import ReadToolRegistry
from app.core.config import ModelMode, get_settings
from app.core.errors import AppError, ErrorCode
from app.incidents.capabilities import CapabilityRegistry, ScopedRoleToolExecutor
from app.incidents.correlator import sanitize_no_oracle_leak
from app.models.cases import Case, CaseEvent
from app.models.enums import AgentTaskState, IncidentStatus, PaymentStatus, TaskforceRole
from app.models.incidents import AgentTask, Incident, IncidentCase
from app.models.simulator import SimRefund


class ToolObservationSummary(BaseModel):
    case_id: str | None = None
    tool_name: str
    status: str
    source: str
    source_ref: str
    summary: dict[str, Any] = Field(default_factory=dict)


class TaskHandoffPayload(BaseModel):
    """Typed JSON handoff contract between logical taskforce roles."""

    handoff_version: str = "FinResolve-Taskforce-Handoff-v1.1"
    incident_id: str
    from_role: str
    to_role: str
    runtime_mode: str = "DEMO_RULES"
    configured_model_mode: str = "DEMO_RULES"
    taskforce_execution_mode: str = "DETERMINISTIC_TASKFORCE_RULES"
    llm_invoked: bool = False
    checked_case_ids: list[str]
    unchecked_case_ids: list[str] = Field(default_factory=list)
    per_case_verification_status: dict[str, str] = Field(default_factory=dict)
    tool_observations: list[ToolObservationSummary]
    findings_vi: str
    recommended_per_case_action: str
    auto_resolved_children: bool = False
    requires_per_case_approval_and_verification: bool = True


class TaskforcePlanOutput(BaseModel):
    """Pydantic-validated LLM role & objective plan for Adaptive Taskforce (Prompt 08.2)."""

    model_config = ConfigDict(extra="forbid")

    selected_roles: list[str] = Field(..., min_length=2, max_length=3)
    role_objectives: dict[str, str] = Field(default_factory=dict)
    initial_hypothesis: str = Field(..., min_length=5, max_length=1000)


class RoleHandoffSynthesis(BaseModel):
    """Pydantic-validated per-role LLM synthesis grounded in observed tool outputs."""

    model_config = ConfigDict(extra="forbid")

    findings_vi: str = Field(..., min_length=5, max_length=1500)
    contradictions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    recommended_per_case_action: str = Field(..., min_length=3, max_length=500)
    request_replan: bool = False
    suggested_next_role: str | None = None


class TaskforceManager:
    """Conditionally selects and executes 2-3 bounded role tasks for an Incident.

    When `MODEL_MODE=LLM_API`, uses `ModelAdapter.generate_structured_json` for dynamic role
    selection, evidence-grounded per-role synthesis, and bounded replanning (max 1 replan),
    while enforcing strict `CapabilityRegistry` read-tool allowlists and zero auto-resolution.
    When no LLM key is configured, runs deterministic `DETERMINISTIC_TASKFORCE_RULES` honestly.
    """

    def __init__(
        self,
        db: Session,
        session_id: str,
        *,
        adapter: ModelAdapter | None = None,
        max_llm_calls: int = 5,
        token_budget: int | None = None,
        manage_transactions: bool = True,
    ) -> None:
        settings = get_settings()
        self._db = db
        self._session_id = session_id
        self._adapter = adapter or get_model_adapter(settings)
        self._max_llm_calls = max(1, min(max_llm_calls, 8))
        self._token_budget = (
            token_budget if token_budget is not None else settings.model_budget_tokens
        )
        self._manage_transactions = manage_transactions

    def _release_read_transaction(self) -> None:
        """Close any open read-only DB transaction before external LLM network I/O."""
        if (
            self._manage_transactions
            and not self._db.new
            and not self._db.dirty
            and not self._db.deleted
        ):
            self._db.rollback()

    def select_roles_for_incident(
        self,
        incident: Incident,
        inc_cases: list[IncidentCase],
    ) -> tuple[list[str], dict[str, Any]]:
        """Conditionally select 2-3 bounded roles based on incident evidence and unknowns."""
        case_ids = [ic.case_id for ic in inc_cases]
        cases_db = (
            list(
                self._db.scalars(
                    select(Case).where(
                        Case.session_id == self._session_id,
                        Case.id.in_(case_ids),
                    )
                ).all()
            )
            if case_ids
            else []
        )

        payment_ids = [
            str(ic.causal_factors_json.get("payment_id"))
            for ic in inc_cases
            if ic.causal_factors_json.get("payment_id")
        ]
        order_ids = [
            str(ic.causal_factors_json.get("order_id"))
            for ic in inc_cases
            if ic.causal_factors_json.get("order_id")
        ]

        existing_refunds_count = 0
        if payment_ids:
            existing_refunds_count = len(
                list(
                    self._db.scalars(
                        select(SimRefund).where(
                            SimRefund.session_id == self._session_id,
                            SimRefund.payment_id.in_(payment_ids),
                        )
                    ).all()
                )
            )

        observed_pay_statuses = {
            str(ic.causal_factors_json.get("observed_payment_status") or "UNKNOWN")
            for ic in inc_cases
        }
        has_mixed_or_non_success_payments = observed_pay_statuses != {PaymentStatus.SUCCESS.value}
        has_refund_case_type = any(c.type == "REFUND_REQUEST" for c in cases_db)
        has_missing_identifiers = any(
            not ic.causal_factors_json.get("order_id")
            or not ic.causal_factors_json.get("payment_id")
            for ic in inc_cases
        )
        is_large_batch = len(inc_cases) >= 5

        selected_roles: list[str] = []
        reasons: list[str] = []

        if payment_ids or order_ids:
            selected_roles.append(TaskforceRole.FINANCE.value)
            reasons.append("payment_ledger_verification_required")

        selected_roles.append(TaskforceRole.OPERATIONS.value)
        reasons.append("worker_batch_order_sync_inspection_required")

        needs_risk = bool(
            existing_refunds_count > 0
            or has_refund_case_type
            or has_mixed_or_non_success_payments
            or is_large_batch
        )
        if needs_risk and len(selected_roles) < 3:
            selected_roles.append(TaskforceRole.RISK.value)
            if existing_refunds_count > 0 or has_refund_case_type:
                reasons.append("refund_history_or_in_flight_refund_detected")
            elif has_mixed_or_non_success_payments:
                reasons.append("mixed_or_non_success_payment_status_anomaly")
            else:
                reasons.append("high_volume_batch_refund_collision_guard")
        elif has_missing_identifiers and len(selected_roles) < 3:
            selected_roles.append(TaskforceRole.SUPPORT.value)
            reasons.append("missing_transaction_identifiers_customer_outreach_needed")

        # Always guarantee 2-3 bounded roles
        if len(selected_roles) < 2:
            selected_roles.append(TaskforceRole.SUPPORT.value)
            reasons.append("bounded_minimum_two_roles_support_fallback")
        selected_roles = selected_roles[:3]

        rationale = {
            "selected_roles": selected_roles,
            "reasons": reasons,
            "existing_refunds_count": existing_refunds_count,
            "observed_payment_statuses": sorted(observed_pay_statuses),
            "has_missing_identifiers": has_missing_identifiers,
            "linked_cases_count": len(inc_cases),
        }
        return selected_roles, rationale

    @staticmethod
    def _select_sample_incident_cases(
        inc_cases: list[IncidentCase],
        payment_ids_with_refunds: set[str],
    ) -> list[IncidentCase]:
        """Pick 1-2 representative IncidentCases to inspect via read tools:
        always includes the first Case plus any Case exhibiting non-SUCCESS payment status or
        existing refund records so anomalies are never missed by sampling."""
        if not inc_cases:
            return []
        selected: list[IncidentCase] = [inc_cases[0]]
        selected_ids: set[str] = {inc_cases[0].case_id}

        for ic in inc_cases[1:]:
            pay_id = str(ic.causal_factors_json.get("payment_id") or "")
            pay_stat = str(ic.causal_factors_json.get("observed_payment_status") or "UNKNOWN")
            if pay_id in payment_ids_with_refunds or pay_stat != PaymentStatus.SUCCESS.value:
                selected.append(ic)
                selected_ids.add(ic.case_id)
                if len(selected) >= 3:
                    break

        return selected

    def assign_and_run_taskforce(
        self,
        incident: Incident,
        *,
        force_rerun: bool = False,
    ) -> list[AgentTask]:
        """Conditionally allocate and run 2-3 bounded role tasks on `incident` using strict
        3-phase short transactions (Prompt 08.3.1 Fix 3):
        - Phase A (Read Snapshot): Load Incident, IncidentCases, existing tasks; do NOT mutate
          or hold write locks; release read transaction before external LLM calls.
        - Phase B (LLM Reasoning & Scoped Read Tools): Invoke LLM outside DB write locks.
        - Phase C (Revalidate & Persist): Acquire short `FOR UPDATE NOWAIT` lock on Incident,
          verify optimistic `updated_at` concurrency token, supersede prior tasks if
          `force_rerun=True`, persist new AgentTasks + CaseEvents, and commit immediately.
        """
        incident_id = incident.id

        # --- PHASE A: READ SNAPSHOT (zero writes, zero locks held across LLM calls) ---
        inc_loaded = self._db.scalar(
            select(Incident).where(
                Incident.id == incident_id,
                Incident.session_id == self._session_id,
            )
        )
        if inc_loaded is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"Incident '{incident_id}' not found in current session",
                status_code=404,
            )

        existing_tasks = list(
            self._db.scalars(
                select(AgentTask)
                .where(
                    AgentTask.session_id == self._session_id,
                    AgentTask.incident_id == incident_id,
                )
                .order_by(AgentTask.created_at.asc())
            ).all()
        )
        if existing_tasks and not force_rerun:
            return existing_tasks

        snapshot_updated_at = inc_loaded.updated_at
        snapshot_title = inc_loaded.title
        snapshot_hypothesis = inc_loaded.hypothesis
        snapshot_shared_factors = dict(inc_loaded.shared_factors_json or {})

        inc_cases = list(
            self._db.scalars(
                select(IncidentCase)
                .where(
                    IncidentCase.incident_id == incident_id,
                    IncidentCase.session_id == self._session_id,
                )
                .order_by(IncidentCase.created_at.asc(), IncidentCase.case_id.asc())
            ).all()
        )

        all_case_ids = [ic.case_id for ic in inc_cases]
        selected_roles, role_rationale = self.select_roles_for_incident(inc_loaded, inc_cases)
        role_objectives_override: dict[str, str] = {}

        all_pay_ids = [
            str(ic.causal_factors_json.get("payment_id"))
            for ic in inc_cases
            if ic.causal_factors_json.get("payment_id")
        ]
        pay_ids_with_refunds: set[str] = set()
        if all_pay_ids:
            ref_rows = list(
                self._db.scalars(
                    select(SimRefund).where(
                        SimRefund.session_id == self._session_id,
                        SimRefund.payment_id.in_(all_pay_ids),
                    )
                ).all()
            )
            pay_ids_with_refunds = {r.payment_id for r in ref_rows if r.payment_id}

        sampled_ics = self._select_sample_incident_cases(inc_cases, pay_ids_with_refunds)
        sampled_ic_snapshots: list[IncidentCase] = [
            IncidentCase(
                id=ic.id,
                incident_id=ic.incident_id,
                case_id=ic.case_id,
                session_id=ic.session_id,
                link_reason=ic.link_reason,
                shared_evidence_refs=list(ic.shared_evidence_refs or []),
                causal_factors_json=dict(ic.causal_factors_json or {}),
                is_auto_resolved=False,
            )
            for ic in sampled_ics
        ]
        inc_snapshot = Incident(
            id=incident_id,
            session_id=self._session_id,
            title=snapshot_title,
            hypothesis=snapshot_hypothesis,
            status=inc_loaded.status,
            correlation_key=inc_loaded.correlation_key,
            correlation_trace=inc_loaded.correlation_trace,
            shared_factors_json=snapshot_shared_factors,
            evidence_refs=list(inc_loaded.evidence_refs or []),
            confidence=inc_loaded.confidence,
        )

        # Release read-only DB transaction BEFORE any external LLM call!
        self._release_read_transaction()

        # --- PHASE B: LLM REASONING & SCOPED READ-TOOL EXECUTION (zero write locks!) ---
        now = datetime.now(UTC)
        llm_calls_attempted = 0
        llm_calls_succeeded = 0
        llm_calls_failed = 0
        llm_prompt_tokens = 0
        llm_completion_tokens = 0
        llm_total_tokens = 0
        llm_fallback_reasons: list[str] = []
        valid_role_values = {r.value for r in TaskforceRole}

        if self._adapter.mode == ModelMode.LLM_API and llm_calls_attempted < self._max_llm_calls:
            llm_calls_attempted += 1
            plan_sys = (
                "You are the FinResolve Adaptive Taskforce Coordinator. "
                "Select 2 to 3 roles from [FINANCE, OPERATIONS, RISK, SUPPORT] based ONLY on "
                "observed incident evidence. Return strict JSON matching: "
                '{"selected_roles": [...], "role_objectives": {"ROLE": "..."}, '
                '"initial_hypothesis": "..."}. Never invent payment proofs or auto-resolve cases.'
            )
            plan_payload = sanitize_no_oracle_leak(
                {
                    "incident_id": incident_id,
                    "title": snapshot_title,
                    "hypothesis": snapshot_hypothesis,
                    "linked_cases_count": len(all_case_ids),
                    "shared_factors": snapshot_shared_factors,
                    "deterministic_rationale": role_rationale,
                }
            )
            try:
                plan_inv = self._adapter.generate_structured_json(
                    plan_sys, json.dumps(plan_payload, ensure_ascii=False, default=str)
                )
                if plan_inv is not None:
                    llm_prompt_tokens += plan_inv.prompt_tokens
                    llm_completion_tokens += plan_inv.completion_tokens
                    llm_total_tokens += plan_inv.total_tokens
                    parsed_plan = TaskforcePlanOutput.model_validate(plan_inv.data)
                    filtered_roles: list[str] = []
                    for r_name in parsed_plan.selected_roles:
                        rn = r_name.strip().upper()
                        if rn in valid_role_values and rn not in filtered_roles:
                            filtered_roles.append(rn)
                    if len(filtered_roles) >= 2:
                        selected_roles = filtered_roles[:3]
                        role_objectives_override = {
                            k.strip().upper(): v.strip()
                            for k, v in parsed_plan.role_objectives.items()
                            if k.strip().upper() in valid_role_values and v.strip()
                        }
                        role_rationale["llm_planned_roles"] = selected_roles
                        role_rationale["llm_initial_hypothesis"] = parsed_plan.initial_hypothesis
                        llm_calls_succeeded += 1
                    else:
                        llm_calls_failed += 1
                        llm_fallback_reasons.append("role_plan_fallback:insufficient_valid_roles")
                else:
                    llm_calls_failed += 1
                    llm_fallback_reasons.append("role_plan_fallback:empty_llm_response")
            except (JobCancelledError, JobSupersededError, BudgetExhaustedError):
                raise
            except Exception as exc:  # noqa: BLE001
                llm_calls_failed += 1
                llm_fallback_reasons.append(f"role_plan_fallback:{str(exc)[:180]}")

        read_registry = ReadToolRegistry(self._db, self._session_id)
        configured_mode = self._adapter.mode.value
        effective_runtime_mode = "DEMO_RULES"

        created_tasks: list[AgentTask] = []
        prior_handoffs_summary: list[dict[str, Any]] = []
        replans_used = 0
        role_queue = list(selected_roles)
        idx = 0

        while idx < len(role_queue):
            role_name = role_queue[idx]
            next_role = (
                role_queue[idx + 1]
                if idx + 1 < len(role_queue)
                else "HUMAN_OPERATOR_PER_CASE_APPROVAL"
            )
            if role_name == TaskforceRole.FINANCE.value:
                task = self._run_finance_task(
                    target_inc=inc_snapshot,
                    all_case_ids=all_case_ids,
                    sampled_ics=sampled_ic_snapshots,
                    next_role=next_role,
                    read_registry=read_registry,
                    effective_runtime_mode=effective_runtime_mode,
                    configured_mode=configured_mode,
                    now=now,
                )
            elif role_name == TaskforceRole.OPERATIONS.value:
                task = self._run_operations_task(
                    target_inc=inc_snapshot,
                    all_case_ids=all_case_ids,
                    sampled_ics=sampled_ic_snapshots,
                    next_role=next_role,
                    read_registry=read_registry,
                    effective_runtime_mode=effective_runtime_mode,
                    configured_mode=configured_mode,
                    now=now,
                )
            elif role_name == TaskforceRole.RISK.value:
                task = self._run_risk_task(
                    target_inc=inc_snapshot,
                    all_case_ids=all_case_ids,
                    sampled_ics=sampled_ic_snapshots,
                    next_role=next_role,
                    read_registry=read_registry,
                    effective_runtime_mode=effective_runtime_mode,
                    configured_mode=configured_mode,
                    now=now,
                )
            else:
                task = self._run_support_task(
                    target_inc=inc_snapshot,
                    all_case_ids=all_case_ids,
                    sampled_ics=sampled_ic_snapshots,
                    next_role=next_role,
                    read_registry=read_registry,
                    effective_runtime_mode=effective_runtime_mode,
                    configured_mode=configured_mode,
                    now=now,
                )

            # Release read-only DB transaction opened by ReadToolRegistry before calling LLM!
            self._release_read_transaction()

            if role_name in role_objectives_override:
                task.objective = role_objectives_override[role_name]

            within_budget = (
                self._adapter.mode == ModelMode.LLM_API
                and llm_calls_attempted < self._max_llm_calls
                and (self._token_budget <= 0 or llm_total_tokens < self._token_budget)
            )
            if within_budget:
                llm_calls_attempted += 1
                role_sys = (
                    f"You are the {role_name} Agent in FinResolve Taskforce. "
                    "Synthesize findings ONLY from the provided tool_observations and "
                    "prior_handoffs. Return strict JSON: "
                    '{"findings_vi": "...", "contradictions": [...], "unknowns": [...], '
                    '"evidence_refs": [...], "recommended_per_case_action": "...", '
                    '"request_replan": false, "suggested_next_role": null}. '
                    "Never mark child cases RESOLVED."
                )
                role_input = sanitize_no_oracle_leak(
                    {
                        "role": role_name,
                        "objective": task.objective,
                        "checked_case_ids": task.handoff_json.get("checked_case_ids", []),
                        "unchecked_case_ids": task.handoff_json.get("unchecked_case_ids", []),
                        "tool_observations": task.handoff_json.get("tool_observations", []),
                        "observed_evidence_refs": list(task.evidence_refs or []),
                        "prior_handoffs": prior_handoffs_summary,
                    }
                )
                try:
                    role_inv = self._adapter.generate_structured_json(
                        role_sys, json.dumps(role_input, ensure_ascii=False, default=str)
                    )
                    if role_inv is not None:
                        llm_prompt_tokens += role_inv.prompt_tokens
                        llm_completion_tokens += role_inv.completion_tokens
                        llm_total_tokens += role_inv.total_tokens
                        synth = RoleHandoffSynthesis.model_validate(role_inv.data)
                        observed_refs_set = set(task.evidence_refs or [])
                        grounded_refs = [r for r in synth.evidence_refs if r in observed_refs_set]
                        hj = dict(task.handoff_json or {})
                        hj["findings_vi"] = synth.findings_vi
                        hj["contradictions"] = synth.contradictions
                        hj["unknowns"] = synth.unknowns
                        hj["llm_grounded_evidence_refs"] = grounded_refs or list(observed_refs_set)
                        hj["recommended_per_case_action"] = synth.recommended_per_case_action
                        hj["runtime_mode"] = "LLM_API"
                        hj["reasoning_mode"] = "LLM_API_ADAPTIVE_TASKFORCE"
                        hj["llm_invoked"] = True
                        hj["llm_output_validated"] = True
                        hj["auto_resolved_children"] = False
                        hj["requires_per_case_approval_and_verification"] = True
                        task.handoff_json = sanitize_no_oracle_leak(hj)
                        llm_calls_succeeded += 1

                        if (
                            synth.request_replan
                            and replans_used < 1
                            and len(role_queue) < 3
                            and isinstance(synth.suggested_next_role, str)
                        ):
                            cand_role = synth.suggested_next_role.strip().upper()
                            if cand_role in valid_role_values and cand_role not in role_queue:
                                role_queue.append(cand_role)
                                replans_used += 1
                                hj_replan = dict(task.handoff_json)
                                hj_replan["handoff_to"] = cand_role
                                hj_replan["replan_triggered"] = True
                                task.handoff_json = hj_replan
                    else:
                        llm_calls_failed += 1
                        llm_fallback_reasons.append(f"{role_name}_fallback:empty_llm_response")
                except (JobCancelledError, JobSupersededError, BudgetExhaustedError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    llm_calls_failed += 1
                    llm_fallback_reasons.append(f"{role_name}_fallback:{str(exc)[:180]}")

            prior_handoffs_summary.append(
                {
                    "role": role_name,
                    "findings_vi": task.handoff_json.get("findings_vi"),
                    "contradictions": task.handoff_json.get("contradictions", []),
                    "unknowns": task.handoff_json.get("unknowns", []),
                    "checked_case_ids": task.handoff_json.get("checked_case_ids", []),
                }
            )
            created_tasks.append(task)
            idx += 1

        # --- PHASE C: REVALIDATE & PERSIST IN A SHORT ATOMIC WRITE TRANSACTION ---
        assert_execution_fencing_in_txn(self._db, session_id=self._session_id)
        try:
            fresh_inc = self._db.scalar(
                select(Incident)
                .where(
                    Incident.id == incident_id,
                    Incident.session_id == self._session_id,
                )
                .with_for_update(nowait=True)
            )
        except OperationalError as err:
            self._db.rollback()
            raise AppError(
                ErrorCode.CONFLICT,
                f"CONCURRENT_TASKFORCE_EXECUTION: Incident '{incident_id}' is currently "
                "being updated by another Taskforce run",
                status_code=409,
                details={"incident_id": incident_id},
            ) from err

        if fresh_inc is None:
            self._db.rollback()
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"Incident '{incident_id}' no longer exists in session",
                status_code=404,
            )

        if fresh_inc.updated_at != snapshot_updated_at:
            self._db.rollback()
            raise AppError(
                ErrorCode.CONFLICT,
                f"STALE_INCIDENT_STATE: Incident '{incident_id}' was updated concurrently "
                "while Taskforce was reasoning; stale results rejected.",
                status_code=409,
                details={"incident_id": incident_id},
            )

        current_tasks = list(
            self._db.scalars(
                select(AgentTask)
                .where(
                    AgentTask.session_id == self._session_id,
                    AgentTask.incident_id == incident_id,
                )
                .order_by(AgentTask.created_at.asc())
            ).all()
        )
        if current_tasks and not force_rerun:
            self._db.rollback()
            return current_tasks

        persist_now = datetime.now(UTC)
        if current_tasks and force_rerun:
            for old_t in current_tasks:
                old_hj = dict(old_t.handoff_json or {})
                if not old_hj.get("superseded"):
                    old_hj["superseded"] = True
                    old_hj["superseded_at"] = persist_now.isoformat()
                    old_t.handoff_json = old_hj
                    old_t.updated_at = persist_now

        for t in created_tasks:
            self._db.add(t)

        selected_roles = list(role_queue)
        llm_actually_invoked = llm_calls_attempted > 0
        llm_output_validated = llm_calls_succeeded > 0
        fallback_reason = "; ".join(llm_fallback_reasons) if llm_fallback_reasons else None
        sf = dict(fresh_inc.shared_factors_json or {})
        sf["role_selection"] = sanitize_no_oracle_leak(role_rationale)
        sf["taskforce_execution_mode"] = (
            "LLM_API_ADAPTIVE_TASKFORCE"
            if llm_output_validated
            else "DETERMINISTIC_TASKFORCE_RULES"
        )
        sf["llm_invoked"] = llm_actually_invoked
        sf["llm_output_validated"] = llm_output_validated
        sf["fallback_reason"] = fallback_reason
        sf["llm_usage"] = {
            "adapter": self._adapter.adapter_name,
            "llm_calls_attempted": llm_calls_attempted,
            "llm_calls_succeeded": llm_calls_succeeded,
            "llm_calls_failed": llm_calls_failed,
            "llm_invoked": llm_actually_invoked,
            "llm_output_validated": llm_output_validated,
            "prompt_tokens": llm_prompt_tokens,
            "completion_tokens": llm_completion_tokens,
            "total_tokens": llm_total_tokens,
            "replans_used": replans_used,
            "fallback_reason": fallback_reason,
            "fallback_reasons": llm_fallback_reasons,
        }
        fresh_inc.shared_factors_json = sf
        fresh_inc.status = IncidentStatus.TASKFORCE_ASSIGNED.value
        fresh_inc.updated_at = persist_now
        self._db.flush()

        actual_checked_union: list[str] = []
        for t in created_tasks:
            for cid in t.handoff_json.get("checked_case_ids") or []:
                if cid not in actual_checked_union:
                    actual_checked_union.append(str(cid))
        actual_unchecked_union = [
            cid for cid in all_case_ids if cid not in set(actual_checked_union)
        ]

        for cid in all_case_ids:
            was_checked = cid in set(actual_checked_union)
            self._db.add(
                CaseEvent(
                    id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                    case_id=cid,
                    session_id=self._session_id,
                    type="INCIDENT_TASKFORCE_HANDOFF",
                    payload_json={
                        "incident_id": fresh_inc.id,
                        "assigned_roles": selected_roles,
                        "task_ids": [t.id for t in created_tasks],
                        "checked_case_ids": actual_checked_union,
                        "unchecked_case_ids": actual_unchecked_union,
                        "case_was_directly_checked": was_checked,
                        "verification_status": (
                            "OBSERVED_SAMPLE_FACT_NOT_VERIFIED" if was_checked else "UNKNOWN"
                        ),
                        "runtime_mode": "LLM_API" if llm_output_validated else "DEMO_RULES",
                        "llm_invoked": llm_actually_invoked,
                        "llm_output_validated": llm_output_validated,
                        "auto_resolved": False,
                        "requires_independent_per_case_verification": True,
                    },
                    created_at=persist_now,
                )
            )

        if self._manage_transactions:
            self._db.commit()
        else:
            self._db.flush()

        if incident is not fresh_inc:
            incident.shared_factors_json = fresh_inc.shared_factors_json
            incident.status = fresh_inc.status
            incident.updated_at = fresh_inc.updated_at
        return created_tasks

    def _run_finance_task(
        self,
        *,
        target_inc: Incident,
        all_case_ids: list[str],
        sampled_ics: list[IncidentCase],
        next_role: str,
        read_registry: ReadToolRegistry,
        effective_runtime_mode: str,
        configured_mode: str,
        now: datetime,
    ) -> AgentTask:
        fin_spec = CapabilityRegistry.get_spec(TaskforceRole.FINANCE.value)
        fin_exec = ScopedRoleToolExecutor(
            role=TaskforceRole.FINANCE.value,
            tool_registry=read_registry,
        )
        fin_obs: list[ToolObservationSummary] = []
        per_case_status: dict[str, str] = {cid: "UNKNOWN" for cid in all_case_ids}
        checked_case_ids: list[str] = []

        verified_success_count = 0
        non_success_cases: list[str] = []
        unknown_authority_cases: list[str] = []

        for ic in sampled_ics:
            pay_id = str(ic.causal_factors_json.get("payment_id") or "")
            ord_id = str(ic.causal_factors_json.get("order_id") or "")
            if pay_id:
                pay_res = fin_exec.execute_read_tool("get_payment_record", {"payment_id": pay_id})
                checked_case_ids.append(ic.case_id)
                p_status: str
                if pay_res.status != "OK":
                    unknown_authority_cases.append(f"{ic.case_id}({pay_res.status})")
                    per_case_status[ic.case_id] = "UNKNOWN"
                    p_status = str(pay_res.status)
                    auth_ok = False
                    res_code = None
                else:
                    p_status = str(pay_res.data.get("status") or pay_res.status)
                    auth_ok = bool(pay_res.data.get("authoritative_success"))
                    res_code = pay_res.data.get("result_code")
                    if auth_ok and p_status == PaymentStatus.SUCCESS.value and res_code == 0:
                        verified_success_count += 1
                        per_case_status[ic.case_id] = "OBSERVED_PAYMENT_SUCCESS_UNVERIFIED_ORDER"
                    else:
                        non_success_cases.append(f"{ic.case_id}({p_status},code={res_code})")
                        per_case_status[ic.case_id] = f"OBSERVED_PAYMENT_{p_status}"

                fin_obs.append(
                    ToolObservationSummary(
                        case_id=ic.case_id,
                        tool_name="get_payment_record",
                        status=pay_res.status,
                        source=pay_res.source,
                        source_ref=pay_res.source_ref,
                        summary=sanitize_no_oracle_leak(
                            {
                                "case_id": ic.case_id,
                                "payment_id": pay_id,
                                "status": p_status,
                                "result_code": res_code,
                                "authoritative_success": auth_ok,
                            }
                        ),
                    )
                )
            elif ord_id:
                cand_res = fin_exec.execute_read_tool(
                    "find_payment_candidates", {"order_id": ord_id}
                )
                checked_case_ids.append(ic.case_id)
                c_count = int(cand_res.data.get("candidate_count") or 0)
                if cand_res.status == "OK" and c_count > 0:
                    per_case_status[ic.case_id] = "OBSERVED_CANDIDATES_FOUND"
                else:
                    unknown_authority_cases.append(f"{ic.case_id}({cand_res.status})")
                    per_case_status[ic.case_id] = "UNKNOWN"
                fin_obs.append(
                    ToolObservationSummary(
                        case_id=ic.case_id,
                        tool_name="find_payment_candidates",
                        status=cand_res.status,
                        source=cand_res.source,
                        source_ref=cand_res.source_ref,
                        summary=sanitize_no_oracle_leak(
                            {"case_id": ic.case_id, "candidate_count": c_count}
                        ),
                    )
                )
            else:
                # No payment_id or order_id available -> no read tool invoked; remains UNKNOWN
                per_case_status[ic.case_id] = "UNKNOWN"

        unchecked_case_ids = [cid for cid in all_case_ids if cid not in set(checked_case_ids)]
        total_cases = len(all_case_ids)

        if non_success_cases:
            findings_vi = (
                f"Vai trò FINANCE đã kiểm tra mẫu {len(checked_case_ids)}/{total_cases} Case "
                f"({checked_case_ids}): phát hiện trạng thái thanh toán không đồng nhất hoặc chưa "
                f"thành công tại {non_success_cases} (chỉ {verified_success_count}/"
                f"{len(checked_case_ids)} Case mẫu đạt SUCCESS). {len(unchecked_case_ids)} Case "
                "còn lại chưa kiểm tra trực tiếp được đánh dấu UNKNOWN. Không đồng bộ hàng loạt."
            )
            rec_action = "HOLD_OR_REVIEW_MIXED_PAYMENT_STATUS_PER_CASE"
        elif unknown_authority_cases or not checked_case_ids:
            findings_vi = (
                f"Vai trò FINANCE đã gọi công cụ cho {len(checked_case_ids)}/{total_cases} Case "
                f"({checked_case_ids}): không đủ thẩm quyền xác minh thanh toán tại "
                f"{unknown_authority_cases or unchecked_case_ids} (trạng thái UNKNOWN). "
                "Cần bổ sung mã giao dịch hoặc kiểm tra độc lập từng Case."
            )
            rec_action = "REQUEST_PAYMENT_EVIDENCE_OR_REVIEW_UNKNOWN_PER_CASE"
        else:
            findings_vi = (
                f"Vai trò FINANCE đã đối soát mẫu {len(checked_case_ids)}/{total_cases} Case "
                f"({checked_case_ids}): {verified_success_count}/{len(checked_case_ids)} Case mẫu "
                "có bản ghi thanh toán SUCCESS (result_code=0). Tuy nhiên "
                f"{len(unchecked_case_ids)} Case còn lại trong sự cố chưa được đọc sổ cái trực "
                "tiếp nên giữ trạng thái UNKNOWN; bắt buộc kiểm tra từng Case trước khi đề xuất."
            )
            rec_action = "VERIFY_PAYMENT_AND_ORDER_SYNC_ELIGIBILITY_PER_CASE"

        fin_handoff = TaskHandoffPayload(
            incident_id=target_inc.id,
            from_role=TaskforceRole.FINANCE.value,
            to_role=next_role,
            runtime_mode=effective_runtime_mode,
            configured_model_mode=configured_mode,
            taskforce_execution_mode="DETERMINISTIC_TASKFORCE_RULES",
            llm_invoked=False,
            checked_case_ids=checked_case_ids,
            unchecked_case_ids=unchecked_case_ids,
            per_case_verification_status=per_case_status,
            tool_observations=fin_obs,
            findings_vi=findings_vi,
            recommended_per_case_action=rec_action,
            auto_resolved_children=False,
            requires_per_case_approval_and_verification=True,
        )

        return AgentTask(
            id=f"TASK-FIN-{uuid.uuid4().hex[:8].upper()}",
            session_id=self._session_id,
            incident_id=target_inc.id,
            case_id=checked_case_ids[0] if checked_case_ids else None,
            role=TaskforceRole.FINANCE.value,
            objective=(
                "Đối soát sổ cái thanh toán trên tập Case mẫu (get_payment_record / "
                "find_payment_candidates) và ghi nhận các Case chưa kiểm tra là UNKNOWN."
            ),
            state=AgentTaskState.COMPLETED.value,
            allowed_tools=sorted(fin_spec.allowed_read_tools),
            evidence_refs=list(target_inc.evidence_refs[:4]),
            handoff_json=fin_handoff.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )

    def _run_operations_task(
        self,
        *,
        target_inc: Incident,
        all_case_ids: list[str],
        sampled_ics: list[IncidentCase],
        next_role: str,
        read_registry: ReadToolRegistry,
        effective_runtime_mode: str,
        configured_mode: str,
        now: datetime,
    ) -> AgentTask:
        ops_spec = CapabilityRegistry.get_spec(TaskforceRole.OPERATIONS.value)
        ops_exec = ScopedRoleToolExecutor(
            role=TaskforceRole.OPERATIONS.value,
            tool_registry=read_registry,
        )
        ops_obs: list[ToolObservationSummary] = []
        per_case_status: dict[str, str] = {cid: "UNKNOWN" for cid in all_case_ids}
        checked_case_ids: list[str] = []

        pending_unpaid_count = 0
        other_order_states: list[str] = []
        missing_order_cases: list[str] = []

        for ic in sampled_ics:
            ord_id = str(ic.causal_factors_json.get("order_id") or "")
            if not ord_id:
                per_case_status[ic.case_id] = "UNKNOWN"
                continue

            ord_res = ops_exec.execute_read_tool("get_order", {"order_id": ord_id})
            evt_res = ops_exec.execute_read_tool("get_order_events", {"order_id": ord_id})
            checked_case_ids.append(ic.case_id)

            o_status: str
            if ord_res.status != "OK":
                missing_order_cases.append(f"{ic.case_id}({ord_res.status})")
                per_case_status[ic.case_id] = "UNKNOWN"
                o_status = str(ord_res.status)
                o_pay_status = "UNKNOWN"
            else:
                o_status = str(ord_res.data.get("status") or ord_res.status)
                o_pay_status = str(ord_res.data.get("payment_status") or "UNKNOWN")
                if o_status == "PENDING" and o_pay_status == "UNPAID":
                    pending_unpaid_count += 1
                    per_case_status[ic.case_id] = "OBSERVED_ORDER_PENDING_UNPAID"
                else:
                    other_order_states.append(f"{ic.case_id}({o_status}/{o_pay_status})")
                    per_case_status[ic.case_id] = f"OBSERVED_ORDER_{o_status}_{o_pay_status}"

            ops_obs.extend(
                [
                    ToolObservationSummary(
                        case_id=ic.case_id,
                        tool_name="get_order",
                        status=ord_res.status,
                        source=ord_res.source,
                        source_ref=ord_res.source_ref,
                        summary=sanitize_no_oracle_leak(
                            {
                                "case_id": ic.case_id,
                                "order_id": ord_id,
                                "order_status": o_status,
                                "payment_status": o_pay_status,
                            }
                        ),
                    ),
                    ToolObservationSummary(
                        case_id=ic.case_id,
                        tool_name="get_order_events",
                        status=evt_res.status,
                        source=evt_res.source,
                        source_ref=evt_res.source_ref,
                        summary=sanitize_no_oracle_leak(
                            {
                                "case_id": ic.case_id,
                                "order_id": ord_id,
                                "worker_id": target_inc.shared_factors_json.get("worker_id"),
                                "batch_id": target_inc.shared_factors_json.get("batch_id"),
                                "error_code": target_inc.shared_factors_json.get("error_code"),
                            }
                        ),
                    ),
                ]
            )

        unchecked_case_ids = [cid for cid in all_case_ids if cid not in set(checked_case_ids)]
        total_cases = len(all_case_ids)

        if other_order_states:
            findings_vi = (
                f"Vai trò OPERATIONS đã kiểm tra mẫu {len(checked_case_ids)}/{total_cases} Case "
                f"({checked_case_ids}): phát hiện trạng thái đơn hàng khác biệt tại "
                f"{other_order_states} ({pending_unpaid_count} Case mẫu ở PENDING/UNPAID). "
                f"{len(unchecked_case_ids)} Case còn lại giữ trạng thái UNKNOWN."
            )
            rec_action = "INSPECT_INDIVIDUAL_ORDER_STATE_BEFORE_PROPOSAL"
        elif missing_order_cases or not checked_case_ids:
            findings_vi = (
                f"Vai trò OPERATIONS đã gọi công cụ cho {len(checked_case_ids)}/{total_cases} "
                f"Case ({checked_case_ids}): không tìm thấy đơn hàng hoặc thiếu order_id tại "
                f"{missing_order_cases or unchecked_case_ids} (trạng thái UNKNOWN)."
            )
            rec_action = "REQUEST_ORDER_IDENTIFIER_OR_INSPECT_PER_CASE"
        else:
            findings_vi = (
                f"Vai trò OPERATIONS đã kiểm tra mẫu {len(checked_case_ids)}/{total_cases} Case "
                f"({checked_case_ids}): {pending_unpaid_count}/{len(checked_case_ids)} đơn mẫu "
                "đang ở trạng thái PENDING/UNPAID khớp với sự kiện nghẽn worker "
                f"({target_inc.shared_factors_json.get('worker_id')}). {len(unchecked_case_ids)} "
                "Case còn lại chưa đọc trực tiếp được giữ trạng thái UNKNOWN."
            )
            rec_action = "PROPOSE_PER_CASE_ORDER_SYNC_UNDER_POLICY"

        ops_handoff = TaskHandoffPayload(
            incident_id=target_inc.id,
            from_role=TaskforceRole.OPERATIONS.value,
            to_role=next_role,
            runtime_mode=effective_runtime_mode,
            configured_model_mode=configured_mode,
            taskforce_execution_mode="DETERMINISTIC_TASKFORCE_RULES",
            llm_invoked=False,
            checked_case_ids=checked_case_ids,
            unchecked_case_ids=unchecked_case_ids,
            per_case_verification_status=per_case_status,
            tool_observations=ops_obs,
            findings_vi=findings_vi,
            recommended_per_case_action=rec_action,
            auto_resolved_children=False,
            requires_per_case_approval_and_verification=True,
        )

        return AgentTask(
            id=f"TASK-OPS-{uuid.uuid4().hex[:8].upper()}",
            session_id=self._session_id,
            incident_id=target_inc.id,
            case_id=checked_case_ids[0] if checked_case_ids else None,
            role=TaskforceRole.OPERATIONS.value,
            objective=(
                "Kiểm tra trạng thái đơn hàng (get_order) và sự kiện worker (get_order_events) "
                "trên tập Case mẫu; giữ trạng thái UNKNOWN cho các Case chưa đọc."
            ),
            state=AgentTaskState.COMPLETED.value,
            allowed_tools=sorted(ops_spec.allowed_read_tools),
            evidence_refs=list(target_inc.evidence_refs[:4]),
            handoff_json=ops_handoff.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )

    def _run_risk_task(
        self,
        *,
        target_inc: Incident,
        all_case_ids: list[str],
        sampled_ics: list[IncidentCase],
        next_role: str,
        read_registry: ReadToolRegistry,
        effective_runtime_mode: str,
        configured_mode: str,
        now: datetime,
    ) -> AgentTask:
        risk_spec = CapabilityRegistry.get_spec(TaskforceRole.RISK.value)
        risk_exec = ScopedRoleToolExecutor(
            role=TaskforceRole.RISK.value,
            tool_registry=read_registry,
        )
        risk_obs: list[ToolObservationSummary] = []
        per_case_status: dict[str, str] = {cid: "UNKNOWN" for cid in all_case_ids}
        checked_case_ids: list[str] = []

        any_in_flight_refund = False
        any_settled_refund = False
        flagged_refund_cases: list[str] = []
        insufficient_authority_cases: list[str] = []

        for ic in sampled_ics:
            pay_id = str(ic.causal_factors_json.get("payment_id") or "")
            ord_id = str(ic.causal_factors_json.get("order_id") or "")
            if not pay_id and not ord_id:
                per_case_status[ic.case_id] = "UNKNOWN"
                continue

            ref_res = risk_exec.execute_read_tool(
                "get_refund_history",
                {"payment_id": pay_id, "order_id": ord_id},
            )
            checked_case_ids.append(ic.case_id)
            refunds_list = list(ref_res.data.get("refunds") or [])
            payment_found = bool(ref_res.data.get("payment_found"))

            # Distinguish verified empty refund history from payment/order not found or unavailable
            if ref_res.status != "OK" or (not payment_found and not refunds_list):
                auth_status = (
                    "UNKNOWN_TOOL_UNAVAILABLE"
                    if ref_res.status in ("TIMEOUT", "ERROR")
                    else "UNKNOWN_PAYMENT_OR_ORDER_NOT_FOUND"
                )
                insufficient_authority_cases.append(f"{ic.case_id}({auth_status})")
                per_case_status[ic.case_id] = "UNKNOWN"
                risk_obs.append(
                    ToolObservationSummary(
                        case_id=ic.case_id,
                        tool_name="get_refund_history",
                        status=ref_res.status,
                        source=ref_res.source,
                        source_ref=ref_res.source_ref,
                        summary=sanitize_no_oracle_leak(
                            {
                                "case_id": ic.case_id,
                                "payment_id": pay_id or None,
                                "order_id": ord_id or None,
                                "payment_found": payment_found,
                                "refund_authority_status": auth_status,
                                "refund_count": len(refunds_list),
                                "has_in_flight_refund": None,
                                "has_settled_refund": None,
                            }
                        ),
                    )
                )
                continue

            settled_amt = sum(
                int(r.get("completed_amount") or r.get("amount") or 0)
                for r in refunds_list
                if str(r.get("status")) == "COMPLETED"
            )
            pending_or_unk_amt = sum(
                int(r.get("requested_amount") or r.get("amount") or 0)
                for r in refunds_list
                if str(r.get("status")) in ("PENDING", "UNKNOWN")
            )
            in_flight_statuses = sorted(
                {
                    str(r.get("status"))
                    for r in refunds_list
                    if str(r.get("status")) in ("PENDING", "UNKNOWN")
                }
            )
            has_in_flight = bool(pending_or_unk_amt > 0 or in_flight_statuses)
            has_settled = bool(settled_amt > 0)

            if has_in_flight:
                any_in_flight_refund = True
                flagged_refund_cases.append(
                    f"{ic.case_id}(IN_FLIGHT:{in_flight_statuses},{pending_or_unk_amt})"
                )
                per_case_status[ic.case_id] = "HOLD_IN_FLIGHT_REFUND_DETECTED"
                auth_status = "VERIFIED_IN_FLIGHT_REFUND_DETECTED"
            elif has_settled:
                any_settled_refund = True
                flagged_refund_cases.append(f"{ic.case_id}(SETTLED:{settled_amt})")
                per_case_status[ic.case_id] = "REVIEW_PRIOR_SETTLED_REFUND_DETECTED"
                auth_status = "VERIFIED_SETTLED_REFUND_DETECTED"
            else:
                per_case_status[ic.case_id] = "OBSERVED_NO_REFUND_ON_VERIFIED_PAYMENT"
                auth_status = "VERIFIED_EMPTY_REFUND_HISTORY"

            risk_obs.append(
                ToolObservationSummary(
                    case_id=ic.case_id,
                    tool_name="get_refund_history",
                    status=ref_res.status,
                    source=ref_res.source,
                    source_ref=ref_res.source_ref,
                    summary=sanitize_no_oracle_leak(
                        {
                            "case_id": ic.case_id,
                            "payment_id": pay_id,
                            "payment_found": payment_found,
                            "refund_authority_status": auth_status,
                            "refund_count": len(refunds_list),
                            "has_in_flight_refund": has_in_flight,
                            "has_settled_refund": has_settled,
                            "in_flight_pending_or_unknown_amount": pending_or_unk_amt,
                            "settled_refund_amount": settled_amt,
                            "in_flight_statuses": in_flight_statuses,
                        }
                    ),
                )
            )

        pol_res = risk_exec.execute_read_tool("get_policy", {"action_type": "ORDER_SYNC"})
        risk_obs.append(
            ToolObservationSummary(
                case_id=None,
                tool_name="get_policy",
                status=pol_res.status,
                source=pol_res.source,
                source_ref=pol_res.source_ref,
                summary=sanitize_no_oracle_leak(
                    {"policy_version": pol_res.data.get("policy_version")}
                ),
            )
        )

        unchecked_case_ids = [cid for cid in all_case_ids if cid not in set(checked_case_ids)]
        total_cases = len(all_case_ids)

        if any_in_flight_refund or any_settled_refund:
            findings_vi = (
                f"CẢNH BÁO RỦI RO: Vai trò RISK kiểm tra mẫu {len(checked_case_ids)}/{total_cases} "
                f"Case ({checked_case_ids}) phát hiện giao dịch hoàn tiền đang chờ xử lý "
                f"(PENDING/UNKNOWN) hoặc đã hoàn tại {flagged_refund_cases}! Bắt buộc tạm giữ "
                "(HOLD_ACTION / REVIEW) để đối soát lệnh hoàn tiền, tuyệt đối không tự động thực "
                f"thi. {len(unchecked_case_ids)} Case còn lại chưa kiểm tra giữ trạng thái UNKNOWN."
            )
            rec_action = "HOLD_ACTION_RECONCILE_IN_FLIGHT_OR_SETTLED_REFUND_PER_CASE"
        elif insufficient_authority_cases or not checked_case_ids:
            findings_vi = (
                f"Vai trò RISK kiểm tra {len(checked_case_ids)}/{total_cases} Case "
                f"({checked_case_ids}): không đủ dữ liệu thẩm quyền để kết luận không có hoàn "
                f"tiền tại {insufficient_authority_cases or unchecked_case_ids} (thiếu bản ghi "
                "thanh toán/đơn hàng hoặc công cụ không sẵn sàng -> trạng thái UNKNOWN)."
            )
            rec_action = "HOLD_OR_REVIEW_UNKNOWN_REFUND_AUTHORITY_PER_CASE"
        else:
            findings_vi = (
                f"Vai trò RISK đã kiểm tra lịch sử hoàn tiền (get_refund_history) trên mẫu "
                f"{len(checked_case_ids)}/{total_cases} Case ({checked_case_ids}): chưa ghi nhận "
                f"lệnh hoàn tiền trên {len(checked_case_ids)} Case mẫu có thanh toán xác thực. "
                f"{len(unchecked_case_ids)} Case còn lại chưa được kiểm tra trực tiếp nên có trạng "
                "thái UNKNOWN; từng Case bắt buộc qua PolicyEngine, Approval và Verifier độc lập."
            )
            rec_action = "PER_CASE_POLICY_APPROVAL_EXECUTION_AND_VERIFICATION"

        risk_handoff = TaskHandoffPayload(
            incident_id=target_inc.id,
            from_role=TaskforceRole.RISK.value,
            to_role=next_role,
            runtime_mode=effective_runtime_mode,
            configured_model_mode=configured_mode,
            taskforce_execution_mode="DETERMINISTIC_TASKFORCE_RULES",
            llm_invoked=False,
            checked_case_ids=checked_case_ids,
            unchecked_case_ids=unchecked_case_ids,
            per_case_verification_status=per_case_status,
            tool_observations=risk_obs,
            findings_vi=findings_vi,
            recommended_per_case_action=rec_action,
            auto_resolved_children=False,
            requires_per_case_approval_and_verification=True,
        )

        return AgentTask(
            id=f"TASK-RSK-{uuid.uuid4().hex[:8].upper()}",
            session_id=self._session_id,
            incident_id=target_inc.id,
            case_id=checked_case_ids[0] if checked_case_ids else None,
            role=TaskforceRole.RISK.value,
            objective=(
                "Kiểm tra thực tế lịch sử hoàn tiền (get_refund_history) và chính sách "
                "(get_policy) trên tập Case mẫu; phân biệt không có hoàn tiền với UNKNOWN."
            ),
            state=AgentTaskState.COMPLETED.value,
            allowed_tools=sorted(risk_spec.allowed_read_tools),
            evidence_refs=list(target_inc.evidence_refs[:4]),
            handoff_json=risk_handoff.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )

    def _run_support_task(
        self,
        *,
        target_inc: Incident,
        all_case_ids: list[str],
        sampled_ics: list[IncidentCase],
        next_role: str,
        read_registry: ReadToolRegistry,
        effective_runtime_mode: str,
        configured_mode: str,
        now: datetime,
    ) -> AgentTask:
        sup_spec = CapabilityRegistry.get_spec(TaskforceRole.SUPPORT.value)
        sup_exec = ScopedRoleToolExecutor(
            role=TaskforceRole.SUPPORT.value,
            tool_registry=read_registry,
        )
        sup_obs: list[ToolObservationSummary] = []
        per_case_status: dict[str, str] = {cid: "UNKNOWN" for cid in all_case_ids}
        checked_case_ids: list[str] = []

        for ic in sampled_ics:
            ord_id = str(ic.causal_factors_json.get("order_id") or "")
            if ord_id:
                ord_res = sup_exec.execute_read_tool("get_order", {"order_id": ord_id})
                checked_case_ids.append(ic.case_id)
                per_case_status[ic.case_id] = (
                    f"OBSERVED_ORDER_{ord_res.status}" if ord_res.status == "OK" else "UNKNOWN"
                )
                sup_obs.append(
                    ToolObservationSummary(
                        case_id=ic.case_id,
                        tool_name="get_order",
                        status=ord_res.status,
                        source=ord_res.source,
                        source_ref=ord_res.source_ref,
                        summary=sanitize_no_oracle_leak(
                            {"case_id": ic.case_id, "order_id": ord_id, "status": ord_res.status}
                        ),
                    )
                )
            else:
                per_case_status[ic.case_id] = "UNKNOWN"

        pol_res = sup_exec.execute_read_tool("get_policy", {"action_type": "ORDER_SYNC"})
        sup_obs.append(
            ToolObservationSummary(
                case_id=None,
                tool_name="get_policy",
                status=pol_res.status,
                source=pol_res.source,
                source_ref=pol_res.source_ref,
                summary=sanitize_no_oracle_leak(
                    {"policy_version": pol_res.data.get("policy_version")}
                ),
            )
        )

        unchecked_case_ids = [cid for cid in all_case_ids if cid not in set(checked_case_ids)]

        sup_handoff = TaskHandoffPayload(
            incident_id=target_inc.id,
            from_role=TaskforceRole.SUPPORT.value,
            to_role=next_role,
            runtime_mode=effective_runtime_mode,
            configured_model_mode=configured_mode,
            taskforce_execution_mode="DETERMINISTIC_TASKFORCE_RULES",
            llm_invoked=False,
            checked_case_ids=checked_case_ids,
            unchecked_case_ids=unchecked_case_ids,
            per_case_verification_status=per_case_status,
            tool_observations=sup_obs,
            findings_vi=(
                f"Vai trò SUPPORT đã gọi công cụ tra cứu đơn hàng cho {len(checked_case_ids)} "
                f"Case ({checked_case_ids}) và kiểm tra chính sách; {len(unchecked_case_ids)} "
                "Case còn lại giữ trạng thái UNKNOWN, cần bổ sung mã định danh."
            ),
            recommended_per_case_action="REQUEST_MISSING_IDENTIFIER_OR_PER_CASE_REVIEW",
            auto_resolved_children=False,
            requires_per_case_approval_and_verification=True,
        )

        return AgentTask(
            id=f"TASK-SUP-{uuid.uuid4().hex[:8].upper()}",
            session_id=self._session_id,
            incident_id=target_inc.id,
            case_id=checked_case_ids[0] if checked_case_ids else None,
            role=TaskforceRole.SUPPORT.value,
            objective="Tra cứu thông tin đơn hàng và chính sách hỗ trợ khách hàng trên Case mẫu.",
            state=AgentTaskState.COMPLETED.value,
            allowed_tools=sorted(sup_spec.allowed_read_tools),
            evidence_refs=list(target_inc.evidence_refs[:4]),
            handoff_json=sup_handoff.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )
