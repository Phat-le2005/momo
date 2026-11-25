"""Prompt 14.2 Fix 3 Tests: Validated Structured AI Recovery Proposals & Non-Override Invariant."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.models.cases import Case, Evidence
from app.models.enums import CaseState, EvidenceKind
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.monitoring.recovery_planner import (
    StructuredAgentRecoveryProposal,
    validate_and_execute_agent_proposal,
)


@pytest.fixture
def db(clean_database: None) -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def test_agent_proposal_respects_escalation_rejects_bad_evidence_and_executes_valid_proposal(
    db: Session, active_session: dict[str, str]
) -> None:
    """Prove runtime consumes actual Agent proposal and NEVER silently overrides it."""
    sid = active_session["id"]
    now = datetime.now(UTC)

    binding = CommerceSourceBinding(
        id=f"BIND-PROP-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        base_url="http://127.0.0.1:8100",
        merchant_id="MERCHANT_FINSHOP_01",
        commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
        commerce_world_epoch=1,
        is_enabled=True,
    )
    db.add(binding)
    db.flush()

    case_a = Case(
        id=f"CASE-A-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        type="PAYMENT_SYNC",
        goal="Investigate order sync",
        state=CaseState.INVESTIGATING.value,
        owner="autonomous-monitor",
        source_system="COMMERCE_HTTP",
        source_binding_id=binding.id,
        idempotency_key=f"IDEMP-CA-{uuid.uuid4().hex[:8]}",
    )
    case_b = Case(
        id=f"CASE-B-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        type="PAYMENT_SYNC",
        goal="Other unrelated case",
        state=CaseState.INVESTIGATING.value,
        owner="autonomous-monitor",
        source_system="COMMERCE_HTTP",
        source_binding_id=binding.id,
        idempotency_key=f"IDEMP-CB-{uuid.uuid4().hex[:8]}",
    )
    db.add_all([case_a, case_b])
    db.flush()

    ev_a = Evidence(
        id=f"EVD-A-{uuid.uuid4().hex[:8].upper()}",
        case_id=case_a.id,
        session_id=sid,
        kind=EvidenceKind.OBSERVED_FACT.value,
        source="MINI_STORE_COMMERCE_HTTP",
        source_ref="/api/v1/service/orders/ORD-PROP-01",
        payload_json={"order_id": "ORD-PROP-01"},
        observed_at=now,
    )
    ev_b = Evidence(
        id=f"EVD-B-{uuid.uuid4().hex[:8].upper()}",
        case_id=case_b.id,
        session_id=sid,
        kind=EvidenceKind.OBSERVED_FACT.value,
        source="MINI_STORE_COMMERCE_HTTP",
        source_ref="/api/v1/service/orders/ORD-OTHER",
        payload_json={"order_id": "ORD-OTHER"},
        observed_at=now,
    )
    ev_claim = Evidence(
        id=f"EVD-CLM-{uuid.uuid4().hex[:8].upper()}",
        case_id=case_a.id,
        session_id=sid,
        kind=EvidenceKind.CLAIM.value,
        source="CUSTOMER_UPLOAD",
        source_ref="receipt.png",
        payload_json={"claimed_amount_vnd": 250000},
        observed_at=now,
    )
    db.add_all([ev_a, ev_b, ev_claim])

    obs = AnomalyObservation(
        id=f"ANOM-PROP-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        source_binding_id=binding.id,
        merchant_id="MERCHANT_FINSHOP_01",
        detector_code="D01",
        anomaly_type="PAYMENT_SUCCEEDED_ORDER_NOT_CONFIRMED",
        severity="HIGH",
        fingerprint=f"fp-prop-{uuid.uuid4().hex[:8]}",
        affected_order_id="ORD-PROP-01",
        affected_payment_id="PAY-PROP-01",
        linked_case_id=case_a.id,
        rationale="Payment succeeded while order still PENDING_PAYMENT",
        lifecycle_state="INVESTIGATING",
    )
    db.add(obs)
    db.commit()

    order_state: dict[str, Any] = {
        "id": "ORD-PROP-01",
        "status": "PENDING_PAYMENT",
        "version": 1,
        "total_vnd": 250000,
    }
    payment_state: dict[str, Any] = {
        "id": "PAY-PROP-01",
        "payment_id": "PAY-PROP-01",
        "order_id": "ORD-PROP-01",
        "provider": "LOCAL_MOCK",
        "status": "SUCCEEDED",
        "is_synced_to_order": False,
        "amount_vnd": 250000,
        "confirmed_at": (now - timedelta(minutes=3)).isoformat(),
    }
    exec_calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/timeline"):
            return httpx.Response(200, json=[])
        if request.url.path.startswith("/api/v1/service/orders/"):
            return httpx.Response(200, json=dict(order_state))
        if request.url.path.startswith("/api/v1/service/payments/"):
            return httpx.Response(200, json=dict(payment_state))
        if request.method == "POST" and request.url.path == "/api/v1/service/recovery/execute":
            exec_calls.append(request.headers.get("x-command-authorization", ""))
            order_state["status"] = "CONFIRMED"
            order_state["version"] = 2
            payment_state["is_synced_to_order"] = True
            return httpx.Response(200, json={"status": "SYNCED", "order_status": "CONFIRMED"})
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)

    # 1. Agent recommends REQUEST_MANUAL_REVIEW -> MUST NOT silently execute ORDER_SYNC!
    prop_wait = StructuredAgentRecoveryProposal(
        case_id=case_a.id,
        run_id="RUN-WAIT-01",
        supporting_evidence_ids=[ev_a.id],
        proposed_command="REQUEST_MANUAL_REVIEW",
        uncertainty_summary="Agent prefers operator review before mutation",
        proposal_source="AGENT_STRUCTURED_PROPOSAL",
    )
    _, res_wait = validate_and_execute_agent_proposal(
        db, proposal=prop_wait, binding=binding, obs=obs, transport=transport
    )
    assert res_wait is None
    assert len(exec_calls) == 0
    assert order_state["status"] == "PENDING_PAYMENT"

    # 2. Proposal citing evidence from UNRELATED Case B -> REJECTED (403)
    prop_bad_ev = StructuredAgentRecoveryProposal(
        case_id=case_a.id,
        run_id="RUN-BADEV-01",
        supporting_evidence_ids=[ev_b.id],
        proposed_command="REQUEST_ORDER_SYNC",
        action_params={"expected_version": 1},
    )
    with pytest.raises(AppError) as exc_badev:
        validate_and_execute_agent_proposal(
            db, proposal=prop_bad_ev, binding=binding, obs=obs, transport=transport
        )
    assert exc_badev.value.status_code == 403
    assert "INVALID_OR_UNRELATED_EVIDENCE_REFS" in exc_badev.value.message
    assert len(exec_calls) == 0

    # 3. Proposal citing untrusted CLAIM evidence instead of OBSERVED_FACT -> REJECTED (403)
    prop_claim_only = StructuredAgentRecoveryProposal(
        case_id=case_a.id,
        run_id="RUN-CLAIM-01",
        supporting_evidence_ids=[ev_claim.id],
        proposed_command="REQUEST_ORDER_SYNC",
        action_params={"expected_version": 1},
    )
    with pytest.raises(AppError) as exc_claim:
        validate_and_execute_agent_proposal(
            db, proposal=prop_claim_only, binding=binding, obs=obs, transport=transport
        )
    assert exc_claim.value.status_code == 403
    assert len(exec_calls) == 0

    # 4. Valid Agent proposal with OBSERVED_FACT -> PolicyEngine signs -> executes & verifies!
    prop_valid = StructuredAgentRecoveryProposal(
        case_id=case_a.id,
        run_id="RUN-VALID-01",
        supporting_evidence_ids=[ev_a.id],
        proposed_command="REQUEST_ORDER_SYNC",
        action_params={"expected_version": 1},
        policy_references=["POL-SYNC-001@2.0"],
        proposal_source="AGENT_STRUCTURED_PROPOSAL",
    )
    _, res_valid = validate_and_execute_agent_proposal(
        db, proposal=prop_valid, binding=binding, obs=obs, transport=transport
    )
    assert res_valid is not None
    assert res_valid.executed is True
    assert res_valid.verified_recovery is True
    assert len(exec_calls) == 1
    assert "." in exec_calls[0]
