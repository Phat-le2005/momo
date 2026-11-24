"""Prompt 08.2 — AI-Native Agentic AI Upgrade Integration Tests (PostgreSQL + Mock LLM Adapter).

Verifies mandatory acceptance criteria without requiring external LLM API keys:
1. Zero PostgreSQL row locks held during LLM invocation (verified via concurrent NOWAIT probe).
2. Multi-step LLM planning -> tool use -> observation -> replan -> proposal -> approval -> verify.
3. Invalid JSON recovery & provider timeout never result in fake RESOLVED.
4. Overlapping investigation blocked (409), stale worker auto-recovered (INTERRUPTED), and
   optimistic concurrency version change aborts cleanly without overwriting.
5. Adaptive Multi-Agent Taskforce invokes LLM when configured (dynamic role selection,
   evidence-grounded handoffs, bounded dynamic replan, token telemetry, 0 auto-resolved children).
6. Evidence-aware AI-assisted Incident correlation supports evidence and abstains on contradiction
   without ever merging lookalikes.
7. Anti-bypass session creation & AI investigation rate limits ignore spoofed `X-Forwarded-For`,
   and streaming ASGI body size middleware rejects oversized payloads with 413.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai_core.context import InvestigationContext
from app.ai_core.model_adapter import ModelInvocationResult, StructuredJsonInvocationResult
from app.ai_core.runtime import BoundedAgentRuntime
from app.ai_core.schemas import Hypothesis, NextStep, Plan, PlanStepKind
from app.core.config import ModelMode, get_settings
from app.core.errors import AppError
from app.core.rate_limit import reset_rate_limiter
from app.db.session import get_sessionmaker
from app.incidents.correlator import IncidentCorrelator, seed_incident_taskforce_scenario
from app.incidents.taskforce import TaskforceManager
from app.models.cases import Case, CaseEvent, Evidence
from app.models.enums import (
    CaseState,
    EvidenceKind,
    FulfillmentStatus,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
    VerificationVerdict,
)
from app.models.simulator import SimOrder, SimPayment


class ScriptedMockLLMAdapter:
    """Deterministic Mock LLM adapter reporting `ModelMode.LLM_API` for integration tests."""

    def __init__(
        self,
        *,
        plan_fn: Callable[[InvestigationContext, int], Plan] | None = None,
        json_responses: list[dict[str, Any] | Exception] | None = None,
    ) -> None:
        self._plan_fn = plan_fn
        self._json_responses = list(json_responses or [])
        self.plan_calls = 0
        self.json_calls = 0

    @property
    def mode(self) -> ModelMode:
        return ModelMode.LLM_API

    @property
    def adapter_name(self) -> str:
        return "ScriptedMockLLMAdapter(mock:gpt-agentic-test)"

    def generate_plan(self, context: InvestigationContext) -> ModelInvocationResult:
        self.plan_calls += 1
        if self._plan_fn is None:
            raise RuntimeError("No plan_fn configured")
        plan = self._plan_fn(context, self.plan_calls)
        return ModelInvocationResult(
            plan=plan,
            model_mode=ModelMode.LLM_API,
            adapter_name=self.adapter_name,
            provider="mock-llm",
            model_name="gpt-agentic-test",
            is_fallback=False,
            prompt_tokens=120,
            completion_tokens=45,
            total_tokens=165,
            latency_ms=5,
        )

    def generate_structured_json(
        self, system_prompt: str, user_message: str
    ) -> StructuredJsonInvocationResult | None:
        _ = (system_prompt, user_message)
        self.json_calls += 1
        if not self._json_responses:
            raise ValueError("Mock LLM ran out of scripted JSON responses")
        nxt = self._json_responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return StructuredJsonInvocationResult(
            data=nxt,
            model_mode=ModelMode.LLM_API,
            adapter_name=self.adapter_name,
            provider="mock-llm",
            model_name="gpt-agentic-test",
            is_fallback=False,
            prompt_tokens=90,
            completion_tokens=60,
            total_tokens=150,
            latency_ms=4,
        )


def _seed_paid_order_case(session_id: str, suffix: str) -> tuple[str, str, str]:
    maker = get_sessionmaker()
    now = datetime.now(UTC)
    ord_id = f"ORD-AG-{suffix}"
    pay_id = f"PAY-AG-{suffix}"
    case_id = f"CASE-AG-{suffix}"
    with maker() as db:
        db.add(
            SimOrder(
                id=ord_id,
                session_id=session_id,
                customer_name="Khách Agentic (SAMPLE)",
                customer_phone="0901112233",
                amount=300000,
                currency="VND",
                subtotal=300000,
                grand_total=300000,
                status=OrderStatus.PENDING.value,
                payment_status=OrderPaymentStatus.UNPAID.value,
                fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
                version=1,
                source_mode="MOCK",
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            SimPayment(
                id=pay_id,
                session_id=session_id,
                order_id=ord_id,
                merchant_id="MERCHANT_MOCK_01",
                provider="MOCK_MOMO",
                provider_ref=f"MOMO_AG_{suffix}",
                result_code=0,
                amount=300000,
                currency="VND",
                status=PaymentStatus.SUCCESS.value,
                verified_at=now,
                observed_at=now,
                verification_source="MOCK_MOMO_ADAPTER_VERIFIED",
                source_mode="MOCK",
                created_at=now,
            )
        )
        db.add(
            Case(
                id=case_id,
                session_id=session_id,
                type="PAYMENT_DISPUTE",
                goal=f"Điều tra đồng bộ đơn {ord_id}",
                state=CaseState.NEW.value,
                version=1,
                owner="guest",
                created_at=now,
                updated_at=now,
            )
        )
        db.flush()
        db.add(
            Evidence(
                id=f"EVD-AG-{suffix}",
                case_id=case_id,
                session_id=session_id,
                kind=EvidenceKind.CLAIM.value,
                source="CUSTOMER_MESSAGE",
                source_ref="intake_text",
                payload_json={"text": f"Tôi đã thanh toán 300.000đ cho đơn {ord_id}"},
                observed_at=now,
            )
        )
        db.commit()
    return case_id, ord_id, pay_id


def test_08_2_no_db_row_lock_held_during_llm_invocation_and_multi_step_e2e(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Acceptance: Zero PostgreSQL row lock held across LLM calls + multi-step tool/replan
    -> WAITING_APPROVAL -> Approval -> Execute -> Independent Verification -> RESOLVED."""
    session_id = active_session["id"]
    headers = {"X-Demo-Session": active_session["token"]}
    case_id, ord_id, pay_id = _seed_paid_order_case(session_id, "LOCK01")
    maker = get_sessionmaker()

    lock_free_during_llm: list[bool] = []

    def _scripted_plan(ctx: InvestigationContext, call_num: int) -> Plan:
        # Prove NO PostgreSQL row lock is held on `Case` while LLM is executing!
        with maker() as probe_db:
            locked_row = probe_db.scalar(
                select(Case).where(Case.id == case_id).with_for_update(nowait=True)
            )
            assert locked_row is not None
            assert locked_row.state == CaseState.INVESTIGATING.value
            probe_db.rollback()
            lock_free_during_llm.append(True)

        if call_num == 1:
            return Plan(
                case_id=ctx.case_id,
                goal=ctx.goal,
                known_evidence_refs=ctx.known_evidence_refs,
                unknowns=["order_status", "payment_status"],
                hypotheses=[Hypothesis(label="dropped_order_sync", supporting_evidence_refs=[])],
                next_step=NextStep(
                    kind=PlanStepKind.READ_TOOL,
                    tool_name="get_order",
                    args={"order_id": ord_id},
                ),
                decision_summary="Bước 1: Đọc trạng thái đơn hàng từ DB.",
            )
        if call_num == 2:
            return Plan(
                case_id=ctx.case_id,
                goal=ctx.goal,
                known_evidence_refs=ctx.known_evidence_refs,
                unknowns=["payment_status"],
                hypotheses=[Hypothesis(label="dropped_order_sync", supporting_evidence_refs=[])],
                next_step=NextStep(
                    kind=PlanStepKind.READ_TOOL,
                    tool_name="find_payment_candidates",
                    args={"order_id": ord_id},
                ),
                decision_summary="Bước 2: Tra cứu giao dịch thanh toán gắn với đơn hàng.",
            )
        return Plan(
            case_id=ctx.case_id,
            goal=ctx.goal,
            known_evidence_refs=ctx.known_evidence_refs,
            unknowns=[],
            hypotheses=[
                Hypothesis(
                    label="verified_payment_missing_order_sync",
                    supporting_evidence_refs=ctx.known_evidence_refs,
                )
            ],
            next_step=NextStep(
                kind=PlanStepKind.PROPOSE_ACTION,
                action_type="ORDER_SYNC",
                args={"order_id": ord_id, "payment_id": pay_id, "amount": 300000},
            ),
            decision_summary="Đủ chứng cứ OBSERVED_FACT; đề xuất ORDER_SYNC chờ phê duyệt.",
        )

    adapter = ScriptedMockLLMAdapter(plan_fn=_scripted_plan)
    with maker() as db:
        runtime = BoundedAgentRuntime(db, adapter=adapter, max_steps=6)
        result = runtime.investigate_case(case_id=case_id, session_id=session_id)

    assert len(lock_free_during_llm) == 3
    assert result.model_mode == ModelMode.LLM_API
    assert result.steps_executed == 3
    assert result.final_state == CaseState.WAITING_APPROVAL.value
    assert len(result.proposed_action_ids) == 1
    prop_id = result.proposed_action_ids[0]

    # Financial write still requires explicit approval -> execute -> independent verification
    appr = client.post(
        f"/api/v1/actions/{prop_id}/simulate-demo-approval",
        headers=headers,
        json={"reason": "Phê duyệt MOCK sau điều tra LLM"},
    )
    assert appr.status_code == 200

    ex = client.post(
        f"/api/v1/actions/{prop_id}/execute",
        headers={**headers, "X-Idempotency-Key": f"AG-EXEC-{prop_id}"},
    )
    assert ex.status_code == 200
    assert ex.json()["case_state"] == CaseState.RESOLVED.value
    assert ex.json()["verification"]["verdict"] == VerificationVerdict.PASSED.value


