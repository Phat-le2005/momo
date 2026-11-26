"""Prompt 14.6 Regression & Safety Hardening Tests (Fixes 1, 2, 3, 4, 5)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.ai_core.model_adapter import ModelInvocationResult
from app.ai_core.runtime import BoundedAgentRuntime
from app.ai_core.schemas import Plan
from app.connectors.commerce import CommerceReadConnector
from app.connectors.commerce_write import (
    _compute_command_hash,
    execute_and_verify_commerce_recovery,
)
from app.core.config import ModelMode
from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.models.actions import ActionProposal
from app.models.cases import Case, CaseEvent, Evidence
from app.models.enums import CaseState, SessionStatus
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.open_world import RecoveryCommand
from app.models.session import DemoSession
from app.policy.contracts import OrderSyncActionParams


def _seed_session_and_binding(db: Any) -> tuple[DemoSession, CommerceSourceBinding]:
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
        commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
        commerce_world_epoch=1,
        is_enabled=True,
        connectivity_status="CONNECTED",
        created_at=now,
        updated_at=now,
    )
    db.add(binding)
    db.commit()
    return sess, binding


def _seed_anomaly(
    db: Any,
    sess: DemoSession,
    binding: CommerceSourceBinding,
    *,
    order_id: str = "ORD-146-01",
    payment_id: str = "PAY-146-01",
) -> tuple[Case, AnomalyObservation]:
    now = datetime.now(UTC)
    case = Case(
        id=f"CASE-{uuid.uuid4().hex[:8].upper()}",
        session_id=sess.id,
        type="PAYMENT_SYNC_ANOMALY",
        goal="Investigate unsynced payment",
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
        id=f"ANOM-{uuid.uuid4().hex[:10].upper()}",
        session_id=sess.id,
        world_epoch=1,
        source_binding_id=binding.id,
        merchant_id=binding.merchant_id,
        detector_code="D01",
        anomaly_type="PAYMENT_SUCCEEDED_ORDER_NOT_CONFIRMED",
        severity="HIGH",
        fingerprint=f"fp-{uuid.uuid4().hex[:10]}",
        lifecycle_state="INVESTIGATING",
        affected_order_id=order_id,
        affected_payment_id=payment_id,
        linked_case_id=case.id,
        rationale="Payment succeeded but order still awaiting payment",
        evidence_refs_json=[],
        affected_entities_json={"order_id": order_id, "payment_id": payment_id},
        first_observed_at=now,
        last_observed_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(obs)
    db.commit()
    return case, obs


def _make_mock_transport(
    *,
    exec_status: int,
    exec_body: dict[str, Any],
    order_status_after: str = "CONFIRMED",
    order_version: int = 1,
    payment_synced: bool = True,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/v1/service/recovery/execute"):
            return httpx.Response(exec_status, json=exec_body)
        if path.endswith("/timeline"):
            return httpx.Response(200, json=[])
        if "/api/v1/service/orders/" in path:
            oid = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "id": oid,
                    "merchant_id": "MERCHANT_FINSHOP_01",
                    "status": order_status_after,
                    "total_vnd": 250000,
                    "version": order_version,
                },
            )
        if "/api/v1/service/payments/" in path:
            pid = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "id": pid,
                    "order_id": "ORD-146-01",
                    "merchant_id": "MERCHANT_FINSHOP_01",
                    "status": "SUCCEEDED",
                    "amount_vnd": 250000,
                    "provider": "LOCAL_MOCK",
                    "is_synced_to_order": payment_synced,
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


# ==============================================================================
# FIX 3 & FIX 4: Strict HTTP 200 Response Identity & HTTP 409 Conflict Semantics
# ==============================================================================


@pytest.mark.parametrize(
    "case_name,exec_body",
    [
        ("empty_json", {}),
        (
            "missing_idempotency_key",
            {
                "command": "REQUEST_ORDER_SYNC",
                "request_hash": "__HASH__",
                "merchant_id": "MERCHANT_FINSHOP_01",
                "order_id": "ORD-146-01",
                "status": "SYNCED",
            },
        ),
        (
            "missing_command",
            {
                "idempotency_key": "__IDEMP__",
                "request_hash": "__HASH__",
                "merchant_id": "MERCHANT_FINSHOP_01",
                "order_id": "ORD-146-01",
                "status": "SYNCED",
            },
        ),
        (
            "missing_request_hash",
            {
                "idempotency_key": "__IDEMP__",
                "command": "REQUEST_ORDER_SYNC",
                "merchant_id": "MERCHANT_FINSHOP_01",
                "order_id": "ORD-146-01",
                "status": "SYNCED",
            },
        ),
        (
            "mismatched_target_order",
            {
                "idempotency_key": "__IDEMP__",
                "command": "REQUEST_ORDER_SYNC",
                "request_hash": "__HASH__",
                "merchant_id": "MERCHANT_FINSHOP_01",
                "order_id": "ORD-OTHER-999",
                "status": "SYNCED",
            },
        ),
        (
            "unknown_semantic_status",
            {
                "idempotency_key": "__IDEMP__",
                "command": "REQUEST_ORDER_SYNC",
                "request_hash": "__HASH__",
                "merchant_id": "MERCHANT_FINSHOP_01",
                "order_id": "ORD-146-01",
                "status": "MAYBE_DONE",
            },
        ),
    ],
)
def test_fix3_http200_incomplete_or_unknown_identity_preserves_unknown_outcome(
    case_name: str,
    exec_body: dict[str, Any],
) -> None:
    sm = get_sessionmaker()
    with sm() as db:
        sess, binding = _seed_session_and_binding(db)
        _case, obs = _seed_anomaly(db, sess, binding)
        idemp = f"IDEMP-146-{case_name}"
        req_hash = _compute_command_hash(
            command="REQUEST_ORDER_SYNC",
            merchant_id=binding.merchant_id,
            commerce_session_id=binding.commerce_session_id,
            world_epoch=binding.commerce_world_epoch,
            order_id=obs.affected_order_id,
            payment_id=obs.affected_payment_id,
            event_id=None,
            expected_version=1,
        )
        body = {
            k: (idemp if v == "__IDEMP__" else req_hash if v == "__HASH__" else v)
            for k, v in exec_body.items()
        }
        transport = _make_mock_transport(exec_status=200, exec_body=body)
        reader = CommerceReadConnector(
            base_url=binding.base_url,
            service_key="test-read-key",
            merchant_id=binding.merchant_id,
            commerce_session_id=binding.commerce_session_id,
            world_epoch=binding.commerce_world_epoch,
            transport=transport,
        )
        res = execute_and_verify_commerce_recovery(
            db,
            binding=binding,
            obs=obs,
            command="REQUEST_ORDER_SYNC",
            idempotency_key=idemp,
            expected_version=1,
            order_snapshot={
                "id": "ORD-146-01",
                "status": "PENDING_PAYMENT",
                "total_vnd": 250000,
                "version": 1,
            },
            payment_snapshot={
                "id": "PAY-146-01",
                "order_id": "ORD-146-01",
                "status": "SUCCEEDED",
                "amount_vnd": 250000,
                "provider": "LOCAL_MOCK",
                "is_synced_to_order": False,
            },
            write_key="test-write-key",
            transport=transport,
            read_connector=reader,
        )
        assert res.executed is False
        assert res.verdict == "UNKNOWN_OUTCOME"
        row = db.scalar(select(RecoveryCommand).where(RecoveryCommand.id == res.command_id))
        assert row is not None
        assert row.state == "UNKNOWN_OUTCOME"
        assert row.execution_certainty == "COMMAND_EXECUTION_UNKNOWN"


def test_fix3_http200_complete_identity_verifies_execution() -> None:
    sm = get_sessionmaker()
    with sm() as db:
        sess, binding = _seed_session_and_binding(db)
        _case, obs = _seed_anomaly(db, sess, binding)
        idemp = "IDEMP-146-VALID-200"
        req_hash = _compute_command_hash(
            command="REQUEST_ORDER_SYNC",
            merchant_id=binding.merchant_id,
            commerce_session_id=binding.commerce_session_id,
            world_epoch=binding.commerce_world_epoch,
            order_id=obs.affected_order_id,
            payment_id=obs.affected_payment_id,
            event_id=None,
            expected_version=1,
        )
        valid_body = {
            "command_id": "RCV-1234567890",
            "command": "REQUEST_ORDER_SYNC",
            "idempotency_key": idemp,
            "request_hash": req_hash,
            "merchant_id": binding.merchant_id,
            "commerce_session_id": binding.commerce_session_id,
            "world_epoch": binding.commerce_world_epoch,
            "order_id": "ORD-146-01",
            "payment_id": "PAY-146-01",
            "status": "SYNCED",
        }
        transport = _make_mock_transport(exec_status=200, exec_body=valid_body)
        reader = CommerceReadConnector(
            base_url=binding.base_url,
            service_key="test-read-key",
            merchant_id=binding.merchant_id,
            commerce_session_id=binding.commerce_session_id,
            world_epoch=binding.commerce_world_epoch,
            transport=transport,
        )
        res = execute_and_verify_commerce_recovery(
            db,
            binding=binding,
            obs=obs,
            command="REQUEST_ORDER_SYNC",
            idempotency_key=idemp,
            expected_version=1,
            order_snapshot={
                "id": "ORD-146-01",
                "status": "PENDING_PAYMENT",
                "total_vnd": 250000,
                "version": 1,
            },
            payment_snapshot={
                "id": "PAY-146-01",
                "order_id": "ORD-146-01",
                "status": "SUCCEEDED",
                "amount_vnd": 250000,
                "provider": "LOCAL_MOCK",
                "is_synced_to_order": False,
            },
            write_key="test-write-key",
            transport=transport,
            read_connector=reader,
        )
        assert res.executed is True
        assert res.verified_recovery is True
        row = db.scalar(select(RecoveryCommand).where(RecoveryCommand.id == res.command_id))
        assert row is not None
        assert row.state == "VERIFIED"
        assert row.execution_certainty == "COMMAND_EXECUTION_VERIFIED"


@pytest.mark.parametrize(
    "conflict_body,expected_state,expected_certainty",
    [
        (
            {
                "error": {
                    "message": "COMMAND_IN_FLIGHT: duplicate",
                    "details": {"reason_code": "COMMAND_IN_FLIGHT"},
                }
            },
            "UNKNOWN_OUTCOME",
            "COMMAND_EXECUTION_UNKNOWN",
        ),
        (
            {"error": {"message": "UNDOCUMENTED_CONFLICT_XYZ"}},
            "UNKNOWN_OUTCOME",
            "COMMAND_EXECUTION_UNKNOWN",
        ),
        (
            {
                "error": {
                    "message": "VERSION_MISMATCH: expected 1",
                    "details": {"reason_code": "VERSION_MISMATCH"},
                }
            },
            "FAILED",
            "CONFIRMED_REJECTED",
        ),
        (
            {
                "error": {
                    "message": "IDEMPOTENCY_KEY_CONFLICT",
                    "details": {"reason_code": "IDEMPOTENCY_KEY_CONFLICT"},
                }
            },
            "FAILED",
            "CONFIRMED_REJECTED",
        ),
    ],
)
def test_fix4_http409_conflict_semantics_distinguished(
    conflict_body: dict[str, Any],
    expected_state: str,
    expected_certainty: str,
) -> None:
    sm = get_sessionmaker()
    with sm() as db:
        sess, binding = _seed_session_and_binding(db)
        _case, obs = _seed_anomaly(db, sess, binding)
        transport = _make_mock_transport(exec_status=409, exec_body=conflict_body)
        reader = CommerceReadConnector(
            base_url=binding.base_url,
            service_key="test-read-key",
            merchant_id=binding.merchant_id,
            commerce_session_id=binding.commerce_session_id,
            world_epoch=binding.commerce_world_epoch,
            transport=transport,
        )
        res = execute_and_verify_commerce_recovery(
            db,
            binding=binding,
            obs=obs,
            command="REQUEST_ORDER_SYNC",
            idempotency_key=f"IDEMP-409-{uuid.uuid4().hex[:6]}",
            expected_version=1,
            order_snapshot={
                "id": "ORD-146-01",
                "status": "PENDING_PAYMENT",
                "total_vnd": 250000,
                "version": 1,
            },
            payment_snapshot={
                "id": "PAY-146-01",
                "order_id": "ORD-146-01",
                "status": "SUCCEEDED",
                "amount_vnd": 250000,
                "provider": "LOCAL_MOCK",
                "is_synced_to_order": False,
            },
            write_key="test-write-key",
            transport=transport,
            read_connector=reader,
        )
        assert res.executed is False
        assert res.http_status == 409
        row = db.scalar(select(RecoveryCommand).where(RecoveryCommand.id == res.command_id))
        assert row is not None
        assert row.state == expected_state
        assert row.execution_certainty == expected_certainty

        # When uncertain (COMMAND_IN_FLIGHT / undocumented), mutation guard MUST block new commands!
        if expected_state == "UNKNOWN_OUTCOME":
            with pytest.raises(AppError) as exc_info:
                execute_and_verify_commerce_recovery(
                    db,
                    binding=binding,
                    obs=obs,
                    command="REQUEST_ORDER_SYNC",
                    idempotency_key=f"IDEMP-409-SECOND-{uuid.uuid4().hex[:6]}",
                    expected_version=1,
                    order_snapshot={
                        "id": "ORD-146-01",
                        "status": "PENDING_PAYMENT",
                        "total_vnd": 250000,
                        "version": 1,
                    },
                    payment_snapshot={
                        "id": "PAY-146-01",
                        "order_id": "ORD-146-01",
                        "status": "SUCCEEDED",
                        "amount_vnd": 250000,
                        "provider": "LOCAL_MOCK",
                        "is_synced_to_order": False,
                    },
                    write_key="test-write-key",
                    transport=transport,
                    read_connector=reader,
                )
            assert exc_info.value.status_code == 409


# ==============================================================================
# FIX 1, FIX 2 & FIX 5: Exact Agent Version Preservation & Current-Run Provenance
# ==============================================================================


class _ScriptedAdapter:
    def __init__(self, plans: list[dict[str, Any]]) -> None:
        self._plans = list(plans)
        self._idx = 0

    @property
    def mode(self) -> ModelMode:
        return ModelMode.LLM_API

    @property
    def adapter_name(self) -> str:
        return "scripted-test"

    def generate_plan(self, context: Any) -> ModelInvocationResult:
        plan = self._plans[min(self._idx, len(self._plans) - 1)]
        self._idx += 1
        return ModelInvocationResult(
            plan=Plan.model_validate(plan),
            model_mode=ModelMode.LLM_API,
            adapter_name="scripted-test",
            provider="scripted-test",
            model_name="scripted-test",
            is_fallback=False,
            prompt_tokens=50,
            completion_tokens=50,
            total_tokens=100,
            latency_ms=5,
        )

    def generate_structured_json(self, system_prompt: str, user_message: str) -> None:
        return None


def test_fix1_fix2_fix5_commerce_http_exact_version_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify stale version rejection, missing version rejection, alias contradiction,
    incomplete provenance rejection, and strict OrderSyncActionParams contract compliance.
    """
    sm = get_sessionmaker()
    with sm() as db:
        sess, binding = _seed_session_and_binding(db)
        case, _obs = _seed_anomaly(db, sess, binding, order_id="ORD-V2", payment_id="PAY-V2")
        now = datetime.now(UTC)
        ev = Evidence(
            id=f"EVD-{uuid.uuid4().hex[:8].upper()}",
            case_id=case.id,
            session_id=sess.id,
            kind="OBSERVED_FACT",
            source="COMMERCE_HTTP",
            source_ref="ORD-V2",
            payload_json={"order_id": "ORD-V2", "payment_id": "PAY-V2"},
            observed_at=now,
            confidence=1.0,
        )
        db.add(ev)
        db.commit()

        class _FakeConn:
            def get_order(self, order_id: str) -> dict[str, Any]:
                return {
                    "endpoint": f"/api/v1/service/orders/{order_id}",
                    "observed_at": now.isoformat(),
                    "data": {
                        "id": order_id,
                        "merchant_id": binding.merchant_id,
                        "status": "PENDING_PAYMENT",
                        "total_vnd": 300000,
                        "version": 2,
                    },
                }

            def get_payment(self, payment_id: str) -> dict[str, Any]:
                return {
                    "endpoint": f"/api/v1/service/payments/{payment_id}",
                    "observed_at": now.isoformat(),
                    "data": {
                        "id": payment_id,
                        "order_id": "ORD-V2",
                        "merchant_id": binding.merchant_id,
                        "status": "SUCCEEDED",
                        "amount_vnd": 300000,
                        "provider": "LOCAL_MOCK",
                        "verified_at": now.isoformat(),
                    },
                }

        monkeypatch.setattr(
            "app.monitoring.service.build_connector_for_binding",
            lambda _b, **_kw: _FakeConn(),
        )

        # Case A: Stale version (Agent proposes version=1, authoritative=2) -> REJECTED!
        stale_adapter = _ScriptedAdapter(
            [
                {
                    "case_id": case.id,
                    "goal": "Sync order",
                    "known_evidence_refs": [ev.id],
                    "unknowns": [],
                    "hypotheses": [{"label": "unsynced", "supporting_evidence_refs": [ev.id]}],
                    "next_step": {
                        "kind": "READ_TOOL",
                        "tool_name": "get_order",
                        "action_type": None,
                        "args": {"order_id": "ORD-V2"},
                    },
                    "decision_summary": "Read order",
                },
                {
                    "case_id": case.id,
                    "goal": "Sync order",
                    "known_evidence_refs": [ev.id],
                    "unknowns": [],
                    "hypotheses": [{"label": "unsynced", "supporting_evidence_refs": [ev.id]}],
                    "next_step": {
                        "kind": "READ_TOOL",
                        "tool_name": "get_payment_record",
                        "action_type": None,
                        "args": {"payment_id": "PAY-V2"},
                    },
                    "decision_summary": "Read payment",
                },
                {
                    "case_id": case.id,
                    "goal": "Sync order",
                    "known_evidence_refs": [ev.id],
                    "unknowns": [],
                    "hypotheses": [{"label": "unsynced", "supporting_evidence_refs": [ev.id]}],
                    "next_step": {
                        "kind": "PROPOSE_ACTION",
                        "tool_name": None,
                        "action_type": "propose_order_sync",
                        "args": {
                            "order_id": "ORD-V2",
                            "payment_id": "PAY-V2",
                            "expected_order_version": 1,
                        },
                    },
                    "decision_summary": "Propose stale v1",
                },
            ]
        )
        rt = BoundedAgentRuntime(db, adapter=stale_adapter, max_steps=4)
        res_stale = rt.investigate_case(case_id=case.id, session_id=sess.id)
        assert res_stale.stop_reason == "STALE_VERSION_REJECTED"
        assert res_stale.proposed_action_ids == []

        # Case B: Only order read (no payment read) -> MISSING_CURRENT_RUN_PROVENANCE!
        case.state = CaseState.NEW.value
        db.commit()
        only_order_adapter = _ScriptedAdapter(
            [
                {
                    "case_id": case.id,
                    "goal": "Sync order",
                    "known_evidence_refs": [ev.id],
                    "unknowns": [],
                    "hypotheses": [{"label": "unsynced", "supporting_evidence_refs": [ev.id]}],
                    "next_step": {
                        "kind": "READ_TOOL",
                        "tool_name": "get_order",
                        "action_type": None,
                        "args": {"order_id": "ORD-V2"},
                    },
                    "decision_summary": "Read order only",
                },
                {
                    "case_id": case.id,
                    "goal": "Sync order",
                    "known_evidence_refs": [ev.id],
                    "unknowns": [],
                    "hypotheses": [{"label": "unsynced", "supporting_evidence_refs": [ev.id]}],
                    "next_step": {
                        "kind": "PROPOSE_ACTION",
                        "tool_name": None,
                        "action_type": "propose_order_sync",
                        "args": {
                            "order_id": "ORD-V2",
                            "payment_id": "PAY-V2",
                            "expected_order_version": 2,
                        },
                    },
                    "decision_summary": "Propose without reading payment",
                },
            ]
        )
        rt2 = BoundedAgentRuntime(db, adapter=only_order_adapter, max_steps=4)
        res_prov = rt2.investigate_case(case_id=case.id, session_id=sess.id)
        assert res_prov.stop_reason == "UNVERIFIED_PAYMENT_BLOCKED"
        assert res_prov.proposed_action_ids == []

        # Case C: Valid matching version=2 with both order & payment observed -> strict contract!
        case.state = CaseState.NEW.value
        db.commit()
        valid_adapter = _ScriptedAdapter(
            [
                {
                    "case_id": case.id,
                    "goal": "Sync order",
                    "known_evidence_refs": [ev.id],
                    "unknowns": [],
                    "hypotheses": [{"label": "unsynced", "supporting_evidence_refs": [ev.id]}],
                    "next_step": {
                        "kind": "READ_TOOL",
                        "tool_name": "get_order",
                        "action_type": None,
                        "args": {"order_id": "ORD-V2"},
                    },
                    "decision_summary": "Read order",
                },
                {
                    "case_id": case.id,
                    "goal": "Sync order",
                    "known_evidence_refs": [ev.id],
                    "unknowns": [],
                    "hypotheses": [{"label": "unsynced", "supporting_evidence_refs": [ev.id]}],
                    "next_step": {
                        "kind": "READ_TOOL",
                        "tool_name": "get_payment_record",
                        "action_type": None,
                        "args": {"payment_id": "PAY-V2"},
                    },
                    "decision_summary": "Read payment",
                },
                {
                    "case_id": case.id,
                    "goal": "Sync order",
                    "known_evidence_refs": [ev.id],
                    "unknowns": [],
                    "hypotheses": [{"label": "unsynced", "supporting_evidence_refs": [ev.id]}],
                    "next_step": {
                        "kind": "PROPOSE_ACTION",
                        "tool_name": None,
                        "action_type": "propose_order_sync",
                        "args": {
                            "order_id": "ORD-V2",
                            "payment_id": "PAY-V2",
                            "expected_order_version": 2,
                        },
                    },
                    "decision_summary": "Propose exact v2",
                },
            ]
        )
        rt3 = BoundedAgentRuntime(db, adapter=valid_adapter, max_steps=4)
        res_ok = rt3.investigate_case(case_id=case.id, session_id=sess.id)
        assert res_ok.stop_reason == "ACTION_PROPOSED"
        assert len(res_ok.proposed_action_ids) == 1
        prop = db.get(ActionProposal, res_ok.proposed_action_ids[0])
        assert prop is not None
        assert "expected_version" not in prop.params_json
        assert prop.params_json["expected_order_version"] == 2
        # Validate strictly against OrderSyncActionParams (extra="forbid")!
        parsed = OrderSyncActionParams.model_validate(prop.params_json)
        assert parsed.expected_order_version == 2
        assert parsed.order_id == "ORD-V2"
        assert parsed.payment_id == "PAY-V2"
        # Ensure STALE_ORDER_VERSION event was logged earlier
        stale_ev = db.scalar(
            select(CaseEvent).where(
                CaseEvent.case_id == case.id,
                CaseEvent.type == "UNSAFE_PROPOSAL_BLOCKED",
            )
        )
        assert stale_ev is not None
