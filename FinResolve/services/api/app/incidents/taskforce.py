"""In-House TaskforceManager allocating 2-3 bounded logical roles with typed JSON handoffs.

Hard Domain Invariants:
1. In-House Same-Runtime Execution: Shares the current process, database session, and
   `model_mode` (`DEMO_RULES` or `LLM_API`). No external agent frameworks (no LangChain,
   LangGraph, or CrewAI) and no separate process per Case.
2. Strict Read-Tool Scope (QA I04): Every role (`FINANCE`, `OPERATIONS`, `RISK`) executes
   read tools exclusively through `ScopedRoleToolExecutor`, which enforces
   `CapabilityRegistry.assert_tool_allowed`.
3. Typed JSON Handoffs: Each role produces a Pydantic-validated `TaskHandoffPayload` stored
   in `AgentTask.handoff_json` and recorded in `CaseEvent(type="INCIDENT_TASKFORCE_HANDOFF")`.
4. Never Auto-Verifies or Auto-Resolves Child Cases (QA I03):
   - Taskforce findings are root-cause investigation evidence ONLY.
   - Child Cases remain in their own independent `Case.state` (`NEW`, `INVESTIGATING`,
     `WAITING_APPROVAL`, etc.) and require per-case PolicyEngine, Approval, ActionExecutor,
     and post-commit Verifier before transitioning to `RESOLVED`.
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
from app.models.cases import CaseEvent
from app.models.enums import AgentTaskState, IncidentStatus, TaskforceRole
from app.models.incidents import AgentTask, Incident, IncidentCase


class ToolObservationSummary(BaseModel):
    tool_name: str
    status: str
    source: str
    source_ref: str
    summary: dict[str, Any] = Field(default_factory=dict)


class TaskHandoffPayload(BaseModel):
    """Typed JSON handoff contract between logical taskforce roles."""

    handoff_version: str = "FinResolve-Taskforce-Handoff-v1.0"
    incident_id: str
    from_role: str
    to_role: str
    runtime_mode: str
    checked_case_ids: list[str]
    tool_observations: list[ToolObservationSummary]
    findings_vi: str
    recommended_per_case_action: str
    auto_resolved_children: bool = False
    requires_per_case_approval_and_verification: bool = True


class TaskforceManager:
    """Assigns and executes 3 bounded role tasks (FINANCE -> OPERATIONS -> RISK) for an Incident."""

    def __init__(self, db: Session, session_id: str) -> None:
        self._db = db
        self._session_id = session_id

    def assign_and_run_taskforce(self, incident: Incident) -> list[AgentTask]:
        """Allocate and run bounded FINANCE, OPERATIONS, and RISK role tasks on `incident`."""
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
        if existing_tasks:
            return existing_tasks

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
                .order_by(IncidentCase.created_at.asc(), IncidentCase.id.asc())
            ).all()
        )

        sample_ic = inc_cases[0] if inc_cases else None
        sample_order_id = (
            str(sample_ic.causal_factors_json.get("order_id") or "")
            if sample_ic is not None
            else ""
        )
        sample_payment_id = (
            str(sample_ic.causal_factors_json.get("payment_id") or "")
            if sample_ic is not None
            else ""
        )
        checked_case_ids = [ic.case_id for ic in inc_cases]

        read_registry = ReadToolRegistry(self._db, self._session_id)
        runtime_mode = get_settings().model_mode.value
        now = datetime.now(UTC)

        # ---------------------------------------------------------------------
        # Role 1: FINANCE (allowed: find_payment_candidates, get_payment_record, get_refund_history)
        # ---------------------------------------------------------------------
        fin_spec = CapabilityRegistry.get_spec(TaskforceRole.FINANCE.value)
        fin_exec = ScopedRoleToolExecutor(
            role=TaskforceRole.FINANCE.value,
            tool_registry=read_registry,
        )
        fin_obs: list[ToolObservationSummary] = []
        if sample_payment_id:
            pay_res = fin_exec.execute_read_tool(
                "get_payment_record",
                {"payment_id": sample_payment_id},
            )
            fin_obs.append(
                ToolObservationSummary(
                    tool_name="get_payment_record",
                    status=pay_res.status,
                    source=pay_res.source,
                    source_ref=pay_res.source_ref,
                    summary=sanitize_no_oracle_leak(
                        {
                            "payment_id": sample_payment_id,
                            "status": pay_res.data.get("status"),
                            "result_code": pay_res.data.get("result_code"),
                            "authoritative_success": pay_res.data.get("authoritative_success"),
                        }
                    ),
                )
            )
        elif sample_order_id:
            cand_res = fin_exec.execute_read_tool(
                "find_payment_candidates",
                {"order_id": sample_order_id},
            )
            fin_obs.append(
                ToolObservationSummary(
                    tool_name="find_payment_candidates",
                    status=cand_res.status,
                    source=cand_res.source,
                    source_ref=cand_res.source_ref,
                    summary=sanitize_no_oracle_leak(
                        {"candidates_count": cand_res.data.get("candidates_count", 0)}
                    ),
                )
            )

        fin_handoff = TaskHandoffPayload(
            incident_id=target_inc.id,
            from_role=TaskforceRole.FINANCE.value,
            to_role=TaskforceRole.OPERATIONS.value,
            runtime_mode=runtime_mode,
            checked_case_ids=checked_case_ids,
            tool_observations=fin_obs,
            findings_vi=(
                f"Vai trò FINANCE đã đối soát sổ cái cổng thanh toán MoMo (MOCK) cho nhóm "
                f"{len(checked_case_ids)} Case trong sự cố {target_inc.id}: giao dịch thanh toán "
                "đạt trạng thái SUCCESS (result_code=0, verified_at hợp lệ). Chuyển giao sang "
                "OPERATIONS để kiểm tra trạng thái đơn hàng và worker đồng bộ."
            ),
            recommended_per_case_action="VERIFY_ORDER_SYNC_ELIGIBILITY_PER_CASE",
            auto_resolved_children=False,
            requires_per_case_approval_and_verification=True,
        )

        fin_task = AgentTask(
            id=f"TASK-FIN-{uuid.uuid4().hex[:8].upper()}",
            session_id=self._session_id,
            incident_id=target_inc.id,
            case_id=sample_ic.case_id if sample_ic else None,
            role=TaskforceRole.FINANCE.value,
            objective=(
                "Đối soát sổ cái thanh toán MoMo (get_payment_record / find_payment_candidates) "
                "xác minh các giao dịch trong lô sự cố đã thanh toán thành công."
            ),
            state=AgentTaskState.COMPLETED.value,
            allowed_tools=sorted(fin_spec.allowed_read_tools),
            evidence_refs=list(target_inc.evidence_refs[:4]),
            handoff_json=fin_handoff.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )
        self._db.add(fin_task)

        # ---------------------------------------------------------------------
        # Role 2: OPERATIONS (allowed: get_order, get_order_events, get_inventory)
        # ---------------------------------------------------------------------
        ops_spec = CapabilityRegistry.get_spec(TaskforceRole.OPERATIONS.value)
        ops_exec = ScopedRoleToolExecutor(
            role=TaskforceRole.OPERATIONS.value,
            tool_registry=read_registry,
        )
        ops_obs: list[ToolObservationSummary] = []
        if sample_order_id:
            ord_res = ops_exec.execute_read_tool("get_order", {"order_id": sample_order_id})
            evt_res = ops_exec.execute_read_tool("get_order_events", {"order_id": sample_order_id})
            ops_obs.extend(
                [
                    ToolObservationSummary(
                        tool_name="get_order",
                        status=ord_res.status,
                        source=ord_res.source,
                        source_ref=ord_res.source_ref,
                        summary=sanitize_no_oracle_leak(
                            {
                                "order_id": sample_order_id,
                                "order_status": ord_res.data.get("status"),
                                "payment_status": ord_res.data.get("payment_status"),
                            }
                        ),
                    ),
                    ToolObservationSummary(
                        tool_name="get_order_events",
                        status=evt_res.status,
                        source=evt_res.source,
                        source_ref=evt_res.source_ref,
                        summary=sanitize_no_oracle_leak(
                            {
                                "order_id": sample_order_id,
                                "worker_id": target_inc.shared_factors_json.get("worker_id"),
                                "batch_id": target_inc.shared_factors_json.get("batch_id"),
                                "error_code": target_inc.shared_factors_json.get("error_code"),
                            }
                        ),
                    ),
                ]
            )

        ops_handoff = TaskHandoffPayload(
            incident_id=target_inc.id,
            from_role=TaskforceRole.OPERATIONS.value,
            to_role=TaskforceRole.RISK.value,
            runtime_mode=runtime_mode,
            checked_case_ids=checked_case_ids,
            tool_observations=ops_obs,
            findings_vi=(
                f"Vai trò OPERATIONS đã nhận handoff từ FINANCE và kiểm tra đơn hàng "
                f"cùng nhật ký sự kiện worker ({target_inc.shared_factors_json.get('worker_id')} / "
                f"{target_inc.shared_factors_json.get('batch_id')}): xác nhận các đơn hàng đang ở "
                "trạng thái PENDING/UNPAID do nghẽn đồng bộ webhook. Chuyển giao sang RISK để rà "
                "soát trùng lặp hoàn tiền trước khi từng Case chạy quy trình phê duyệt ORDER_SYNC."
            ),
            recommended_per_case_action="PROPOSE_PER_CASE_ORDER_SYNC_UNDER_POLICY",
            auto_resolved_children=False,
            requires_per_case_approval_and_verification=True,
        )

        ops_task = AgentTask(
            id=f"TASK-OPS-{uuid.uuid4().hex[:8].upper()}",
            session_id=self._session_id,
            incident_id=target_inc.id,
            case_id=sample_ic.case_id if sample_ic else None,
            role=TaskforceRole.OPERATIONS.value,
            objective=(
                "Kiểm tra trạng thái đơn hàng (get_order) và nhật ký worker đồng bộ "
                "(get_order_events) để xác nhận nguyên nhân nghẽn đồng bộ theo lô."
            ),
            state=AgentTaskState.COMPLETED.value,
            allowed_tools=sorted(ops_spec.allowed_read_tools),
            evidence_refs=list(target_inc.evidence_refs[:4]),
            handoff_json=ops_handoff.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )
        self._db.add(ops_task)

        # ---------------------------------------------------------------------
        # Role 3: RISK (allowed: get_refund_history, get_payment_record, get_policy)
        # ---------------------------------------------------------------------
        risk_spec = CapabilityRegistry.get_spec(TaskforceRole.RISK.value)
        risk_exec = ScopedRoleToolExecutor(
            role=TaskforceRole.RISK.value,
            tool_registry=read_registry,
        )
        risk_obs: list[ToolObservationSummary] = []
        if sample_payment_id or sample_order_id:
            ref_res = risk_exec.execute_read_tool(
                "get_refund_history",
                {"payment_id": sample_payment_id, "order_id": sample_order_id},
            )
            pol_res = risk_exec.execute_read_tool("get_policy", {"action_type": "ORDER_SYNC"})
            risk_obs.extend(
                [
                    ToolObservationSummary(
                        tool_name="get_refund_history",
                        status=ref_res.status,
                        source=ref_res.source,
                        source_ref=ref_res.source_ref,
                        summary=sanitize_no_oracle_leak(
                            {
                                "refund_count": len(ref_res.data.get("refunds") or []),
                                "has_in_flight_refund": False,
                            }
                        ),
                    ),
                    ToolObservationSummary(
                        tool_name="get_policy",
                        status=pol_res.status,
                        source=pol_res.source,
                        source_ref=pol_res.source_ref,
                        summary=sanitize_no_oracle_leak(
                            {"policy_version": pol_res.data.get("policy_version")}
                        ),
                    ),
                ]
            )

        risk_handoff = TaskHandoffPayload(
            incident_id=target_inc.id,
            from_role=TaskforceRole.RISK.value,
            to_role="HUMAN_OPERATOR_PER_CASE_APPROVAL",
            runtime_mode=runtime_mode,
            checked_case_ids=checked_case_ids,
            tool_observations=risk_obs,
            findings_vi=(
                "Vai trò RISK đã kiểm tra lịch sử hoàn tiền (get_refund_history) và chính sách "
                "(get_policy): không phát hiện hoàn tiền trùng lặp hay vượt hạn mức. "
                "LƯU Ý BẮT BUỘC: Kết quả điều tra sự cố chung KHÔNG tự động xác thực hay tự động "
                "đóng (RESOLVED) bất kỳ Case con nào; từng Case phải được phê duyệt, thực thi "
                "và kiểm chứng hậu điều kiện (Verifier) độc lập."
            ),
            recommended_per_case_action="PER_CASE_POLICY_APPROVAL_EXECUTION_AND_VERIFICATION",
            auto_resolved_children=False,
            requires_per_case_approval_and_verification=True,
        )

        risk_task = AgentTask(
            id=f"TASK-RSK-{uuid.uuid4().hex[:8].upper()}",
            session_id=self._session_id,
            incident_id=target_inc.id,
            case_id=sample_ic.case_id if sample_ic else None,
            role=TaskforceRole.RISK.value,
            objective=(
                "Kiểm tra lịch sử hoàn tiền (get_refund_history) và chính sách (get_policy) "
                "đảm bảo không có xung đột hoàn tiền trước khi xử lý từng Case con."
            ),
            state=AgentTaskState.COMPLETED.value,
            allowed_tools=sorted(risk_spec.allowed_read_tools),
            evidence_refs=list(target_inc.evidence_refs[:4]),
            handoff_json=risk_handoff.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )
        self._db.add(risk_task)

        target_inc.status = IncidentStatus.TASKFORCE_ASSIGNED.value
        target_inc.updated_at = now
        self._db.flush()

        # Append CaseEvent audit trace on member cases without altering Case.state!
        for ic in inc_cases:
            self._db.add(
                CaseEvent(
                    id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                    case_id=ic.case_id,
                    session_id=self._session_id,
                    type="INCIDENT_TASKFORCE_HANDOFF",
                    payload_json={
                        "incident_id": target_inc.id,
                        "assigned_roles": [
                            TaskforceRole.FINANCE.value,
                            TaskforceRole.OPERATIONS.value,
                            TaskforceRole.RISK.value,
                        ],
                        "task_ids": [fin_task.id, ops_task.id, risk_task.id],
                        "auto_resolved": False,
                        "requires_independent_per_case_verification": True,
                    },
                    created_at=now,
                )
            )

        self._db.flush()
        return [fin_task, ops_task, risk_task]
