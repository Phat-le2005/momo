"""Prompt 14.4 Fix 2 Tests: Exact Investigation Provenance, Claim-Token & Lease Fencing."""

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
from app.models.actions import ActionProposal
from app.models.cases import Case, Evidence
from app.models.enums import CaseState, EvidenceKind
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.supervisor import SupervisorJob
from app.monitoring.recovery_planner import (
    StructuredAgentRecoveryProposal,
    validate_and_execute_agent_proposal,
)
from app.supervisor.service import _run_commerce_post_investigation_recovery


@pytest.fixture
def db(clean_database: None) -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def test_exact_investigation_run_provenance_and_worker_claim_token_fencing(
    db: Session, active_session: dict[str, str]
) -> None:
    """Prove Supervisor NEVER selects a proposal by timestamp alone:
    - Proposal from another investigation with NEWER created_at -> IGNORED!
    - Proposal with NULL investigation_run_id -> IGNORED!
    - Wrong worker claim_token -> 409 INVALID_WORKER_CLAIM_TOKEN!
    - Expired worker lease -> 409 EXPIRED_WORKER_LEASE!
    - Changed anomaly occurrence -> 409 ANOMALY_OCCURRENCE_MISMATCH!
    - Valid current-run proposal with matching run_id, claim_token, active lease -> EXECUTES!
    """
    sid = active_session["id"]
    now = datetime.now(UTC)

    binding = CommerceSourceBinding(
        id=f"BIND-PROV-{uuid.uuid4().hex[:8].upper()}",
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
        id=f"CASE-PROV-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        type="PAYMENT_SYNC",
        goal="Provenance test",
        state=CaseState.INVESTIGATING.value,
        owner="autonomous-monitor",
        source_system="COMMERCE_HTTP",
        source_binding_id=binding.id,
        idempotency_key=f"IDEMP-PROV-{uuid.uuid4().hex[:8]}",
    )
    db.add(case_obj)
    db.flush()

    ev = Evidence(
        id=f"EVD-PROV-{uuid.uuid4().hex[:8].upper()}",
        case_id=case_obj.id,
        session_id=sid,
        kind=EvidenceKind.OBSERVED_FACT.value,
        source="MINI_STORE_COMMERCE_HTTP",
        source_ref="/api/v1/service/orders/ORD-PROV-01",
        payload_json={"order_id": "ORD-PROV-01"},
        observed_at=now,
    )
    db.add(ev)

    obs = AnomalyObservation(
        id=f"ANOM-PROV-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        source_binding_id=binding.id,
        merchant_id="MERCHANT_FINSHOP_01",
        detector_code="D01",
        anomaly_type="PAYMENT_SUCCEEDED_ORDER_NOT_CONFIRMED",
        severity="HIGH",
        fingerprint=f"fp-prov-{uuid.uuid4().hex[:8]}",
        affected_order_id="ORD-PROV-01",
        affected_payment_id="PAY-PROV-01",
        linked_case_id=case_obj.id,
        rationale="Payment succeeded while order still PENDING_PAYMENT",
        lifecycle_state="INVESTIGATING",
        occurrence_count=2,
    )
    db.add(obs)

    active_job = SupervisorJob(
        id=f"JOB-ACT-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        job_type="ANOMALY_INVESTIGATION",
        dedupe_key=f"dedupe-act-{uuid.uuid4().hex[:8]}",
        target_case_id=case_obj.id,
        status="RUNNING",
        claim_token="CLAIM-TOK-VALID-01",
        locked_until=now + timedelta(minutes=5),
    )
    expired_job = SupervisorJob(
        id=f"JOB-EXP-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        job_type="ANOMALY_INVESTIGATION",
        dedupe_key=f"dedupe-exp-{uuid.uuid4().hex[:8]}",
        target_case_id=case_obj.id,
        status="RUNNING",
        claim_token="CLAIM-TOK-EXP-01",
        locked_until=now - timedelta(seconds=10),
    )
    db.add_all([active_job, expired_job])

    # Proposal from ANOTHER investigation with NEWER created_at + proposal with NULL run_id
    other_run_newer = ActionProposal(
        id=f"AP-OTHER-{uuid.uuid4().hex[:8].upper()}",
        case_id=case_obj.id,
        session_id=sid,
        investigation_run_id="RUN-DIFFERENT-99",
        action_type="REQUEST_ORDER_SYNC",
        params_json={"order_id": "ORD-PROV-01", "expected_version": 1},
        evidence_refs=[ev.id],
        expected_version=1,
        status="PROPOSED",
        created_at=now + timedelta(seconds=30),
    )
    null_run_prop = ActionProposal(
        id=f"AP-NULL-{uuid.uuid4().hex[:8].upper()}",
        case_id=case_obj.id,
        session_id=sid,
        investigation_run_id=None,
        action_type="REQUEST_ORDER_SYNC",
        params_json={"order_id": "ORD-PROV-01", "expected_version": 1},
        evidence_refs=[ev.id],
        expected_version=1,
        status="PROPOSED",
        created_at=now + timedelta(seconds=60),
    )
    db.add_all([other_run_newer, null_run_prop])
    db.commit()

    order_state: dict[str, Any] = {
        "id": "ORD-PROV-01",
        "status": "PENDING_PAYMENT",
        "version": 1,
        "total_vnd": 120000,
    }
    payment_state: dict[str, Any] = {
        "id": "PAY-PROV-01",
        "order_id": "ORD-PROV-01",
        "provider": "LOCAL_MOCK",
        "status": "SUCCEEDED",
        "is_synced_to_order": False,
        "amount_vnd": 120000,
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
            exec_calls.append("EXECUTED")
            order_state["status"] = "CONFIRMED"
            order_state["version"] = 2
            payment_state["is_synced_to_order"] = True
            return httpx.Response(200, json={"status": "SYNCED", "order_status": "CONFIRMED"})
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)

    # 1. Newer other-run & NULL-run proposals MUST NOT execute for RUN-CURRENT-01!
    msg = _run_commerce_post_investigation_recovery(
        db,
        case_id=case_obj.id,
        session_id=sid,
        run_id="RUN-CURRENT-01",
        job_id=active_job.id,
        claim_token="CLAIM-TOK-VALID-01",
        run_started_at=now - timedelta(seconds=5),
        is_fallback_mode=False,
        anomaly_id=obs.id,
        commerce_transport=transport,
    )
    assert msg is not None and "Escalated safely" in msg
    assert len(exec_calls) == 0

    # 2. Wrong worker claim_token -> 409 INVALID_WORKER_CLAIM_TOKEN
    with pytest.raises(AppError) as exc_tok:
        validate_and_execute_agent_proposal(
            db,
            proposal=StructuredAgentRecoveryProposal(
                case_id=case_obj.id,
                run_id="RUN-CURRENT-01",
                supervisor_job_id=active_job.id,
                anomaly_id=obs.id,
                anomaly_occurrence=2,
                target_order_id="ORD-PROV-01",
                expected_version=1,
                supporting_evidence_ids=[ev.id],
                proposed_command="REQUEST_ORDER_SYNC",
            ),
            binding=binding,
            obs=obs,
            expected_run_id="RUN-CURRENT-01",
            expected_job_id=active_job.id,
            expected_claim_token="WRONG-CLAIM-TOKEN",
            expected_anomaly_occurrence=2,
            transport=transport,
        )
    assert exc_tok.value.status_code == 409
    assert "INVALID_WORKER_CLAIM_TOKEN" in exc_tok.value.message

    # 3. Expired worker lease -> 409 EXPIRED_WORKER_LEASE
    with pytest.raises(AppError) as exc_lease:
        validate_and_execute_agent_proposal(
            db,
            proposal=StructuredAgentRecoveryProposal(
                case_id=case_obj.id,
                run_id="RUN-CURRENT-01",
                supervisor_job_id=expired_job.id,
                anomaly_id=obs.id,
                anomaly_occurrence=2,
                target_order_id="ORD-PROV-01",
                expected_version=1,
                supporting_evidence_ids=[ev.id],
                proposed_command="REQUEST_ORDER_SYNC",
            ),
            binding=binding,
            obs=obs,
            expected_run_id="RUN-CURRENT-01",
            expected_job_id=expired_job.id,
            expected_claim_token="CLAIM-TOK-EXP-01",
            expected_anomaly_occurrence=2,
            transport=transport,
        )
    assert exc_lease.value.status_code == 409
    assert "EXPIRED_WORKER_LEASE" in exc_lease.value.message

    # 4. Changed anomaly occurrence (proposal=1 != current=2) -> 409 ANOMALY_OCCURRENCE_MISMATCH
    with pytest.raises(AppError) as exc_occ:
        validate_and_execute_agent_proposal(
            db,
            proposal=StructuredAgentRecoveryProposal(
                case_id=case_obj.id,
                run_id="RUN-CURRENT-01",
                supervisor_job_id=active_job.id,
                anomaly_id=obs.id,
                anomaly_occurrence=1,
                target_order_id="ORD-PROV-01",
                expected_version=1,
                supporting_evidence_ids=[ev.id],
                proposed_command="REQUEST_ORDER_SYNC",
            ),
            binding=binding,
            obs=obs,
            expected_run_id="RUN-CURRENT-01",
            expected_job_id=active_job.id,
            expected_claim_token="CLAIM-TOK-VALID-01",
            expected_anomaly_occurrence=2,
            transport=transport,
        )
    assert exc_occ.value.status_code == 409
    assert "ANOMALY_OCCURRENCE_MISMATCH" in exc_occ.value.message

    # 5. Valid current-run proposal bound to RUN-CURRENT-01 -> EXECUTES cleanly!
    valid_prop = ActionProposal(
        id=f"AP-CURR-{uuid.uuid4().hex[:8].upper()}",
        case_id=case_obj.id,
        session_id=sid,
        investigation_run_id="RUN-CURRENT-01",
        action_type="REQUEST_ORDER_SYNC",
        params_json={"order_id": "ORD-PROV-01", "payment_id": "PAY-PROV-01", "expected_version": 1},
        evidence_refs=[ev.id],
        expected_version=1,
        status="PROPOSED",
        created_at=now,
    )
    db.add(valid_prop)
    db.commit()

    msg_ok = _run_commerce_post_investigation_recovery(
        db,
        case_id=case_obj.id,
        session_id=sid,
        run_id="RUN-CURRENT-01",
        job_id=active_job.id,
        claim_token="CLAIM-TOK-VALID-01",
        is_fallback_mode=False,
        anomaly_id=obs.id,
        commerce_transport=transport,
    )
    assert msg_ok is not None and "Autonomous recovery verified" in msg_ok
    assert len(exec_calls) == 1
    db.refresh(valid_prop)
    assert valid_prop.status == "EXECUTED"


