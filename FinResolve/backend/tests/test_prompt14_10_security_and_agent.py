"""Regression tests for Prompt 14.10 — Controller Reset Auth Bypass, Fail-Closed Dispatch
Fencing, Gemini Transient Retry/Fallback, Evidence-Guided Replanning & Honest Benchmark Counters.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_core.context import InvestigationContext
from app.ai_core.model_adapter import LLMModelAdapter, ModelInvocationResult
from app.ai_core.runtime import BoundedAgentRuntime
from app.ai_core.schemas import (
    Hypothesis,
    NextStep,
    Plan,
    PlanStepKind,
)
from app.connectors.commerce_write import execute_and_verify_commerce_recovery
from app.core.config import ModelMode, Settings, get_settings
from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.models.cases import Case, CaseEvent, Evidence
from app.models.enums import CaseState, EvidenceKind, SessionStatus
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.session import DemoSession
from app.models.supervisor import SupervisorState
from app.monitoring.service import ensure_commerce_binding
from app.supervisor.service import ensure_supervisor_state


def test_read_only_guest_cannot_elevate_to_controller_via_reset_scan_or_release(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0 Security Fix: When EXPERIENCE_OPERATOR_TOKEN is configured and NO controller exists,
    a READ_ONLY_PREVIEW guest MUST be rejected (403) on /reset, /scan, and /release and
    MUST NEVER obtain an enabled CommerceSourceBinding."""
    monkeypatch.setenv("COMMERCE_SERVICE_BASE_URL", "http://127.0.0.1:8100")
    monkeypatch.setenv("EXPERIENCE_OPERATOR_TOKEN", "op-secret-1410-strict-token")
    get_settings.cache_clear()
    db_session: Session = get_sessionmaker()()
    try:
        # Anonymous bootstrap without X-Operator-Token -> READ_ONLY_PREVIEW
        boot = client.post("/api/v1/experience/bootstrap", json={"ttl_minutes": 60})
        assert boot.status_code == 200
        boot_data = boot.json()
        assert boot_data["session"]["workspace_role"] == "READ_ONLY_PREVIEW"
        assert boot_data["sandbox"]["binding_id"] is None
        guest_token = boot_data["session"]["token"]
        guest_sid = boot_data["session"]["session_id"]

        # Overview MUST report READ_ONLY_PREVIEW even when no other controller exists
        ov = client.get("/api/v1/experience/overview", headers={"X-Demo-Session": guest_token})
        assert ov.status_code == 200
        assert ov.json()["workspace_role"] == "READ_ONLY_PREVIEW"

        # Calling /reset, /scan, /release as READ_ONLY_PREVIEW MUST fail-closed with 403!
        r_reset = client.post("/api/v1/experience/reset", headers={"X-Demo-Session": guest_token})
        assert r_reset.status_code == 403
        assert "CONTROLLER_AUTHORIZATION_REQUIRED" in json.dumps(r_reset.json())

        r_scan = client.post("/api/v1/experience/scan", headers={"X-Demo-Session": guest_token})
        assert r_scan.status_code == 403
        assert "CONTROLLER_AUTHORIZATION_REQUIRED" in json.dumps(r_scan.json())

        r_rel = client.post("/api/v1/experience/release", headers={"X-Demo-Session": guest_token})
        assert r_rel.status_code == 403
        assert "CONTROLLER_AUTHORIZATION_REQUIRED" in json.dumps(r_rel.json())

        # Verify ZERO enabled bindings exist for guest
        db_session.expire_all()
        enabled_bindings = list(
            db_session.scalars(
                select(CommerceSourceBinding).where(
                    CommerceSourceBinding.session_id == guest_sid,
                    CommerceSourceBinding.is_enabled.is_(True),
                )
            ).all()
        )
        assert enabled_bindings == []

        # Now authenticate with valid X-Operator-Token -> promoted to CONTROLLER
        op_boot = client.post(
            "/api/v1/experience/bootstrap",
            headers={
                "X-Demo-Session": guest_token,
                "X-Operator-Token": "op-secret-1410-strict-token",
            },
            json={"ttl_minutes": 60},
        )
        assert op_boot.status_code == 200
        assert op_boot.json()["session"]["workspace_role"] == "CONTROLLER"
        assert op_boot.json()["sandbox"]["binding_id"] is not None

        # Active controller CAN reset cleanly
        r_reset_ok = client.post(
            "/api/v1/experience/reset", headers={"X-Demo-Session": guest_token}
        )
        assert r_reset_ok.status_code == 200
        assert r_reset_ok.json()["world_epoch"] == 2
    finally:
        db_session.close()
        get_settings.cache_clear()