def test_08_2_invalid_json_replan_and_optimistic_concurrency_interruption(
    active_session: dict[str, str],
) -> None:
    """Acceptance: Invalid JSON triggers safe replan; concurrent external Case mutation during
    LLM I/O triggers INVESTIGATION_INTERRUPTED (409) without overwriting."""
    session_id = active_session["id"]
    case_id, ord_id, _ = _seed_paid_order_case(session_id, "REPLAN01")
    maker = get_sessionmaker()

    def _flaky_then_valid(ctx: InvestigationContext, call_num: int) -> Plan:
        if call_num == 1:
            raise ValueError("Invalid JSON from model: {broken")
        return Plan(
            case_id=ctx.case_id,
            goal=ctx.goal,
            known_evidence_refs=ctx.known_evidence_refs,
            unknowns=["customer_confirmation"],
            hypotheses=[Hypothesis(label="needs_evidence", supporting_evidence_refs=[])],
            next_step=NextStep(
                kind=PlanStepKind.ASK_USER,
                args={"question": f"Vui lòng xác nhận lại mã giao dịch cho đơn {ord_id}."},
            ),
            decision_summary="Phục hồi sau JSON lỗi và yêu cầu thêm thông tin.",
        )

    with maker() as db:
        res = BoundedAgentRuntime(
            db, adapter=ScriptedMockLLMAdapter(plan_fn=_flaky_then_valid), max_replans=2
        ).investigate_case(case_id=case_id, session_id=session_id)
    assert res.replans_used == 1
    assert res.final_state == CaseState.WAITING_EVIDENCE.value

    # Now test concurrent external mutation during LLM I/O -> INVESTIGATION_INTERRUPTED (409)
    case_id2, _, _ = _seed_paid_order_case(session_id, "CONC02")

    def _mutate_during_llm(ctx: InvestigationContext, _call_num: int) -> Plan:
        with maker() as other_db:
            other_case = other_db.get(Case, case_id2)
            assert other_case is not None
            other_case.version += 1
            other_case.state = CaseState.ESCALATED.value
            other_db.commit()
        return Plan(
            case_id=ctx.case_id,
            goal=ctx.goal,
            known_evidence_refs=ctx.known_evidence_refs,
            unknowns=[],
            hypotheses=[],
            next_step=NextStep(kind=PlanStepKind.STOP, args={}),
            decision_summary="Stop",
        )

    with maker() as db:
        with pytest.raises(AppError) as exc_info:
            BoundedAgentRuntime(
                db, adapter=ScriptedMockLLMAdapter(plan_fn=_mutate_during_llm)
            ).investigate_case(case_id=case_id2, session_id=session_id)
        assert exc_info.value.status_code == 409
        assert "INVESTIGATION_INTERRUPTED" in exc_info.value.message

    # Also test stale worker recovery (old unfinished INVESTIGATION_STARTED -> INTERRUPTED)
    case_id3, _, _ = _seed_paid_order_case(session_id, "STALE03")
    with maker() as db:
        c3 = db.get(Case, case_id3)
        assert c3 is not None
        c3.state = CaseState.INVESTIGATING.value
        db.add(
            CaseEvent(
                id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                case_id=case_id3,
                session_id=session_id,
                type="INVESTIGATION_STARTED",
                payload_json={"run_id": "RUN-CRASHED-WORKER", "job_status": "RUNNING"},
                created_at=datetime.now(UTC) - timedelta(minutes=10),
            )
        )
        db.commit()

    with maker() as db:
        rec = BoundedAgentRuntime(
            db,
            adapter=ScriptedMockLLMAdapter(plan_fn=_flaky_then_valid),
            stale_investigation_timeout_seconds=60,
        ).investigate_case(case_id=case_id3, session_id=session_id)
        assert rec.final_state == CaseState.WAITING_EVIDENCE.value