def test_prompt14_5_fail_closed_worker_fencing_rejects_missing_job_token_or_lease(
    db: Session, active_session: dict[str, str]
) -> None:
    """Prompt 14.5 Fix 2: Missing job, missing claim_token, or missing lease MUST fail closed."""
    sid = active_session["id"]
    now = datetime.now(UTC)

    binding = CommerceSourceBinding(
        id=f"BIND-FC-{uuid.uuid4().hex[:8].upper()}",
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
        id=f"CASE-FC-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        type="PAYMENT_SYNC",
        goal="Fail-closed fencing",
        state=CaseState.INVESTIGATING.value,
        owner="autonomous-monitor",
        source_system="COMMERCE_HTTP",
        source_binding_id=binding.id,
        idempotency_key=f"IDEMP-FC-{uuid.uuid4().hex[:8]}",
    )
    db.add(case_obj)
    db.flush()

    ev = Evidence(
        id=f"EVD-FC-{uuid.uuid4().hex[:8].upper()}",
        case_id=case_obj.id,
        session_id=sid,
        kind=EvidenceKind.OBSERVED_FACT.value,
        source="MINI_STORE_COMMERCE_HTTP",
        source_ref="/api/v1/service/orders/ORD-FC-01",
        payload_json={"order_id": "ORD-FC-01"},
        observed_at=now,
    )
    db.add(ev)

    obs = AnomalyObservation(
        id=f"ANOM-FC-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        source_binding_id=binding.id,
        merchant_id="MERCHANT_FINSHOP_01",
        detector_code="D01",
        anomaly_type="PAYMENT_SUCCEEDED_ORDER_NOT_CONFIRMED",
        severity="HIGH",
        fingerprint=f"fp-fc-{uuid.uuid4().hex[:8]}",
        affected_order_id="ORD-FC-01",
        affected_payment_id="PAY-FC-01",
        linked_case_id=case_obj.id,
        rationale="Unsynced payment",
        lifecycle_state="INVESTIGATING",
    )
    db.add(obs)

    no_lease_job = SupervisorJob(
        id=f"JOB-NOLS-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        job_type="ANOMALY_INVESTIGATION",
        dedupe_key=f"dedupe-nols-{uuid.uuid4().hex[:8]}",
        target_case_id=case_obj.id,
        status="RUNNING",
        claim_token="TOK-NOLS-01",
        locked_until=None,
    )
    db.add(no_lease_job)
    db.commit()

    prop = StructuredAgentRecoveryProposal(
        case_id=case_obj.id,
        run_id="RUN-FC-01",
        anomaly_id=obs.id,
        target_order_id="ORD-FC-01",
        target_payment_id="PAY-FC-01",
        expected_version=1,
        supporting_evidence_ids=[ev.id],
        proposed_command="REQUEST_ORDER_SYNC",
    )

    # 1. Non-existent job ID -> 409 MISSING_SUPERVISOR_JOB
    with pytest.raises(AppError) as e_miss:
        validate_and_execute_agent_proposal(
            db,
            proposal=prop,
            binding=binding,
            obs=obs,
            expected_run_id="RUN-FC-01",
            expected_job_id="JOB-DOES-NOT-EXIST",
            expected_claim_token="TOK-1",
            require_worker_fence=True,
        )
    assert e_miss.value.status_code == 409
    assert "MISSING_SUPERVISOR_JOB" in e_miss.value.message

    # 2. Missing claim token -> 409 MISSING_WORKER_CLAIM_TOKEN
    with pytest.raises(AppError) as e_tok:
        validate_and_execute_agent_proposal(
            db,
            proposal=prop,
            binding=binding,
            obs=obs,
            expected_run_id="RUN-FC-01",
            expected_job_id=no_lease_job.id,
            expected_claim_token=None,
            require_worker_fence=True,
        )
    assert e_tok.value.status_code == 409
    assert "MISSING_WORKER_CLAIM_TOKEN" in e_tok.value.message

    # 3. Missing lease expiration -> 409 MISSING_WORKER_LEASE
    with pytest.raises(AppError) as e_ls:
        validate_and_execute_agent_proposal(
            db,
            proposal=prop,
            binding=binding,
            obs=obs,
            expected_run_id="RUN-FC-01",
            expected_job_id=no_lease_job.id,
            expected_claim_token="TOK-NOLS-01",
            require_worker_fence=True,
        )
    assert e_ls.value.status_code == 409
    assert "MISSING_WORKER_LEASE" in e_ls.value.message

    # 4 (Prompt 14.5 Fix 3): Missing required targets & stale/unrelated evidence MUST fail closed!
    # 4a. Missing target_order_id -> 403 MISSING_REQUIRED_PROPOSAL_TARGET
    with pytest.raises(AppError) as e_noord:
        validate_and_execute_agent_proposal(
            db,
            proposal=StructuredAgentRecoveryProposal(
                case_id=case_obj.id,
                run_id="RUN-FC-01",
                anomaly_id=obs.id,
                target_order_id=None,
                target_payment_id="PAY-FC-01",
                expected_version=1,
                supporting_evidence_ids=[ev.id],
                proposed_command="REQUEST_ORDER_SYNC",
            ),
            binding=binding,
            obs=obs,
            expected_run_id="RUN-FC-01",
        )
    assert e_noord.value.status_code == 403
    assert "MISSING_REQUIRED_PROPOSAL_TARGET" in e_noord.value.message

    # 4b. Missing target_payment_id -> 403 MISSING_REQUIRED_PROPOSAL_TARGET
    with pytest.raises(AppError) as e_nopay:
        validate_and_execute_agent_proposal(
            db,
            proposal=StructuredAgentRecoveryProposal(
                case_id=case_obj.id,
                run_id="RUN-FC-01",
                anomaly_id=obs.id,
                target_order_id="ORD-FC-01",
                target_payment_id=None,
                expected_version=1,
                supporting_evidence_ids=[ev.id],
                proposed_command="REQUEST_ORDER_SYNC",
            ),
            binding=binding,
            obs=obs,
            expected_run_id="RUN-FC-01",
        )
    assert e_nopay.value.status_code == 403
    assert "MISSING_REQUIRED_PROPOSAL_TARGET" in e_nopay.value.message

    # 4c. Missing expected_version -> 403 MISSING_EXPECTED_VERSION
    with pytest.raises(AppError) as e_nover:
        validate_and_execute_agent_proposal(
            db,
            proposal=StructuredAgentRecoveryProposal(
                case_id=case_obj.id,
                run_id="RUN-FC-01",
                anomaly_id=obs.id,
                target_order_id="ORD-FC-01",
                target_payment_id="PAY-FC-01",
                expected_version=None,
                supporting_evidence_ids=[ev.id],
                proposed_command="REQUEST_ORDER_SYNC",
            ),
            binding=binding,
            obs=obs,
            expected_run_id="RUN-FC-01",
        )
    assert e_nover.value.status_code == 403
    assert "MISSING_EXPECTED_VERSION" in e_nover.value.message

    # 4d. Stale supporting evidence (> 24h old) -> 403 STALE_SUPPORTING_EVIDENCE
    ev_stale = Evidence(
        id=f"EVD-STALE-{uuid.uuid4().hex[:8].upper()}",
        case_id=case_obj.id,
        session_id=sid,
        kind=EvidenceKind.OBSERVED_FACT.value,
        source="MINI_STORE_COMMERCE_HTTP",
        source_ref="/api/v1/service/orders/ORD-FC-01",
        payload_json={"order_id": "ORD-FC-01"},
        observed_at=now - timedelta(hours=48),
    )
    ev_other_ord = Evidence(
        id=f"EVD-OTH-{uuid.uuid4().hex[:8].upper()}",
        case_id=case_obj.id,
        session_id=sid,
        kind=EvidenceKind.OBSERVED_FACT.value,
        source="MINI_STORE_COMMERCE_HTTP",
        source_ref="/api/v1/service/orders/ORD-OTHER-99",
        payload_json={"order_id": "ORD-OTHER-99"},
        observed_at=now,
    )
    db.add_all([ev_stale, ev_other_ord])
    db.commit()

    with pytest.raises(AppError) as e_stale:
        validate_and_execute_agent_proposal(
            db,
            proposal=StructuredAgentRecoveryProposal(
                case_id=case_obj.id,
                run_id="RUN-FC-01",
                anomaly_id=obs.id,
                target_order_id="ORD-FC-01",
                target_payment_id="PAY-FC-01",
                expected_version=1,
                supporting_evidence_ids=[ev_stale.id],
                proposed_command="REQUEST_ORDER_SYNC",
            ),
            binding=binding,
            obs=obs,
            expected_run_id="RUN-FC-01",
        )
    assert e_stale.value.status_code == 403
    assert "STALE_SUPPORTING_EVIDENCE" in e_stale.value.message

    # 4e. Evidence referencing unrelated order -> 403 EVIDENCE_TARGET_MISMATCH
    with pytest.raises(AppError) as e_evmis:
        validate_and_execute_agent_proposal(
            db,
            proposal=StructuredAgentRecoveryProposal(
                case_id=case_obj.id,
                run_id="RUN-FC-01",
                anomaly_id=obs.id,
                target_order_id="ORD-FC-01",
                target_payment_id="PAY-FC-01",
                expected_version=1,
                supporting_evidence_ids=[ev_other_ord.id],
                proposed_command="REQUEST_ORDER_SYNC",
            ),
            binding=binding,
            obs=obs,
            expected_run_id="RUN-FC-01",
        )
    assert e_evmis.value.status_code == 403
    assert "EVIDENCE_TARGET_MISMATCH" in e_evmis.value.message