def test_commerce_write_dispatch_fails_closed_on_expired_session_or_revoked_binding() -> None:
    """Fail-closed dispatch fencing rejects expired sessions and revoked/disabled bindings."""
    db_session: Session = get_sessionmaker()()
    try:
        now = datetime.now(UTC)
        sess = DemoSession(
            id=f"sess_exp_{uuid.uuid4().hex[:8]}",
            token=f"dsk_{uuid.uuid4().hex}",
            status=SessionStatus.ACTIVE.value,
            max_cases=10,
            case_count=0,
            expires_at=now - timedelta(minutes=5),  # EXPIRED!
            created_at=now - timedelta(hours=1),
            updated_at=now - timedelta(hours=1),
        )
        db_session.add(sess)
        db_session.flush()
        ensure_supervisor_state(db_session, sess.id)
        binding = ensure_commerce_binding(
            db_session,
            session_id=sess.id,
            connector_id="default_ministore",
            base_url="http://127.0.0.1:8100",
            merchant_id="MERCHANT_FINSHOP_01",
            commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
            commerce_world_epoch=1,
            is_enabled=True,
        )
        obs = AnomalyObservation(
            id=f"ANOM-{uuid.uuid4().hex[:8].upper()}",
            session_id=sess.id,
            source_binding_id=binding.id,
            merchant_id="MERCHANT_FINSHOP_01",
            world_epoch=1,
            detector_code="D01",
            anomaly_type="PAYMENT_ORDER_DESYNC",
            severity="HIGH",
            fingerprint=f"d01:{uuid.uuid4().hex[:8]}",
            lifecycle_state="INVESTIGATING",
            affected_order_id="ORD-EXP-1",
            affected_payment_id="PAY-EXP-1",
            rationale="test",
            evidence_refs_json=[],
            first_observed_at=now,
            last_observed_at=now,
        )
        db_session.add(obs)
        db_session.commit()

        with pytest.raises(AppError) as exc_info:
            execute_and_verify_commerce_recovery(
                db_session,
                binding=binding,
                obs=obs,
                command="SYNC_ORDER_PAYMENT_STATUS",
                idempotency_key=f"idem-exp-{uuid.uuid4().hex[:8]}",
                expected_version=1,
                order_snapshot={
                    "id": "ORD-EXP-1",
                    "version": 1,
                    "total_vnd": 100000,
                    "status": "PENDING",
                },
                payment_snapshot={
                    "id": "PAY-EXP-1",
                    "order_id": "ORD-EXP-1",
                    "amount_vnd": 100000,
                    "status": "SUCCEEDED",
                },
            )
        assert exc_info.value.status_code == 403
        assert "CONTROLLER_SESSION_EXPIRED" in exc_info.value.message

        # Un-expire session, disable binding -> MUST fail closed with 403
        sess.expires_at = now + timedelta(hours=1)
        binding.is_enabled = False
        db_session.commit()

        with pytest.raises(AppError) as exc_info2:
            execute_and_verify_commerce_recovery(
                db_session,
                binding=binding,
                obs=obs,
                command="SYNC_ORDER_PAYMENT_STATUS",
                idempotency_key=f"idem-revoked-{uuid.uuid4().hex[:8]}",
                expected_version=1,
                order_snapshot={
                    "id": "ORD-EXP-1",
                    "version": 1,
                    "total_vnd": 100000,
                    "status": "PENDING",
                },
                payment_snapshot={
                    "id": "PAY-EXP-1",
                    "order_id": "ORD-EXP-1",
                    "amount_vnd": 100000,
                    "status": "SUCCEEDED",
                },
            )
        assert exc_info2.value.status_code == 403
        assert "CONTROLLER_OWNERSHIP_REVOKED" in exc_info2.value.message
    finally:
        db_session.close()


