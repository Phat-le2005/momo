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
            from tests.helpers_recovery import mock_receiver_response

            return httpx.Response(
                200,
                json=mock_receiver_response(
                    request, status="SYNCED", extra={"order_status": "CONFIRMED"}
                ),
            )
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
        target_order_id="ORD-PROP-01",
        target_payment_id="PAY-PROP-01",
        expected_version=1,
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
        target_order_id="ORD-PROP-01",
        target_payment_id="PAY-PROP-01",
        expected_version=1,
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
        target_order_id="ORD-PROP-01",
        target_payment_id="PAY-PROP-01",
        expected_version=1,
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


def test_prompt14_3_proposal_fencing_rejects_stale_run_expired_superseded_and_wrong_target(
    db: Session, active_session: dict[str, str]
) -> None:
    """Prompt 14.3 Fix 2: Prove proposals are strictly fenced to current run, active job,
    exact anomaly/order/payment target, non-expired lifecycle, and exact expected_version.
    """
    from app.models.actions import ActionProposal
    from app.models.supervisor import SupervisorJob
    from app.supervisor.service import _run_commerce_post_investigation_recovery

    sid = active_session["id"]
    now = datetime.now(UTC)

    binding = CommerceSourceBinding(
        id=f"BIND-FENCE-{uuid.uuid4().hex[:8].upper()}",
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

    case_obj = Case(
        id=f"CASE-FENCE-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        type="PAYMENT_SYNC",
        goal="Fencing test",
        state=CaseState.INVESTIGATING.value,
        owner="autonomous-monitor",
        source_system="COMMERCE_HTTP",
        source_binding_id=binding.id,
        idempotency_key=f"IDEMP-FENCE-{uuid.uuid4().hex[:8]}",
    )
    db.add(case_obj)
    db.flush()

    ev = Evidence(
        id=f"EVD-FENCE-{uuid.uuid4().hex[:8].upper()}",
        case_id=case_obj.id,
        session_id=sid,
        kind=EvidenceKind.OBSERVED_FACT.value,
        source="MINI_STORE_COMMERCE_HTTP",
        source_ref="/api/v1/service/orders/ORD-FENCE-01",
        payload_json={"order_id": "ORD-FENCE-01"},
        observed_at=now,
    )
    db.add(ev)

    obs = AnomalyObservation(
        id=f"ANOM-FENCE-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        source_binding_id=binding.id,
        merchant_id="MERCHANT_FINSHOP_01",
        detector_code="D01",
        anomaly_type="PAYMENT_SUCCEEDED_ORDER_NOT_CONFIRMED",
        severity="HIGH",
        fingerprint=f"fp-fence-{uuid.uuid4().hex[:8]}",
        affected_order_id="ORD-FENCE-01",
        affected_payment_id="PAY-FENCE-01",
        linked_case_id=case_obj.id,
        rationale="Payment succeeded while order still PENDING_PAYMENT",
        lifecycle_state="INVESTIGATING",
    )
    db.add(obs)

    superseded_job = SupervisorJob(
        id=f"JOB-SUPER-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        job_type="ANOMALY_INVESTIGATION",
        dedupe_key=f"dedupe-super-{uuid.uuid4().hex[:8]}",
        target_case_id=case_obj.id,
        status="SUPERSEDED",
    )
    db.add(superseded_job)

    # An older proposal from 10 minutes ago (earlier run) and a REJECTED proposal
    old_prop = ActionProposal(
        id=f"AP-OLD-{uuid.uuid4().hex[:8].upper()}",
        case_id=case_obj.id,
        session_id=sid,
        action_type="REQUEST_ORDER_SYNC",
        params_json={"order_id": "ORD-FENCE-01", "expected_version": 1},
        expected_version=1,
        status="PROPOSED",
        created_at=now - timedelta(minutes=10),
    )
    rejected_prop = ActionProposal(
        id=f"AP-REJ-{uuid.uuid4().hex[:8].upper()}",
        case_id=case_obj.id,
        session_id=sid,
        action_type="REQUEST_ORDER_SYNC",
        params_json={"order_id": "ORD-FENCE-01", "expected_version": 1},
        expected_version=1,
        status="REJECTED",
        created_at=now + timedelta(seconds=1),
    )
    db.add_all([old_prop, rejected_prop])
    db.commit()

    exec_calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/timeline"):
            return httpx.Response(200, json=[])
        if request.url.path.startswith("/api/v1/service/orders/"):
            return httpx.Response(
                200,
                json={
                    "id": "ORD-FENCE-01",
                    "status": "PENDING_PAYMENT",
                    "version": 3,
                    "total_vnd": 100000,
                },
            )
        if request.url.path.startswith("/api/v1/service/payments/"):
            return httpx.Response(
                200,
                json={
                    "id": "PAY-FENCE-01",
                    "order_id": "ORD-FENCE-01",
                    "provider": "LOCAL_MOCK",
                    "status": "SUCCEEDED",
                    "is_synced_to_order": False,
                    "amount_vnd": 100000,
                    "confirmed_at": (now - timedelta(minutes=3)).isoformat(),
                },
            )
        if request.method == "POST" and request.url.path == "/api/v1/service/recovery/execute":
            exec_calls.append("EXECUTED")
            from tests.helpers_recovery import mock_receiver_response

            return httpx.Response(
                200,
                json=mock_receiver_response(
                    request, status="SYNCED", extra={"order_status": "CONFIRMED"}
                ),
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)

    # 1. Post-investigation recovery with run_started_at=now MUST ignore old_prop & rejected_prop
    msg = _run_commerce_post_investigation_recovery(
        db,
        case_id=case_obj.id,
        session_id=sid,
        run_id="RUN-CURR-01",
        run_started_at=now,
        is_fallback_mode=False,
        anomaly_id=obs.id,
        commerce_transport=transport,
    )
    assert msg is not None and "Escalated safely" in msg
    assert len(exec_calls) == 0

    # 2. Proposal with mismatched run_id -> 409 REJECTED
    with pytest.raises(AppError) as exc_run:
        validate_and_execute_agent_proposal(
            db,
            proposal=StructuredAgentRecoveryProposal(
                case_id=case_obj.id,
                run_id="RUN-OLD-99",
                anomaly_id=obs.id,
                supporting_evidence_ids=[ev.id],
                proposed_command="REQUEST_ORDER_SYNC",
                action_params={"expected_version": 3},
            ),
            binding=binding,
            obs=obs,
            expected_run_id="RUN-CURR-01",
            transport=transport,
        )
    assert exc_run.value.status_code == 403
    assert "PROPOSAL_RUN_MISMATCH" in exc_run.value.message

    # 3. Expired proposal -> 403 REJECTED
    with pytest.raises(AppError) as exc_exp:
        validate_and_execute_agent_proposal(
            db,
            proposal=StructuredAgentRecoveryProposal(
                case_id=case_obj.id,
                run_id="RUN-CURR-01",
                anomaly_id=obs.id,
                supporting_evidence_ids=[ev.id],
                proposed_command="REQUEST_ORDER_SYNC",
                expires_at=now - timedelta(seconds=5),
            ),
            binding=binding,
            obs=obs,
            expected_run_id="RUN-CURR-01",
            transport=transport,
        )
    assert exc_exp.value.status_code == 403
    assert "PROPOSAL_EXPIRED" in exc_exp.value.message

    # 4. Superseded Supervisor job -> 409 REJECTED
    with pytest.raises(AppError) as exc_job:
        validate_and_execute_agent_proposal(
            db,
            proposal=StructuredAgentRecoveryProposal(
                case_id=case_obj.id,
                run_id="RUN-CURR-01",
                supervisor_job_id=superseded_job.id,
                anomaly_id=obs.id,
                supporting_evidence_ids=[ev.id],
                proposed_command="REQUEST_ORDER_SYNC",
            ),
            binding=binding,
            obs=obs,
            expected_run_id="RUN-CURR-01",
            expected_job_id=superseded_job.id,
            transport=transport,
        )
    assert exc_job.value.status_code == 409
    assert "SUPERSEDED_SUPERVISOR_JOB" in exc_job.value.message

    # 5. Wrong target_order_id -> 403 REJECTED
    with pytest.raises(AppError) as exc_ord:
        validate_and_execute_agent_proposal(
            db,
            proposal=StructuredAgentRecoveryProposal(
                case_id=case_obj.id,
                run_id="RUN-CURR-01",
                anomaly_id=obs.id,
                target_order_id="ORD-WRONG-99",
                supporting_evidence_ids=[ev.id],
                proposed_command="REQUEST_ORDER_SYNC",
            ),
            binding=binding,
            obs=obs,
            expected_run_id="RUN-CURR-01",
            transport=transport,
        )
    assert exc_ord.value.status_code == 403
    assert "PROPOSAL_TARGET_MISMATCH" in exc_ord.value.message

    # 6. Stale expected_version (1 != authoritative 3) -> 409 REJECTED
    with pytest.raises(AppError) as exc_ver:
        validate_and_execute_agent_proposal(
            db,
            proposal=StructuredAgentRecoveryProposal(
                case_id=case_obj.id,
                run_id="RUN-CURR-01",
                anomaly_id=obs.id,
                target_order_id="ORD-FENCE-01",
                target_payment_id="PAY-FENCE-01",
                expected_version=1,
                supporting_evidence_ids=[ev.id],
                proposed_command="REQUEST_ORDER_SYNC",
                action_params={"expected_version": 1},
            ),
            binding=binding,
            obs=obs,
            expected_run_id="RUN-CURR-01",
            transport=transport,
        )
    assert exc_ver.value.status_code == 409
    assert "PROPOSAL_STALE_VERSION" in exc_ver.value.message
    assert len(exec_calls) == 0