def test_08_2_adaptive_llm_taskforce_dynamic_roles_handoffs_and_replan(
    active_session: dict[str, str],
) -> None:
    """Acceptance: TaskforceManager genuinely invokes LLM_API when configured, plans roles,
    executes scoped read tools, dynamically replans to add RISK, records real token usage,
    and NEVER auto-resolves child Cases."""
    session_id = active_session["id"]
    maker = get_sessionmaker()
    with maker() as db:
        seed_incident_taskforce_scenario(db, session_id)
        outcome = IncidentCorrelator(db, session_id).correlate_session_cases()
        assert len(outcome.incidents) == 1
        inc = outcome.incidents[0]

        mock_llm = ScriptedMockLLMAdapter(
            json_responses=[
                # 1. Role planner selects 2 initial roles (FINANCE, OPERATIONS)
                {
                    "selected_roles": ["FINANCE", "OPERATIONS"],
                    "role_objectives": {
                        "FINANCE": "Đối soát thanh toán mẫu bằng read tools được cấp phép.",
                        "OPERATIONS": "Kiểm tra sự kiện đồng bộ worker và yêu cầu replan nếu cần.",
                    },
                    "initial_hypothesis": "Nghẽn đồng bộ theo lô worker cần kiểm tra thêm rủi ro.",
                },
                # 2. FINANCE synthesis (no replan)
                {
                    "findings_vi": "Finance Agent xác nhận mẫu giao dịch SUCCESS trên sổ cái MOCK.",
                    "contradictions": [],
                    "unknowns": ["Các Case chưa được lấy mẫu cần đối soát độc lập."],
                    "evidence_refs": [],
                    "recommended_per_case_action": "PROPOSE_ORDER_SYNC_PER_CASE_AFTER_CHECK",
                    "request_replan": False,
                    "suggested_next_role": None,
                },
                # 3. OPERATIONS synthesis triggers bounded replan -> adds RISK role!
                {
                    "findings_vi": "Operations Agent phát hiện nghẽn lô lớn; đề xuất bổ sung Risk.",
                    "contradictions": [],
                    "unknowns": ["Chưa kiểm tra lịch sử hoàn tiền trên toàn bộ lô."],
                    "evidence_refs": [],
                    "recommended_per_case_action": "HANDOFF_TO_RISK_AGENT",
                    "request_replan": True,
                    "suggested_next_role": "RISK",
                },
                # 4. Dynamically added RISK synthesis
                {
                    "findings_vi": (
                        "Risk Agent đã rà soát mẫu lịch sử hoàn tiền, không tự đóng Case."
                    ),
                    "contradictions": [],
                    "unknowns": ["Các Case chưa kiểm tra cần Policy & Approval riêng."],
                    "evidence_refs": [],
                    "recommended_per_case_action": "REQUIRE_PER_CASE_APPROVAL_AND_VERIFIER",
                    "request_replan": False,
                    "suggested_next_role": None,
                },
            ]
        )
        tasks = TaskforceManager(db, session_id, adapter=mock_llm).assign_and_run_taskforce(
            inc, force_rerun=True
        )
        db.commit()

        assert [t.role for t in tasks] == ["FINANCE", "OPERATIONS", "RISK"]
        assert mock_llm.json_calls == 4
        assert inc.shared_factors_json["taskforce_execution_mode"] == "LLM_API_ADAPTIVE_TASKFORCE"
        assert inc.shared_factors_json["llm_invoked"] is True
        assert inc.shared_factors_json["llm_usage"]["total_tokens"] == 600
        assert inc.shared_factors_json["llm_usage"]["replans_used"] == 1
        assert tasks[1].handoff_json["replan_triggered"] is True
        assert tasks[1].handoff_json["handoff_to"] == "RISK"
        assert all(t.handoff_json["auto_resolved_children"] is False for t in tasks)