def test_llm_adapter_retries_transient_503_and_falls_back_to_configured_model() -> None:
    """LLMModelAdapter retries HTTP 503 with bounded backoff and falls back to MODEL_FALLBACK_NAME."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "gemini-3.8-flash" in str(request.url):
            return httpx.Response(
                503,
                headers={"Retry-After": "0.1"},
                json={"error": {"code": 503, "message": "high demand"}},
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": json.dumps({"ok": True, "model": "fallback"})}]
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 12,
                    "candidatesTokenCount": 8,
                    "totalTokenCount": 20,
                },
            },
        )

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(
        model_provider="gemini",
        model_name="gemini-3.8-flash",
        model_fallback_name="gemini-3.5-flash-lite",
        model_api_key="test-key-not-real",
        model_max_retries=1,
        model_timeout_seconds=10.0,
    )
    adapter = LLMModelAdapter(settings=settings, http_client=mock_client)
    res = adapter.generate_structured_json("sys", "user")

    assert res.data == {"ok": True, "model": "fallback"}
    assert res.model_name == "gemini-3.5-flash-lite"
    assert res.retry_count == 1
    assert res.fallback_reason is not None
    assert len(calls) == 3  # 2 attempts on primary (initial + 1 retry) + 1 fallback


def test_evidence_guided_replanning_recovers_missing_expected_order_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LLM proposes ORDER_SYNC without expected_order_version on COMMERCE_HTTP, runtime
    gives structured validation_feedback and allows replanning instead of terminating early."""
    db_session: Session = get_sessionmaker()()
    try:
        _run_replanning_test(db_session, monkeypatch)
    finally:
        db_session.close()


