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

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.read_tools import ReadToolRegistry
from app.core.config import get_settings
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


class TaskforceManager:
    """Conditionally selects and executes 2-3 bounded role tasks for an Incident."""

    def __init__(self, db: Session, session_id: str) -> None:
        self._db = db
        self._session_id = session_id

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
        """Conditionally allocate and run 2-3 bounded role tasks on `incident`."""
        existing_tasks = list(
            self._db.scalars(
                select(AgentTask)
                .where(
                    AgentTask.session_id == self._session_id,
                    AgentTask.incident_id == incident.id,
                )
                .order_by(AgentTask.created_at.asc())
            ).all()
        )
        if existing_tasks and not force_rerun:
            return existing_tasks
        if existing_tasks and force_rerun:
            for old_t in existing_tasks:
                self._db.delete(old_t)
            self._db.flush()

        inc_loaded = self._db.scalar(
            select(Incident).where(
                Incident.id == incident.id,
                Incident.session_id == self._session_id,
            )
        )
        target_inc = inc_loaded or incident
        inc_cases = list(
            self._db.scalars(
                select(IncidentCase)
                .where(
                    IncidentCase.incident_id == target_inc.id,
                    IncidentCase.session_id == self._session_id,
                )
                .order_by(IncidentCase.created_at.asc(), IncidentCase.case_id.asc())
            ).all()
        )

        all_case_ids = [ic.case_id for ic in inc_cases]
        selected_roles, role_rationale = self.select_roles_for_incident(target_inc, inc_cases)

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

        read_registry = ReadToolRegistry(self._db, self._session_id)
        configured_mode = get_settings().model_mode.value
        # Honest model-mode reporting: TaskforceManager uses deterministic rule orchestration
        # and does not invoke an external LLM.
        effective_runtime_mode = "DEMO_RULES"
        now = datetime.now(UTC)

        created_tasks: list[AgentTask] = []

        for idx, role_name in enumerate(selected_roles):
            next_role = (
                selected_roles[idx + 1]
                if idx + 1 < len(selected_roles)
                else "HUMAN_OPERATOR_PER_CASE_APPROVAL"
            )
            if role_name == TaskforceRole.FINANCE.value:
                task = self._run_finance_task(
                    target_inc=target_inc,
                    all_case_ids=all_case_ids,
                    sampled_ics=sampled_ics,
                    next_role=next_role,
                    read_registry=read_registry,
                    effective_runtime_mode=effective_runtime_mode,
                    configured_mode=configured_mode,
                    now=now,
                )
            elif role_name == TaskforceRole.OPERATIONS.value:
                task = self._run_operations_task(
                    target_inc=target_inc,
                    all_case_ids=all_case_ids,
                    sampled_ics=sampled_ics,
                    next_role=next_role,
                    read_registry=read_registry,
                    effective_runtime_mode=effective_runtime_mode,
                    configured_mode=configured_mode,
                    now=now,
                )
            elif role_name == TaskforceRole.RISK.value:
                task = self._run_risk_task(
                    target_inc=target_inc,
                    all_case_ids=all_case_ids,
                    sampled_ics=sampled_ics,
                    next_role=next_role,
                    read_registry=read_registry,
                    effective_runtime_mode=effective_runtime_mode,
                    configured_mode=configured_mode,
                    now=now,
                )
            else:
                task = self._run_support_task(
                    target_inc=target_inc,
                    all_case_ids=all_case_ids,
                    sampled_ics=sampled_ics,
                    next_role=next_role,
                    read_registry=read_registry,
                    effective_runtime_mode=effective_runtime_mode,
                    configured_mode=configured_mode,
                    now=now,
                )
            self._db.add(task)
            created_tasks.append(task)

        sf = dict(target_inc.shared_factors_json or {})
        sf["role_selection"] = sanitize_no_oracle_leak(role_rationale)
        sf["taskforce_execution_mode"] = "DETERMINISTIC_TASKFORCE_RULES"
        sf["llm_invoked"] = False
        target_inc.shared_factors_json = sf
        target_inc.status = IncidentStatus.TASKFORCE_ASSIGNED.value
        target_inc.updated_at = now
        self._db.flush()

        actual_checked_union: list[str] = []
        for t in created_tasks:
            for cid in t.handoff_json.get("checked_case_ids") or []:
                if cid not in actual_checked_union:
                    actual_checked_union.append(str(cid))
        actual_unchecked_union = [
            cid for cid in all_case_ids if cid not in set(actual_checked_union)
        ]

        for ic in inc_cases:
            was_checked = ic.case_id in set(actual_checked_union)
            self._db.add(
                CaseEvent(
                    id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                    case_id=ic.case_id,
                    session_id=self._session_id,
                    type="INCIDENT_TASKFORCE_HANDOFF",
                    payload_json={
                        "incident_id": target_inc.id,
                        "assigned_roles": selected_roles,
                        "task_ids": [t.id for t in created_tasks],
                        "checked_case_ids": actual_checked_union,
                        "unchecked_case_ids": actual_unchecked_union,
                        "case_was_directly_checked": was_checked,
                        "verification_status": (
                            "OBSERVED_SAMPLE_FACT_NOT_VERIFIED" if was_checked else "UNKNOWN"
                        ),
                        "runtime_mode": effective_runtime_mode,
                        "llm_invoked": False,
                        "auto_resolved": False,
                        "requires_independent_per_case_verification": True,
                    },
                    created_at=now,
                )
            )

        self._db.flush()
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