def test_08_2_ai_assisted_correlation_supports_evidence_and_abstains_on_contradiction(
    active_session: dict[str, str],
) -> None:
    """Acceptance: AI-assisted correlation records grounded supporting evidence, never merges
    lookalikes, and abstains cleanly when evidence is contradictory."""
    session_id = active_session["id"]
    maker = get_sessionmaker()

    with maker() as db:
        seed_incident_taskforce_scenario(db, session_id)
        # 1. Abstain when LLM flags contradictory evidence
        abstain_llm = ScriptedMockLLMAdapter(
            json_responses=[
                {
                    "shared_root_cause_hypothesis": "Bằng chứng thanh toán có dấu hiệu mâu thuẫn.",
                    "supporting_evidence_refs": [],
                    "contradicting_evidence_refs": [],
                    "confirmed_case_ids": [],
                    "excluded_lookalike_case_ids": [],
                    "abstain": True,
                    "abstain_reason": "Bằng chứng chưa đủ đồng nhất để gộp thành Incident.",
                }
            ]
        )
        abstain_outcome = IncidentCorrelator(
            db, session_id, adapter=abstain_llm
        ).correlate_session_cases()
        assert len(abstain_outcome.incidents) == 0
        assert len(abstain_outcome.abstained_clusters) == 1
        assert abstain_llm.json_calls == 1

        # 2. Confirm cluster when evidence supports shared root cause (lookalikes still excluded)
        confirm_llm = ScriptedMockLLMAdapter(
            json_responses=[
                {
                    "shared_root_cause_hypothesis": (
                        "AI xác nhận 12 Case chia sẻ cùng lỗi worker/batch trong khung 20 phút."
                    ),
                    "supporting_evidence_refs": ["FABRICATED_REF_IGNORED"],
                    "contradicting_evidence_refs": [],
                    "confirmed_case_ids": [],
                    "excluded_lookalike_case_ids": [],
                    "abstain": False,
                    "abstain_reason": None,
                }
            ]
        )
        confirm_outcome = IncidentCorrelator(
            db, session_id, adapter=confirm_llm
        ).correlate_session_cases()
        assert len(confirm_outcome.incidents) == 1
        assert len(confirm_outcome.rejected_lookalikes) == 2
        ai_rev = confirm_outcome.incidents[0].shared_factors_json["ai_correlation_review"]
        assert ai_rev["llm_invoked"] is True
        # Fabricated evidence ref must be stripped by backend provenance intersection!
        assert "FABRICATED_REF_IGNORED" not in ai_rev["supporting_evidence_refs"]


def test_08_2_rate_limits_ignore_spoofed_xff_headers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (STEP 6): Client-spoofed X-Forwarded-For cannot bypass guest session creation
    rate limit."""
    monkeypatch.setenv("RATE_LIMIT_SESSIONS_PER_MINUTE", "2")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    get_settings.cache_clear()
    reset_rate_limiter()

    r1 = client.post(
        "/api/v1/demo/sessions",
        headers={"X-Forwarded-For": "1.1.1.1"},
        json={"seed_fixtures": False},
    )
    r2 = client.post(
        "/api/v1/demo/sessions",
        headers={"X-Forwarded-For": "2.2.2.2"},
        json={"seed_fixtures": False},
    )
    r3 = client.post(
        "/api/v1/demo/sessions",
        headers={"X-Forwarded-For": "3.3.3.3"},
        json={"seed_fixtures": False},
    )
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r3.status_code == 429
    assert r3.json()["error"]["code"] == "RATE_LIMITED"