def _run_replanning_test(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    sess = DemoSession(
        id=f"sess_replan_{uuid.uuid4().hex[:8]}",
        token=f"dsk_{uuid.uuid4().hex}",
        status=SessionStatus.ACTIVE.value,
        max_cases=10,
        case_count=1,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    db_session.add(sess)
    db_session.flush()
    ensure_supervisor_state(db_session, sess.id)
    binding = ensure_commerce_binding(
        db_session,
        session_id=sess.id,
        connector_id="default_ministore",
        base_url="http://127.0.0.1:8100",
        merchant_id="MERCHANT_FINSHOP_01",
        commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
        commerce_world_epoch=1,
        is_enabled=True,
    )
    case = Case(
        id=f"CASE-RP-{uuid.uuid4().hex[:6].upper()}",
        session_id=sess.id,
        type="PAYMENT_SYNC",
        state=CaseState.NEW.value,
        goal="Investigate order ORD-RP-1 and payment PAY-RP-1",
        source_system="COMMERCE_HTTP",
        source_binding_id=binding.id,
        version=1,
        created_at=now,
        updated_at=now,
    )
    db_session.add(case)
    db_session.add(
        Evidence(
            id=f"EVD-{uuid.uuid4().hex[:8].upper()}",
            case_id=case.id,
            session_id=sess.id,
            kind=EvidenceKind.CLAIM.value,
            source="monitor",
            source_ref="ANOM-1",
            payload_json={"text": "Order ORD-RP-1 still PENDING while PAY-RP-1 SUCCEEDED"},
            observed_at=now,
        )
    )
    db_session.commit()

    class FakeCommerceConnector:
        def get_order(self, order_id: str) -> dict[str, object]:
            return {
                "endpoint": f"/api/v1/service/orders/{order_id}",
                "observed_at": datetime.now(UTC).isoformat(),
                "data": {
                    "id": order_id,
                    "order_id": order_id,
                    "status": "PENDING",
                    "total_vnd": 250000,
                    "version": 3,
                    "merchant_id": "MERCHANT_FINSHOP_01",
                    "commerce_session_id": "CSESS_FINSHOP_DEFAULT_01",
                    "world_epoch": 1,
                },
                "freshness_ms": 5,
            }

        def get_payment(self, payment_id: str) -> dict[str, object]:
            return {
                "endpoint": f"/api/v1/service/payments/{payment_id}",
                "observed_at": datetime.now(UTC).isoformat(),
                "data": {
                    "id": payment_id,
                    "payment_id": payment_id,
                    "order_id": "ORD-RP-1",
                    "status": "SUCCEEDED",
                    "amount_vnd": 250000,
                    "merchant_id": "MERCHANT_FINSHOP_01",
                    "commerce_session_id": "CSESS_FINSHOP_DEFAULT_01",
                },
                "freshness_ms": 5,
            }

    monkeypatch.setattr(
        "app.monitoring.service.build_connector_for_binding",
        lambda _b: FakeCommerceConnector(),
    )

    class ReplanningFakeAdapter:
        mode = ModelMode.LLM_API
        adapter_name = "FakeGeminiReplan"

        def __init__(self) -> None:
            self.step = 0

        def generate_plan(self, context: InvestigationContext) -> ModelInvocationResult:
            self.step += 1
            if self.step == 1:
                ns = NextStep(
                    kind=PlanStepKind.READ_TOOL,
                    tool_name="get_order",
                    args={"order_id": "ORD-RP-1"},
                )
            elif self.step == 2:
                # Prematurely proposes ORDER_SYNC without get_payment_record!
                ns = NextStep(
                    kind=PlanStepKind.PROPOSE_ACTION,
                    action_type="propose_order_sync",
                    args={"order_id": "ORD-RP-1", "payment_id": "PAY-RP-1"},
                )
            elif self.step == 3:
                assert "MISSING_EVIDENCE_CATEGORY(payment)" in (
                    context.validation_error_feedback or ""
                )
                ns = NextStep(
                    kind=PlanStepKind.READ_TOOL,
                    tool_name="get_payment_record",
                    args={"payment_id": "PAY-RP-1"},
                )
            elif self.step == 4:
                # Proposes ORDER_SYNC but omits expected_order_version!
                ns = NextStep(
                    kind=PlanStepKind.PROPOSE_ACTION,
                    action_type="propose_order_sync",
                    args={"order_id": "ORD-RP-1", "payment_id": "PAY-RP-1"},
                )
            else:
                assert "MISSING_REQUIRED_ARGUMENT(expected_order_version)" in (
                    context.validation_error_feedback or ""
                )
                ns = NextStep(
                    kind=PlanStepKind.PROPOSE_ACTION,
                    action_type="propose_order_sync",
                    args={
                        "order_id": "ORD-RP-1",
                        "payment_id": "PAY-RP-1",
                        "expected_order_version": 3,
                    },
                )
            plan = Plan(
                case_id=context.case_id,
                goal=context.goal,
                known_evidence_refs=context.known_evidence_refs,
                unknowns=[],
                hypotheses=[Hypothesis(label="payment_sync", supporting_evidence_refs=[])],
                next_step=ns,
                decision_summary="Tiến hành xác minh và đồng bộ đơn hàng.",
            )
            return ModelInvocationResult(
                plan=plan,
                model_mode=ModelMode.LLM_API,
                adapter_name=self.adapter_name,
                provider="gemini",
                model_name="gemini-3.5-flash",
                is_fallback=False,
                total_tokens=100,
            )

        def generate_structured_json(self, system_prompt: str, user_message: str) -> None:
            return None

    rt = BoundedAgentRuntime(db_session, adapter=ReplanningFakeAdapter(), max_steps=6, max_replans=2)
    res = rt.investigate_case(case_id=case.id, session_id=sess.id)

    assert res.stop_reason == "ACTION_PROPOSED"
    assert res.final_state == CaseState.WAITING_APPROVAL.value
    assert res.replans_used == 2
    assert len(res.proposed_action_ids) == 1
    replan_events = list(
        db_session.scalars(
            select(CaseEvent).where(
                CaseEvent.case_id == case.id,
                CaseEvent.type == "PLAN_REVISED",
            )
        ).all()
    )
    assert len(replan_events) == 2


def test_benchmark_safety_counters_detect_unauthorized_and_duplicate_mutations() -> None:
    """Safety counters MUST be independently measured, never hardcoded to 0."""
    from scripts.run_benchmark import measure_safety_counters

    cmds = [
        SimpleNamespace(
            state="VERIFIED",
            authorization_digest="abc123",
            policy_decision="ALLOW",
            command="SYNC_ORDER_PAYMENT_STATUS",
            target_order_id="ORD-1",
        ),
        SimpleNamespace(
            state="VERIFIED",
            authorization_digest="abc124",
            policy_decision="ALLOW",
            command="SYNC_ORDER_PAYMENT_STATUS",
            target_order_id="ORD-1",  # duplicate!
        ),
        SimpleNamespace(
            state="EXECUTING",
            authorization_digest="",  # missing digest -> unauthorized!
            policy_decision="DENY",
            command="SYNC_ORDER_PAYMENT_STATUS",
            target_order_id="ORD-HEALTHY",  # unexpected target -> unauthorized!
        ),
    ]
    counters = measure_safety_counters(commands=cmds, expected_target_order_ids={"ORD-1"})
    assert counters["unauthorized_mutations"] == 1
    assert counters["duplicate_mutations"] == 1
    assert counters["total_executed_commands"] == 3
