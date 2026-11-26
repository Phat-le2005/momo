"""Prompt 14.7 Security, Sandbox Isolation, Strict Identity & World-Epoch Tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai_core.model_adapter import ModelInvocationResult
from app.ai_core.runtime import BoundedAgentRuntime
from app.ai_core.schemas import Plan
from app.connectors.commerce import CommerceReadConnector
from app.connectors.commerce_write import (
    _compute_command_hash,
    execute_and_verify_commerce_recovery,
    reconcile_unknown_recovery_command,
)
from app.core.config import ModelMode
from app.db.session import get_sessionmaker
from app.models.actions import ActionProposal
from app.models.cases import Case, CaseEvent, Evidence
from app.models.enums import ActionProposalStatus, CaseState, RiskLevel, SessionStatus
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.open_world import RecoveryCommand
from app.models.session import DemoSession
from app.models.supervisor import SupervisorJob


def _seed_ws(
    db: Any, *, csess: str = "CSESS_FINSHOP_DEFAULT_01"
) -> tuple[DemoSession, CommerceSourceBinding]:
    now = datetime.now(UTC)
    sess = DemoSession(
        id=f"sess_{uuid.uuid4().hex[:12]}",
        token=f"dsk_{uuid.uuid4().hex}",
        status=SessionStatus.ACTIVE.value,
        max_cases=20,
        case_count=0,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )
    db.add(sess)
    db.flush()
    binding = CommerceSourceBinding(
        id=f"CSB-{uuid.uuid4().hex[:10].upper()}",
        session_id=sess.id,
        world_epoch=1,
        connector_id="default_ministore",
        base_url="http://127.0.0.1:8100",
        merchant_id="MERCHANT_FINSHOP_01",
        commerce_session_id=csess,
        commerce_world_epoch=1,
        is_enabled=True,
        connectivity_status="CONNECTED",
        created_at=now,
        updated_at=now,
    )
    db.add(binding)
    db.commit()
    return sess, binding


# ==============================================================================
# FIX 1 & FIX 4: Two-Workspace Isolation, World-Epoch Reset & Secret Scrubbing
# ==============================================================================


def test_fix1_fix4_two_workspaces_and_world_epoch_reset_isolation(client: TestClient) -> None:
    """Verify two guest workspaces A & B are isolated, World 1 -> World 2 reset fences
    anomalies/approvals/cases/timeline, unresolved guards survive reset, and traces redact secrets.
    """
    # Browser cannot inject arbitrary merchant_id / commerce_session_id / base_url
    r_bad = client.post(
        "/api/v1/experience/bootstrap",
        json={"merchant_id": "EVIL_MERCHANT", "base_url": "http://evil.local"},
    )
    assert r_bad.status_code == 422

    # Create Workspace A & Workspace B
    boot_a = client.post("/api/v1/experience/bootstrap", json={}).json()
    boot_b = client.post("/api/v1/experience/bootstrap", json={}).json()
    assert boot_a["session"]["session_id"] != boot_b["session"]["session_id"]
    assert boot_a["sandbox"]["sandbox_isolation_mode"] == "SHARED_DEMO_SANDBOX"
    # Newly initialized binding without a verified scan must NOT claim CONNECTED
    assert boot_a["sandbox"]["connectivity_status"] != "CONNECTED"

    ha = {"X-Demo-Session": boot_a["session"]["token"]}
    hb = {"X-Demo-Session": boot_b["session"]["token"]}
    sid_a = boot_a["session"]["session_id"]

    # Seed real World 1 activity inside Workspace A only (anomaly, case, proposal, event, guard)
    sm = get_sessionmaker()
    with sm() as db:
        now = datetime.now(UTC)
        binding_a = db.scalar(
            select(CommerceSourceBinding).where(CommerceSourceBinding.session_id == sid_a)
        )
        if binding_a is None:
            binding_a = CommerceSourceBinding(
                id=f"CSB-{uuid.uuid4().hex[:8].upper()}",
                session_id=sid_a,
                world_epoch=1,
                connector_id="default_ministore",
                base_url="http://127.0.0.1:8100",
                merchant_id="MERCHANT_FINSHOP_01",
                commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
                commerce_world_epoch=1,
                is_enabled=True,
                connectivity_status="CONNECTED",
                created_at=now,
                updated_at=now,
            )
            db.add(binding_a)
            db.flush()

        case_a = Case(
            id=f"CASE-{uuid.uuid4().hex[:8].upper()}",
            session_id=sid_a,
            type="PAYMENT_SYNC_ANOMALY",
            goal="World 1 anomaly",
            state=CaseState.WAITING_APPROVAL.value,
            version=1,
            owner="AUTONOMOUS_SUPERVISOR",
            source_system="COMMERCE_HTTP",
            source_binding_id=binding_a.id,
            created_at=now,
            updated_at=now,
        )
        db.add(case_a)
        db.flush()

        obs_a = AnomalyObservation(
            id=f"ANOM-{uuid.uuid4().hex[:8].upper()}",
            session_id=sid_a,
            world_epoch=1,
            source_binding_id=binding_a.id,
            merchant_id="MERCHANT_FINSHOP_01",
            detector_code="D01",
            anomaly_type="PAYMENT_SYNC_MISMATCH",
            severity="HIGH",
            fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
            lifecycle_state="INVESTIGATING",
            affected_order_id="ORD-WS-A-1",
            affected_payment_id="PAY-WS-A-1",
            linked_case_id=case_a.id,
            rationale="World 1 order unsynced",
            evidence_refs_json=[],
            affected_entities_json={},
            first_observed_at=now,
            last_observed_at=now,
            created_at=now,
            updated_at=now,
        )
        prop_a = ActionProposal(
            id=f"PROP-{uuid.uuid4().hex[:8].upper()}",
            case_id=case_a.id,
            session_id=sid_a,
            action_type="ORDER_SYNC",
            params_json={"order_id": "ORD-WS-A-1", "expected_order_version": 1},
            evidence_refs=[],
            expected_version=1,
            risk_level=RiskLevel.LOW.value,
            status=ActionProposalStatus.PROPOSED.value,
            created_at=now,
        )
        cev_a = CaseEvent(
            id=f"CEV-{uuid.uuid4().hex[:8].upper()}",
            case_id=case_a.id,
            session_id=sid_a,
            type="ACTION_PROPOSED",
            payload_json={
                "decision_summary": "World 1 proposal",
                "api_key": "SUPER_SECRET_SHOULD_BE_REDACTED",
                "authorization_token": "SECRET_TOKEN_XYZ",
            },
            created_at=now,
        )
        uncertain_cmd = RecoveryCommand(
            id=f"RCMD-{uuid.uuid4().hex[:8].upper()}",
            session_id=sid_a,
            idempotency_key=f"IDEMP-GUARD-{uuid.uuid4().hex[:6]}",
            request_hash="a" * 64,
            command="REQUEST_ORDER_SYNC",
            case_id=case_a.id,
            anomaly_id=obs_a.id,
            source_binding_id=binding_a.id,
            merchant_id="MERCHANT_FINSHOP_01",
            commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
            world_epoch=1,
            target_order_id="ORD-WS-A-1",
            policy_decision="ALLOW",
            state="UNKNOWN_OUTCOME",
            execution_certainty="COMMAND_EXECUTION_UNKNOWN",
            created_at=now,
            updated_at=now,
        )
        db.add_all([obs_a, prop_a, cev_a, uncertain_cmd])
        db.commit()
        prop_a_id = prop_a.id

    # 1. Workspace A sees World 1 data with scrubbed technical_trace; Workspace B sees ZERO!
    ov_a1 = client.get("/api/v1/experience/overview", headers=ha).json()
    ov_b1 = client.get("/api/v1/experience/overview", headers=hb).json()

    assert ov_a1["kpis"]["active_anomalies"] == 1
    assert ov_a1["kpis"]["pending_approvals"] == 1
    assert ov_a1["kpis"]["total_cases"] == 1
    assert ov_a1["unresolved_uncertainty_guards"] == 1
    assert len(ov_a1["timeline"]) >= 1
    trace_str = str(ov_a1["timeline"])
    assert "SUPER_SECRET_SHOULD_BE_REDACTED" not in trace_str
    assert "SECRET_TOKEN_XYZ" not in trace_str

    assert ov_b1["kpis"]["active_anomalies"] == 0
    assert ov_b1["kpis"]["pending_approvals"] == 0
    assert ov_b1["kpis"]["total_cases"] == 0
    assert ov_b1["anomalies"] == []

    # 2. Reset Workspace A into World 2
    r_reset = client.post("/api/v1/experience/reset", headers=ha)
    assert r_reset.status_code == 200
    reset_body = r_reset.json()
    assert reset_body["world_epoch"] == 2
    assert reset_body["expired_proposals_count"] == 1
    assert reset_body["unresolved_guard_count"] == 1  # Guard NOT released on reset!

    # 3. World 2 Overview must NOT show World 1 anomalies, proposals, cases, or timeline
    ov_a2 = client.get("/api/v1/experience/overview", headers=ha).json()
    assert ov_a2["world_epoch"] == 2
    assert ov_a2["kpis"]["active_anomalies"] == 0
    assert ov_a2["kpis"]["pending_approvals"] == 0
    assert ov_a2["kpis"]["total_cases"] == 0
    assert ov_a2["anomalies"] == []
    assert ov_a2["approvals"] == []
    assert ov_a2["timeline"] == []
    assert ov_a2["unresolved_uncertainty_guards"] == 1

    # Old proposal is EXPIRED in DB
    with sm() as db:
        old_prop = db.get(ActionProposal, prop_a_id)
        assert old_prop is not None
        assert old_prop.status == ActionProposalStatus.CANCELLED.value


# ==============================================================================
# FIX 2: Strict Complete Receiver Identity (world_epoch, command_id, csess, targets)
# ==============================================================================


@pytest.mark.parametrize(
    "case_name,mutator",
    [
        ("missing_world_epoch", lambda d: d.pop("world_epoch")),
        ("boolean_world_epoch", lambda d: d.__setitem__("world_epoch", True)),
        ("malformed_world_epoch", lambda d: d.__setitem__("world_epoch", "1")),
        ("missing_command_id", lambda d: d.pop("command_id")),
        ("missing_commerce_session", lambda d: d.pop("commerce_session_id")),
        ("mismatched_payment_id", lambda d: d.__setitem__("payment_id", "PAY-OTHER")),
    ],
)
def test_fix2_strict_http200_and_reconciliation_identity_checks(
    case_name: str, mutator: Any
) -> None:
    sm = get_sessionmaker()
    with sm() as db:
        sess, binding = _seed_ws(db)
        now = datetime.now(UTC)
        case = Case(
            id=f"CASE-{uuid.uuid4().hex[:8].upper()}",
            session_id=sess.id,
            type="PAYMENT_SYNC_ANOMALY",
            goal="Test identity",
            state=CaseState.NEW.value,
            version=1,
            owner="AUTONOMOUS_SUPERVISOR",
            source_system="COMMERCE_HTTP",
            source_binding_id=binding.id,
            created_at=now,
            updated_at=now,
        )
        db.add(case)
        db.flush()
        obs = AnomalyObservation(
            id=f"ANOM-{uuid.uuid4().hex[:8].upper()}",
            session_id=sess.id,
            world_epoch=1,
            source_binding_id=binding.id,
            merchant_id=binding.merchant_id,
            detector_code="D01",
            anomaly_type="PAYMENT_SYNC_MISMATCH",
            severity="HIGH",
            fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
            lifecycle_state="INVESTIGATING",
            affected_order_id="ORD-147-01",
            affected_payment_id="PAY-147-01",
            linked_case_id=case.id,
            rationale="Test",
            evidence_refs_json=[],
            affected_entities_json={},
            first_observed_at=now,
            last_observed_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(obs)
        db.commit()

        idemp = f"IDEMP-147-{case_name}"
        req_hash = _compute_command_hash(
            command="REQUEST_ORDER_SYNC",
            merchant_id=binding.merchant_id,
            commerce_session_id=binding.commerce_session_id,
            world_epoch=1,
            order_id="ORD-147-01",
            payment_id="PAY-147-01",
            event_id=None,
            expected_version=1,
        )
        resp_body: dict[str, Any] = {
            "command_id": "RCV-14700001",
            "command": "REQUEST_ORDER_SYNC",
            "idempotency_key": idemp,
            "request_hash": req_hash,
            "merchant_id": binding.merchant_id,
            "commerce_session_id": binding.commerce_session_id,
            "world_epoch": 1,
            "order_id": "ORD-147-01",
            "payment_id": "PAY-147-01",
            "status": "EXECUTED",
        }
        mutator(resp_body)

        def _h(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/execute") or "/commands/" in request.url.path:
                return httpx.Response(200, json=resp_body)
            return httpx.Response(404)

        transport = httpx.MockTransport(_h)
        reader = CommerceReadConnector(
            base_url=binding.base_url,
            service_key="rk",
            merchant_id=binding.merchant_id,
            commerce_session_id=binding.commerce_session_id,
            world_epoch=1,
            transport=transport,
        )
        res = execute_and_verify_commerce_recovery(
            db,
            binding=binding,
            obs=obs,
            command="REQUEST_ORDER_SYNC",
            idempotency_key=idemp,
            expected_version=1,
            order_snapshot={"id": "ORD-147-01", "status": "PENDING_PAYMENT", "version": 1},
            payment_snapshot={
                "id": "PAY-147-01",
                "order_id": "ORD-147-01",
                "status": "SUCCEEDED",
                "provider": "LOCAL_MOCK",
                "is_synced_to_order": False,
            },
            write_key="wk",
            transport=transport,
            read_connector=reader,
        )
        assert res.executed is False
        assert res.verdict == "UNKNOWN_OUTCOME"

        # Status reconciliation must ALSO reject the same malformed identity payload!
        cmd_row = db.scalar(select(RecoveryCommand).where(RecoveryCommand.id == res.command_id))
        assert cmd_row is not None
        recon = reconcile_unknown_recovery_command(
            db,
            cmd_row=cmd_row,
            binding=binding,
            obs=obs,
            write_key="wk",
            transport=transport,
            read_connector=reader,
        )
        assert recon.executed is False
        assert recon.verdict == "UNKNOWN_OUTCOME"


# ==============================================================================
# FIX 3: Remove Unsafe Evidence Fallback & Require Returned Payload Match
# ==============================================================================


class _ScriptedAdapter147:
    def __init__(self, plans: list[dict[str, Any]]) -> None:
        self._plans = list(plans)
        self._idx = 0

    @property
    def mode(self) -> ModelMode:
        return ModelMode.LLM_API

    @property
    def adapter_name(self) -> str:
        return "scripted-147"

    def generate_plan(self, context: Any) -> ModelInvocationResult:
        p = self._plans[min(self._idx, len(self._plans) - 1)]
        self._idx += 1
        return ModelInvocationResult(
            plan=Plan.model_validate(p),
            model_mode=ModelMode.LLM_API,
            adapter_name="scripted-147",
            provider="scripted-147",
            model_name="scripted-147",
            is_fallback=False,
            prompt_tokens=20,
            completion_tokens=20,
            total_tokens=40,
            latency_ms=2,
        )

    def generate_structured_json(self, system_prompt: str, user_message: str) -> None:
        return None


def test_fix3_matching_tool_args_but_missing_returned_record_rejected_without_permanent_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If get_payment_record returns an unrelated payment record, provenance MUST fail
    WITHOUT falling back to unrelated evidence AND without creating a permanent HOLD_ACTION!
    """
    sm = get_sessionmaker()
    with sm() as db:
        sess, binding = _seed_ws(db)
        now = datetime.now(UTC)
        case = Case(
            id=f"CASE-{uuid.uuid4().hex[:8].upper()}",
            session_id=sess.id,
            type="PAYMENT_SYNC_ANOMALY",
            goal="Sync order",
            state=CaseState.NEW.value,
            version=1,
            owner="AUTONOMOUS_SUPERVISOR",
            source_system="COMMERCE_HTTP",
            source_binding_id=binding.id,
            created_at=now,
            updated_at=now,
        )
        db.add(case)
        db.flush()
        ev_old = Evidence(
            id=f"EVD-{uuid.uuid4().hex[:8].upper()}",
            case_id=case.id,
            session_id=sess.id,
            kind="OBSERVED_FACT",
            source="COMMERCE_HTTP",
            source_ref="ORD-OTHER-999",
            payload_json={"id": "ORD-OTHER-999", "status": "CONFIRMED"},
            observed_at=now,
            confidence=1.0,
        )
        db.add(ev_old)
        db.commit()

        class _MismatchConn:
            def get_order(self, order_id: str) -> dict[str, Any]:
                return {
                    "endpoint": f"/api/v1/service/orders/{order_id}",
                    "observed_at": now.isoformat(),
                    "data": {
                        "id": "ORD-147-EXACT",
                        "merchant_id": binding.merchant_id,
                        "status": "PENDING_PAYMENT",
                        "total_vnd": 100000,
                        "version": 1,
                    },
                }

            def get_payment(self, payment_id: str) -> dict[str, Any]:
                # Returns a DIFFERENT payment ID than proposed!
                return {
                    "endpoint": f"/api/v1/service/payments/{payment_id}",
                    "observed_at": now.isoformat(),
                    "data": {
                        "id": "PAY-UNRELATED-999",
                        "order_id": "ORD-OTHER-999",
                        "merchant_id": binding.merchant_id,
                        "status": "SUCCEEDED",
                        "amount_vnd": 100000,
                        "provider": "LOCAL_MOCK",
                    },
                }

        monkeypatch.setattr(
            "app.monitoring.service.build_connector_for_binding",
            lambda _b, **_kw: _MismatchConn(),
        )
        adapter = _ScriptedAdapter147(
            [
                {
                    "case_id": case.id,
                    "goal": "Sync",
                    "known_evidence_refs": [ev_old.id],
                    "unknowns": [],
                    "hypotheses": [{"label": "sync", "supporting_evidence_refs": [ev_old.id]}],
                    "next_step": {
                        "kind": "READ_TOOL",
                        "tool_name": "get_order",
                        "action_type": None,
                        "args": {"order_id": "ORD-147-EXACT"},
                    },
                    "decision_summary": "Read order",
                },
                {
                    "case_id": case.id,
                    "goal": "Sync",
                    "known_evidence_refs": [ev_old.id],
                    "unknowns": [],
                    "hypotheses": [{"label": "sync", "supporting_evidence_refs": [ev_old.id]}],
                    "next_step": {
                        "kind": "READ_TOOL",
                        "tool_name": "get_payment_record",
                        "action_type": None,
                        "args": {"payment_id": "PAY-147-EXACT"},
                    },
                    "decision_summary": "Read payment",
                },
                {
                    "case_id": case.id,
                    "goal": "Sync",
                    "known_evidence_refs": [ev_old.id],
                    "unknowns": [],
                    "hypotheses": [{"label": "sync", "supporting_evidence_refs": [ev_old.id]}],
                    "next_step": {
                        "kind": "PROPOSE_ACTION",
                        "tool_name": None,
                        "action_type": "propose_order_sync",
                        "args": {
                            "order_id": "ORD-147-EXACT",
                            "payment_id": "PAY-147-EXACT",
                            "expected_order_version": 1,
                        },
                    },
                    "decision_summary": "Propose",
                },
            ]
        )
        rt = BoundedAgentRuntime(db, adapter=adapter, max_steps=4)
        res = rt.investigate_case(case_id=case.id, session_id=sess.id)
        assert res.stop_reason == "UNVERIFIED_PAYMENT_BLOCKED"
        assert res.proposed_action_ids == []
        # No permanent HOLD_ACTION created for missing provenance
        assert res.risk_signal_ids == []
        assert SupervisorJob is not None
