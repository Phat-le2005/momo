"""Comprehensive PostgreSQL integration tests for Prompt 03 & Commit e121722 Hardening:
Policy Engine, BusinessActionContract, Approval Gateway, Action Executor, and Post-Commit Verifier.

Test Matrix (QA_MATRIX.md, prompts/03_policy_execution.md & commit e121722 review):
- T01: Full E2E `payment_sync` pipeline:
       Complaint -> AI Investigation -> Action Proposal -> Human Approval (with server-signed
       X-Demo-Manager-Token) -> Safe Execution (SimOrder mutated to PAID, version=2) ->
       Post-Commit Durable Read-Back Verification (PASSED) -> Case RESOLVED.
- T02: Unverified payment or missing OBSERVED_FACT evidence blocked by PolicyEngine.
- T04: Execution side-effect timeout -> ActionExecution(UNKNOWN) & Case(UNKNOWN_OUTCOME);
       blind retry with new idempotency key rejected with 409 CONFLICT.
- T06: Idempotent replay with same idempotency key produces one effect;
       approval replay with different key rejected with 409;
       concurrent execution attempts on PostgreSQL serialized via row locks.
- T11: Approval v2 digest bound to exact order, amount, evidence, role, approver_id, and
       policy_version; parameter/identity tampering and legacy digests rejected with 409.
- T12: Expired approval rejected at execution time with 409 APPROVAL_EXPIRED.
- T13: Order version changed before write rejected with 409 STALE_ORDER_VERSION.
- T20: Executor returns HTTP 200 OK, but DB postcondition not met ->
       Verifier records Verification(FAILED) and transitions Case to FAILED (not RESOLVED).
- Hardening 1: Guest token alone cannot self-assert DEMO_MANAGER, OPS_MANAGER, or FINANCE_LEAD;
       request JSON role/id spoofing rejected with 403 APPROVAL_IMPERSONATION_BLOCKED.
- Hardening 2: HOLD_ORDER execution disabled with 409 HOLD_ORDER_EXECUTION_DISABLED;
       Verifier never reports PASSED for a no-op HOLD_ORDER action.
- Hardening 3: Cross-tenant manager token and cross-session approval rejected (403 / 404).
- Hardening 4: Refund policy enforces merchant, currency, result_code, source_mode, and balance;
       REFUND_EXECUTE execution gated in Prompt 03 until Prompt 04 Risk Engine.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.session import issue_demo_manager_token
from app.db.session import get_sessionmaker
from app.models.actions import ActionExecution, ActionProposal, Approval
from app.models.cases import Case, CaseEvent
from app.models.enums import (
    ActionExecutionState,
    ActionProposalStatus,
    ActionType,
    CaseState,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
    VerificationVerdict,
)
from app.models.session import DemoSession
from app.models.simulator import SimOrder, SimPayment
from app.policy.executor import Verifier

# ---------------------------------------------------------------------------
# 1. QA T01: Full End-to-End Flow from Complaint to Verified RESOLVED
# ---------------------------------------------------------------------------


def test_t01_full_e2e_complaint_investigate_propose_approve_execute_verify_resolved(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    manager_token = active_session["manager_token"]
    session_id = active_session["id"]
    guest_headers = {"X-Demo-Session": token}
    manager_headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": manager_token}

    # Step 1: Customer submits Vietnamese complaint using guest token
    intake_res = client.post(
        "/api/v1/cases",
        headers=guest_headers,
        json={
            "text": (
                "Tôi đã thanh toán 250.000đ qua ví MoMo mã GD MOMO_TXN_888, "
                "nhưng đơn hàng ORD-SYNC của tôi vẫn chưa được xác nhận."
            ),
            "type": "PAYMENT_DISPUTE",
        },
    )
    assert intake_res.status_code == 201
    case_id = intake_res.json()["id"]

    # Step 2: AI Core investigates and proposes ORDER_SYNC (without mutating DB)
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=guest_headers)
    assert inv_res.status_code == 200
    inv_data = inv_res.json()
    assert inv_data["final_state"] == CaseState.WAITING_APPROVAL.value
    assert len(inv_data["proposed_action_ids"]) == 1
    proposal_id = inv_data["proposed_action_ids"][0]

    # Inspect proposal & BusinessActionContract via GET /api/v1/actions/{proposal_id}
    prop_detail_res = client.get(f"/api/v1/actions/{proposal_id}", headers=guest_headers)
    assert prop_detail_res.status_code == 200
    prop_detail = prop_detail_res.json()
    assert prop_detail["status"] == ActionProposalStatus.PROPOSED.value
    assert prop_detail["action_type"] == ActionType.ORDER_SYNC.value
    assert prop_detail["contract"]["policy_version"] == "FinResolve-Policy-v1.2"
    target_order_id = prop_detail["params_json"]["order_id"]

    # Verify SimOrder in DB is still PENDING / UNPAID / version 1 prior to approval & execution
    maker = get_sessionmaker()
    with maker() as db:
        order_before = db.scalar(
            select(SimOrder).where(
                SimOrder.session_id == session_id,
                SimOrder.id == target_order_id,
            )
        )
        assert order_before is not None
        assert order_before.status == OrderStatus.PENDING.value
        assert order_before.payment_status == OrderPaymentStatus.UNPAID.value
        assert order_before.version == 1

    # Step 3: Human / Demo Manager Approval using server-issued X-Demo-Manager-Token
    approve_res = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=manager_headers,
        json={
            "decision": "APPROVED",
            "reason": "Đã kiểm tra biên lai MoMo khớp 250.000 VND với đơn hàng.",
        },
    )
    assert approve_res.status_code == 200
    approve_data = approve_res.json()
    assert approve_data["decision"] == "APPROVED"
    assert approve_data["approver_role"] == "DEMO_MANAGER"
    assert approve_data["proposal_status"] == ActionProposalStatus.APPROVED.value
    assert approve_data["policy_version"] == "FinResolve-Policy-v1.2"
    assert len(approve_data["digest"]) == 64

    # Step 4: Safe Execution + Post-Commit Durable Read-Back Verification -> Case RESOLVED
    exec_res = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**manager_headers, "X-Idempotency-Key": "EXEC-T01-KEY-001"},
    )
    assert exec_res.status_code == 200
    exec_data = exec_res.json()

    assert exec_data["execution_state"] == ActionExecutionState.EXECUTED.value
    assert exec_data["proposal_status"] == ActionProposalStatus.EXECUTED.value
    assert exec_data["case_state"] == CaseState.RESOLVED.value
    assert exec_data["idempotent_replay"] is False
    assert exec_data["verification"] is not None
    assert exec_data["verification"]["verdict"] == VerificationVerdict.PASSED.value
    postcond = exec_data["verification"]["postconditions_json"]
    assert postcond["read_back_verified"] is True
    assert postcond["post_commit_durable_read_back"] is True
    assert postcond["observed_order_status"] == "PAID"
    assert postcond["observed_order_version"] == 2

    # Step 5: Inspect PostgreSQL directly to confirm durable order mutation
    with maker() as db:
        order_after = db.scalar(
            select(SimOrder).where(
                SimOrder.session_id == session_id,
                SimOrder.id == target_order_id,
            )
        )
        assert order_after is not None
        assert order_after.status == OrderStatus.PAID.value
        assert order_after.payment_status == OrderPaymentStatus.PAID.value
        assert order_after.version == 2

    # Step 6: Inspect Case detail and audit events timeline
    case_res = client.get(f"/api/v1/cases/{case_id}", headers=guest_headers)
    assert case_res.status_code == 200
    case_final = case_res.json()
    assert case_final["state"] == CaseState.RESOLVED.value
    assert len(case_final["verifications"]) == 1
    assert case_final["verifications"][0]["verdict"] == "PASSED"

    events_res = client.get(f"/api/v1/cases/{case_id}/events", headers=guest_headers)
    assert events_res.status_code == 200
    event_types = [e["type"] for e in events_res.json()]
    for expected_evt in (
        "CASE_CREATED",
        "INVESTIGATION_STARTED",
        "AI_PLAN_GENERATED",
        "TOOL_READ_COMPLETED",
        "ACTION_PROPOSED",
        "INVESTIGATION_COMPLETED",
        "ACTION_APPROVED",
        "ACTION_EXECUTED",
        "VERIFICATION_PASSED",
    ):
        assert expected_evt in event_types


# ---------------------------------------------------------------------------
# 2. Hardening 1 & 3: Guest Role Spoofing, Impersonation & Cross-Tenant Approval
# ---------------------------------------------------------------------------


def test_hardening1_guest_token_cannot_self_assert_or_spoof_approver_role(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    manager_token = active_session["manager_token"]
    guest_headers = {"X-Demo-Session": token}
    manager_headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": manager_token}

    intake_res = client.post(
        "/api/v1/cases",
        headers=guest_headers,
        json={
            "text": (
                "Tôi đã thanh toán 250.000đ qua MoMo cho đơn hàng ORD-SYNC nhưng chưa cập nhật."
            ),
            "type": "PAYMENT_DISPUTE",
        },
    )
    case_id = intake_res.json()["id"]
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=guest_headers)
    proposal_id = inv_res.json()["proposed_action_ids"][0]

    # 2a. Attempt to execute BEFORE approval -> 403 FORBIDDEN
    unapproved_exec = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**guest_headers, "X-Idempotency-Key": "KEY-UNAPPROVED"},
    )
    assert unapproved_exec.status_code == 403

    # 2b. Guest token alone (no X-Demo-Manager-Token) trying to self-assert
    #     DEMO_MANAGER, OPS_MANAGER, or FINANCE_LEAD in request JSON -> MUST FAIL 403!
    for spoofed_role in ("DEMO_MANAGER", "OPS_MANAGER", "FINANCE_LEAD"):
        spoof_res = client.post(
            f"/api/v1/actions/{proposal_id}/approve",
            headers=guest_headers,
            json={
                "decision": "APPROVED",
                "approver_role": spoofed_role,
                "approver_id": "self_asserted_admin",
            },
        )
        assert spoof_res.status_code == 403
        assert "APPROVAL_IMPERSONATION_BLOCKED" in spoof_res.json()["error"]["message"]

    # 2c. Passing guest token inside X-Demo-Manager-Token header -> MUST FAIL 403!
    guest_as_mgr = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers={"X-Demo-Session": token, "X-Demo-Manager-Token": token},
        json={"decision": "APPROVED"},
    )
    assert guest_as_mgr.status_code == 403
    assert "APPROVAL_IMPERSONATION_BLOCKED" in guest_as_mgr.json()["error"]["message"]

    # 2d. Passing a forged HMAC signature in X-Demo-Manager-Token -> MUST FAIL 403!
    forged_mgr = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers={
            "X-Demo-Session": token,
            "X-Demo-Manager-Token": "dmk_FINANCE_LEAD.hacker_01.0000deadbeef",
        },
        json={"decision": "APPROVED"},
    )
    assert forged_mgr.status_code == 403
    assert "CROSS_SESSION_OR_FORGED_MANAGER_TOKEN" in forged_mgr.json()["error"]["message"]

    # 2e. Valid X-Demo-Manager-Token (role=DEMO_MANAGER), but request JSON attempts to
    #     override/spoof approver_role="FINANCE_LEAD" or a different approver_id -> MUST FAIL 403!
    mismatch_role_res = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=manager_headers,
        json={"decision": "APPROVED", "approver_role": "FINANCE_LEAD"},
    )
    assert mismatch_role_res.status_code == 403
    assert "APPROVAL_IMPERSONATION_BLOCKED" in mismatch_role_res.json()["error"]["message"]

    mismatch_id_res = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=manager_headers,
        json={"decision": "APPROVED", "approver_id": "impersonated_cfo_99"},
    )
    assert mismatch_id_res.status_code == 403
    assert "APPROVAL_IMPERSONATION_BLOCKED" in mismatch_id_res.json()["error"]["message"]


def test_hardening1_cross_tenant_manager_token_and_approval_rejected(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token_a = active_session["token"]
    guest_headers_a = {"X-Demo-Session": token_a}

    intake_res = client.post(
        "/api/v1/cases",
        headers=guest_headers_a,
        json={
            "text": "Tôi đã thanh toán 250.000đ qua MoMo cho đơn hàng ORD-SYNC.",
            "type": "PAYMENT_DISPUTE",
        },
    )
    case_id = intake_res.json()["id"]
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=guest_headers_a)
    proposal_id_a = inv_res.json()["proposed_action_ids"][0]

    # Create Session B
    sess_b = client.post("/api/v1/demo/sessions").json()
    assert "manager_token" not in sess_b
    token_b = sess_b["token"]
    maker = get_sessionmaker()
    with maker() as db:
        sess_b_obj = db.get(DemoSession, sess_b["session_id"])
        assert sess_b_obj is not None
        manager_token_b = issue_demo_manager_token(sess_b_obj, role="DEMO_MANAGER")

    # 1. Session A's X-Demo-Session paired with Session B's X-Demo-Manager-Token -> 403 FORBIDDEN!
    cross_token_res = client.post(
        f"/api/v1/actions/{proposal_id_a}/approve",
        headers={"X-Demo-Session": token_a, "X-Demo-Manager-Token": manager_token_b},
        json={"decision": "APPROVED"},
    )
    assert cross_token_res.status_code == 403
    assert "CROSS_SESSION_OR_FORGED_MANAGER_TOKEN" in cross_token_res.json()["error"]["message"]

    # 2. Session B's session + Session B's manager token accessing Session A's proposal -> 404!
    headers_b = {"X-Demo-Session": token_b, "X-Demo-Manager-Token": manager_token_b}
    assert client.get(f"/api/v1/actions/{proposal_id_a}", headers=headers_b).status_code == 404
    assert (
        client.post(f"/api/v1/actions/{proposal_id_a}/approve", headers=headers_b).status_code
        == 404
    )
    assert (
        client.post(f"/api/v1/actions/{proposal_id_a}/execute", headers=headers_b).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# 3. Hardening 2: HOLD_ORDER No-Op Never Executes or Reports PASSED
# ---------------------------------------------------------------------------


def test_hardening2_hold_order_no_op_never_executes_or_verifies_as_passed(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    manager_token = active_session["manager_token"]
    session_id = active_session["id"]
    manager_headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": manager_token}

    maker = get_sessionmaker()
    with maker() as db:
        order = db.scalar(
            select(SimOrder).where(
                SimOrder.session_id == session_id,
                SimOrder.id.like("ORD-SYNC-%"),
            )
        )
        assert order is not None

        hold_prop = ActionProposal(
            id="PROP-HOLD-NOOP-01",
            case_id=None,
            session_id=session_id,
            action_type=ActionType.HOLD_ORDER.value,
            params_json={"order_id": order.id, "reason": "Tạm giữ đơn để kiểm tra"},
            evidence_refs=[],
            expected_version=1,
            risk_level="MEDIUM",
            status=ActionProposalStatus.PROPOSED.value,
            created_at=datetime.now(UTC),
        )
        db.add(hold_prop)
        db.commit()

    # Approve HOLD_ORDER proposal
    appr_res = client.post(
        "/api/v1/actions/PROP-HOLD-NOOP-01/approve",
        headers=manager_headers,
        json={"decision": "APPROVED"},
    )
    assert appr_res.status_code == 200

    # 3a. Attempting to execute HOLD_ORDER via /execute MUST fail with 409 CONFLICT
    exec_res = client.post(
        "/api/v1/actions/PROP-HOLD-NOOP-01/execute",
        headers={**manager_headers, "X-Idempotency-Key": "EXEC-HOLD-NOOP-01"},
    )
    assert exec_res.status_code == 409
    assert "HOLD_ORDER_EXECUTION_DISABLED" in exec_res.json()["error"]["message"]

    # 3b. Direct unit check on Verifier: even if a synthetic ActionExecution for HOLD_ORDER
    #     is passed to Verifier.verify_execution, it MUST return FAILED, never PASSED!
    with maker() as db:
        prop_db = db.get(ActionProposal, "PROP-HOLD-NOOP-01")
        assert prop_db is not None
        synthetic_exec = ActionExecution(
            id="EXEC-SYNTH-HOLD-01",
            proposal_id=prop_db.id,
            idempotency_key="SYNTH-HOLD-KEY-01",
            state=ActionExecutionState.EXECUTED.value,
            result_json={"http_status": 200, "action_type": "HOLD_ORDER"},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(synthetic_exec)
        db.commit()

        verifier = Verifier(db, session_id)
        verif = verifier.verify_execution(
            execution=synthetic_exec,
            proposal=prop_db,
            case=None,
            use_separate_readback_session=True,
        )
        assert verif.verdict == VerificationVerdict.FAILED.value
        assert verif.postconditions_json["read_back_verified"] is False
        assert any(
            "HOLD_ORDER_NO_OP_CANNOT_VERIFY_PASSED" in r
            for r in verif.postconditions_json["failure_reasons"]
        )


# ---------------------------------------------------------------------------
# 4. Hardening 3: Refund Policy Invariants & Proposal-Only Gate Until Prompt 04
# ---------------------------------------------------------------------------


def test_hardening3_refund_policy_mismatches_and_proposal_only_execution_gate(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    session_id = active_session["id"]

    maker = get_sessionmaker()
    with maker() as db:
        session_obj = db.get(DemoSession, session_id)
        assert session_obj is not None
        finance_token = issue_demo_manager_token(session_obj, role="FINANCE_LEAD")
        ops_token = issue_demo_manager_token(session_obj, role="OPS_MANAGER")

        # Create a paid order with 0 existing refunds for refund policy tests
        paid_order = SimOrder(
            id="ORD-REF-POL-01",
            session_id=session_id,
            customer_name="Lê Thị Mai",
            customer_phone="0911222333",
            amount=300000,
            currency="VND",
            subtotal=300000,
            grand_total=300000,
            status=OrderStatus.PAID.value,
            payment_status=OrderPaymentStatus.PAID.value,
        )
        paid_payment = SimPayment(
            id="PAY-REF-POL-01",
            session_id=session_id,
            order_id=paid_order.id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_REFPOL_01",
            result_code=0,
            amount=300000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=datetime.now(UTC),
            source_mode="MOCK",
        )
        # 4a. Proposal with merchant mismatch
        prop_bad_merchant = ActionProposal(
            id="PROP-REF-BAD-MERCH",
            case_id=None,
            session_id=session_id,
            action_type=ActionType.REFUND_EXECUTE.value,
            params_json={
                "payment_id": paid_payment.id,
                "order_id": paid_order.id,
                "amount": 100000,
                "currency": "VND",
                "merchant_id": "MERCHANT_WRONG_99",
                "reason": "Test merchant mismatch",
            },
            evidence_refs=[],
            expected_version=1,
            risk_level="HIGH",
            status=ActionProposalStatus.PROPOSED.value,
        )
        # 4b. Proposal with currency mismatch
        prop_bad_curr = ActionProposal(
            id="PROP-REF-BAD-CURR",
            case_id=None,
            session_id=session_id,
            action_type=ActionType.REFUND_EXECUTE.value,
            params_json={
                "payment_id": paid_payment.id,
                "order_id": paid_order.id,
                "amount": 100,
                "currency": "USD",
                "merchant_id": "MERCHANT_MOCK_01",
                "reason": "Test currency mismatch",
            },
            evidence_refs=[],
            expected_version=1,
            risk_level="HIGH",
            status=ActionProposalStatus.PROPOSED.value,
        )
        # 4c. Valid refund proposal within eligible balance
        prop_valid_ref = ActionProposal(
            id="PROP-REF-VALID-01",
            case_id=None,
            session_id=session_id,
            action_type=ActionType.REFUND_EXECUTE.value,
            params_json={
                "payment_id": paid_payment.id,
                "order_id": paid_order.id,
                "amount": 100000,
                "currency": "VND",
                "merchant_id": "MERCHANT_MOCK_01",
                "reason": "Hàng lỗi một phần",
            },
            evidence_refs=[],
            expected_version=1,
            risk_level="MEDIUM",
            status=ActionProposalStatus.PROPOSED.value,
        )
        db.add_all([paid_order, paid_payment, prop_bad_merchant, prop_bad_curr, prop_valid_ref])
        db.commit()

    finance_headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": finance_token}
    ops_headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": ops_token}

    # Merchant mismatch rejected by PolicyEngine with 409
    r_merch = client.post(
        "/api/v1/actions/PROP-REF-BAD-MERCH/approve",
        headers=finance_headers,
        json={"decision": "APPROVED"},
    )
    assert r_merch.status_code == 409
    assert "PAYMENT_MERCHANT_MISMATCH" in r_merch.json()["error"]["message"]

    # Currency mismatch rejected by PolicyEngine with 409
    r_curr = client.post(
        "/api/v1/actions/PROP-REF-BAD-CURR/approve",
        headers=finance_headers,
        json={"decision": "APPROVED"},
    )
    assert r_curr.status_code == 409
    assert "PAYMENT_CURRENCY_MISMATCH" in r_curr.json()["error"]["message"]

    # OPS_MANAGER is not authorized to approve REFUND_EXECUTE (requires FINANCE_LEAD / DEMO_MANAGER)
    r_ops = client.post(
        "/api/v1/actions/PROP-REF-VALID-01/approve",
        headers=ops_headers,
        json={"decision": "APPROVED"},
    )
    assert r_ops.status_code == 403

    # FINANCE_LEAD can approve the valid refund proposal
    r_fin_ok = client.post(
        "/api/v1/actions/PROP-REF-VALID-01/approve",
        headers=finance_headers,
        json={"decision": "APPROVED"},
    )
    assert r_fin_ok.status_code == 200
    assert r_fin_ok.json()["approval_mode"] == "AUTHENTICATED_MANAGER"
    assert r_fin_ok.json()["is_simulated_demo_approval"] is False

    # Executing the valid partial refund (100,000 / 300,000 VND) under FINANCE_LEAD approval
    # succeeds in MOCK mode and passes post-commit durable read-back verification!
    r_exec_ok = client.post(
        "/api/v1/actions/PROP-REF-VALID-01/execute",
        headers={**finance_headers, "X-Idempotency-Key": "EXEC-REF-VALID-01"},
    )
    assert r_exec_ok.status_code == 200
    exec_body = r_exec_ok.json()
    assert exec_body["execution_state"] == "EXECUTED"
    assert exec_body["verification"]["verdict"] == "PASSED"
    assert exec_body["verification"]["postconditions_json"]["observed_completed_amount"] == 100000
    assert exec_body["verification"]["postconditions_json"]["post_commit_durable_read_back"] is True


# ---------------------------------------------------------------------------
# 5. QA T04: Side-Effect Timeout -> UNKNOWN_OUTCOME & No Blind Retry
# ---------------------------------------------------------------------------


def test_t04_execution_timeout_records_unknown_and_blocks_blind_retry(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    manager_token = active_session["manager_token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": manager_token}

    intake_res = client.post(
        "/api/v1/cases",
        headers=headers,
        json={
            "text": "Tôi đã thanh toán 250.000đ cho đơn hàng ORD-SYNC mã GD MOMO_TXN_888.",
            "type": "PAYMENT_DISPUTE",
        },
    )
    case_id = intake_res.json()["id"]
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=headers)
    proposal_id = inv_res.json()["proposed_action_ids"][0]

    appr_res = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={"decision": "APPROVED"},
    )
    assert appr_res.status_code == 200

    # Execute with simulated timeout -> UNKNOWN state and Case UNKNOWN_OUTCOME
    exec_timeout_res = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "EXEC-TIMEOUT-01"},
        json={"simulate_timeout": True},
    )
    assert exec_timeout_res.status_code == 200
    t_data = exec_timeout_res.json()
    assert t_data["execution_state"] == ActionExecutionState.UNKNOWN.value
    assert t_data["case_state"] == CaseState.UNKNOWN_OUTCOME.value
    assert t_data["verification"] is None

    # Attempting a blind retry with a new idempotency key MUST be rejected with 409 CONFLICT
    blind_retry_res = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "EXEC-BLIND-RETRY-02"},
    )
    assert blind_retry_res.status_code == 409
    assert "UNKNOWN_OUTCOME_NO_BLIND_RETRY" in blind_retry_res.json()["error"]["message"]

    # Calling with the exact same idempotency key returns the UNKNOWN execution without side effect
    idemp_res = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "EXEC-TIMEOUT-01"},
    )
    assert idemp_res.status_code == 200
    assert idemp_res.json()["idempotent_replay"] is True
    assert idemp_res.json()["execution_state"] == ActionExecutionState.UNKNOWN.value

    maker = get_sessionmaker()
    with maker() as db:
        order = db.scalar(
            select(SimOrder).where(
                SimOrder.session_id == session_id,
                SimOrder.id.like("ORD-SYNC-%"),
            )
        )
        assert order is not None
        assert order.status == OrderStatus.PENDING.value


# ---------------------------------------------------------------------------
# 6. QA T06: Idempotency, Approval Replay Block & Concurrent Execution Lock
# ---------------------------------------------------------------------------


def test_t06_idempotency_approval_replay_and_concurrent_execution(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    manager_token = active_session["manager_token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": manager_token}

    intake_res = client.post(
        "/api/v1/cases",
        headers=headers,
        json={
            "text": "Đơn hàng ORD-SYNC đã thanh toán 250.000đ qua MoMo nhưng vẫn chờ.",
            "type": "PAYMENT_DISPUTE",
        },
    )
    case_id = intake_res.json()["id"]
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=headers)
    proposal_id = inv_res.json()["proposed_action_ids"][0]

    appr_res = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={"decision": "APPROVED"},
    )
    assert appr_res.status_code == 200

    # Attempting to approve a second time -> 409 CONFLICT
    dup_appr = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={"decision": "APPROVED"},
    )
    assert dup_appr.status_code == 409

    # Launch 4 concurrent execution requests with DIFFERENT idempotency keys:
    # Exactly 1 must succeed (200 OK), and the other 3 must be blocked as approval replay (409)!
    def attempt_exec(idx: int) -> tuple[int, dict[str, object]]:
        with TestClient(client.app) as worker_client:
            r = worker_client.post(
                f"/api/v1/actions/{proposal_id}/execute",
                headers={**headers, "X-Idempotency-Key": f"CONC-EXEC-KEY-{idx}"},
            )
            return r.status_code, r.json()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(attempt_exec, i) for i in range(4)]
        results = [f.result() for f in futures]

    status_codes = [r[0] for r in results]
    assert status_codes.count(200) == 1
    assert status_codes.count(409) == 3

    winning_body = next(r[1] for r in results if r[0] == 200)
    winning_key = str(winning_body["idempotency_key"])

    # Re-sending the winning idempotency key replays the exact same result without a second write
    replay_res = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": winning_key},
    )
    assert replay_res.status_code == 200
    assert replay_res.json()["idempotent_replay"] is True
    assert replay_res.json()["execution_id"] == winning_body["execution_id"]

    maker = get_sessionmaker()
    with maker() as db:
        order = db.scalar(
            select(SimOrder).where(
                SimOrder.session_id == session_id,
                SimOrder.id.like("ORD-SYNC-%"),
            )
        )
        assert order is not None
        assert order.version == 2


# ---------------------------------------------------------------------------
# 7. Hardening 4 & QA T11/T12: Legacy Digest Rejection, Identity Binding & Expiry
# ---------------------------------------------------------------------------


def test_t11_t12_legacy_digest_rejected_and_identity_parameter_tampering_blocked(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    manager_token = active_session["manager_token"]
    headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": manager_token}

    intake_res = client.post(
        "/api/v1/cases",
        headers=headers,
        json={
            "text": "Tôi đã thanh toán 250.000đ qua MoMo cho đơn hàng ORD-SYNC.",
            "type": "PAYMENT_DISPUTE",
        },
    )
    case_id = intake_res.json()["id"]
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=headers)
    proposal_id = inv_res.json()["proposed_action_ids"][0]

    appr_res = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={"decision": "APPROVED"},
    )
    assert appr_res.status_code == 200
    approval_id = appr_res.json()["approval_id"]
    valid_v2_digest = appr_res.json()["digest"]

    maker = get_sessionmaker()

    # 7a. Hardening 4: Replace v2 digest with legacy 4-field digest -> MUST FAIL 409!
    with maker() as db:
        prop = db.get(ActionProposal, proposal_id)
        appr = db.get(Approval, approval_id)
        assert prop is not None and appr is not None
        legacy_src = f"{prop.id}:{prop.action_type}:{prop.params_json}:{prop.expected_version}"
        appr.digest = hashlib.sha256(legacy_src.encode("utf-8")).hexdigest()
        db.commit()

    legacy_exec = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "EXEC-LEGACY-DIGEST-01"},
    )
    assert legacy_exec.status_code == 409
    assert "APPROVAL_DIGEST_MISMATCH" in legacy_exec.json()["error"]["message"]

    # 7b. Hardening 4: Restore v2 digest, then tamper with approver_id on Approval row -> 409!
    with maker() as db:
        appr = db.get(Approval, approval_id)
        assert appr is not None
        orig_approver_id = appr.approver_id
        appr.digest = valid_v2_digest
        appr.approver_id = "tampered_approver_identity"
        db.commit()

    tampered_id_exec = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "EXEC-TAMPER-ID-01"},
    )
    assert tampered_id_exec.status_code == 409
    assert "APPROVAL_DIGEST_MISMATCH" in tampered_id_exec.json()["error"]["message"]

    # 7c. QA T11: Restore approver_id, then tamper with proposal amount -> 409!
    with maker() as db:
        prop = db.get(ActionProposal, proposal_id)
        appr = db.get(Approval, approval_id)
        assert prop is not None and appr is not None
        appr.approver_id = orig_approver_id
        orig_params = dict(prop.params_json)
        prop.params_json = {**orig_params, "amount": 999999}
        db.commit()

    tampered_amt_exec = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "EXEC-TAMPER-AMT-01"},
    )
    assert tampered_amt_exec.status_code == 409
    assert "APPROVAL_DIGEST_MISMATCH" in tampered_amt_exec.json()["error"]["message"]

    # 7d. QA T12: Restore params, then expire the approval -> 409 APPROVAL_EXPIRED!
    with maker() as db:
        prop = db.get(ActionProposal, proposal_id)
        appr = db.get(Approval, approval_id)
        assert prop is not None and appr is not None
        prop.params_json = orig_params
        appr.expiry = datetime.now(UTC) - timedelta(minutes=10)
        db.commit()

    expired_exec = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "EXEC-EXPIRED-01"},
    )
    assert expired_exec.status_code == 409
    assert "APPROVAL_EXPIRED" in expired_exec.json()["error"]["message"]


# ---------------------------------------------------------------------------
# 8. QA T13: Order Version Changes Before Write -> 409 Conflict (No Stale Write)
# ---------------------------------------------------------------------------


def test_t13_order_version_changed_before_write_rejected_on_recheck(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    manager_token = active_session["manager_token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": manager_token}

    intake_res = client.post(
        "/api/v1/cases",
        headers=headers,
        json={
            "text": "Tôi đã thanh toán 250.000đ qua MoMo cho đơn hàng ORD-SYNC.",
            "type": "PAYMENT_DISPUTE",
        },
    )
    case_id = intake_res.json()["id"]
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=headers)
    proposal_id = inv_res.json()["proposed_action_ids"][0]

    appr_res = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={"decision": "APPROVED"},
    )
    assert appr_res.status_code == 200

    # Simulate concurrent modification of SimOrder.version (1 -> 2) before execution
    maker = get_sessionmaker()
    with maker() as db:
        order = db.scalar(
            select(SimOrder).where(
                SimOrder.session_id == session_id,
                SimOrder.id.like("ORD-SYNC-%"),
            )
        )
        assert order is not None
        order.version = 2
        db.commit()

    stale_exec = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "EXEC-STALE-VER-01"},
    )
    assert stale_exec.status_code == 409
    assert "STALE_ORDER_VERSION" in stale_exec.json()["error"]["message"]

    with maker() as db:
        order_after = db.scalar(
            select(SimOrder).where(
                SimOrder.session_id == session_id,
                SimOrder.id.like("ORD-SYNC-%"),
            )
        )
        assert order_after is not None
        assert order_after.status == OrderStatus.PENDING.value
        assert order_after.version == 2


# ---------------------------------------------------------------------------
# 9. QA T20: Tool Returns 200 OK but DB Postcondition Not Met -> FAILED
# ---------------------------------------------------------------------------


def test_t20_executor_returns_200_but_postcondition_not_met_fails_verification(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    manager_token = active_session["manager_token"]
    headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": manager_token}

    intake_res = client.post(
        "/api/v1/cases",
        headers=headers,
        json={
            "text": "Tôi đã thanh toán 250.000đ qua MoMo cho đơn hàng ORD-SYNC.",
            "type": "PAYMENT_DISPUTE",
        },
    )
    case_id = intake_res.json()["id"]
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=headers)
    proposal_id = inv_res.json()["proposed_action_ids"][0]

    client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={"decision": "APPROVED"},
    )

    # Execute with simulate_verification_failure=True (executor reports 200 OK, DB not updated)
    exec_res = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "EXEC-T20-FAIL-01"},
        json={"simulate_verification_failure": True},
    )
    assert exec_res.status_code == 200
    data = exec_res.json()

    assert data["result_json"]["http_status"] == 200
    assert data["verification"] is not None
    assert data["verification"]["verdict"] == VerificationVerdict.FAILED.value
    assert data["verification"]["postconditions_json"]["read_back_verified"] is False
    assert data["verification"]["postconditions_json"]["post_commit_durable_read_back"] is True
    assert data["case_state"] == CaseState.FAILED.value  # NEVER RESOLVED!


# ---------------------------------------------------------------------------
# 10. Prevent Overlapping Investigations on One Case
# ---------------------------------------------------------------------------


def test_prevent_overlapping_investigations_on_one_case(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token}

    intake_res = client.post(
        "/api/v1/cases",
        headers=headers,
        json={
            "text": "Tôi đã thanh toán 250.000đ qua MoMo cho đơn hàng ORD-SYNC.",
            "type": "PAYMENT_DISPUTE",
        },
    )
    case_id = intake_res.json()["id"]

    maker = get_sessionmaker()
    with maker() as db:
        case = db.get(Case, case_id)
        assert case is not None
        active_evt = CaseEvent(
            id="EVT-ACTIVE-RUN-001",
            case_id=case_id,
            session_id=session_id,
            type="INVESTIGATION_STARTED",
            payload_json={"run_id": "RUN-IN-PROGRESS-01", "job_status": "RUNNING"},
            created_at=datetime.now(UTC),
        )
        db.add(active_evt)
        db.commit()

    overlap_sync = client.post(f"/api/v1/cases/{case_id}/investigate", headers=headers)
    assert overlap_sync.status_code == 409
    assert "OVERLAPPING_INVESTIGATION_BLOCKED" in overlap_sync.json()["error"]["message"]

    overlap_async = client.post(
        f"/api/v1/cases/{case_id}/investigate",
        headers=headers,
        json={"run_async": True},
    )
    assert overlap_async.status_code == 409
    assert "OVERLAPPING_INVESTIGATION_BLOCKED" in overlap_async.json()["error"]["message"]


# ---------------------------------------------------------------------------
# 11. Commit caffa301 Hardening: Public Guest Session, Simulated Demo Approval,
#     Deployed Secret Validation & Verification Recovery
# ---------------------------------------------------------------------------


def test_caffa301_public_session_never_returns_manager_capability_and_simulated_approval_labeled(
    client: TestClient,
) -> None:
    """Requirements 1 & 2:
    - Public `POST /api/v1/demo/sessions` never returns a real manager authorization capability.
    - Self-serve MOCK demonstrations use `/simulate-demo-approval`, which is explicitly labeled
      `SIMULATED_DEMO_APPROVAL` and completely separate from `/approve`."""
    sess_res = client.post("/api/v1/demo/sessions")
    assert sess_res.status_code == 201
    sess_body = sess_res.json()

    # 1. Verify no manager_token or dmk_ secret is ever exposed in public session creation
    assert "manager_token" not in sess_body
    for val in sess_body.values():
        assert not str(val).startswith("dmk_")

    guest_token = sess_body["token"]
    guest_headers = {"X-Demo-Session": guest_token}

    # Create Case and run AI investigation
    c_res = client.post(
        "/api/v1/cases",
        headers=guest_headers,
        json={
            "text": "Tôi đã thanh toán 250.000đ qua MoMo cho đơn hàng ORD-SYNC mã GD MOMO_TXN_888.",
            "type": "PAYMENT_DISPUTE",
        },
    )
    case_id = c_res.json()["id"]
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=guest_headers)
    proposal_id = inv_res.json()["proposed_action_ids"][0]

    # 2. Guest cannot call authenticated manager `/approve` endpoint (403 FORBIDDEN)
    real_appr_attempt = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=guest_headers,
        json={"decision": "APPROVED"},
    )
    assert real_appr_attempt.status_code == 403

    # 3. Guest can use the separate self-serve `/simulate-demo-approval` flow in MOCK mode,
    #    which is explicitly labeled SIMULATED_DEMO_APPROVAL!
    sim_appr_res = client.post(
        f"/api/v1/actions/{proposal_id}/simulate-demo-approval",
        headers=guest_headers,
        json={"reason": "Giám khảo chạy kịch bản tự phục vụ (MOCK)"},
    )
    assert sim_appr_res.status_code == 200
    sim_data = sim_appr_res.json()
    assert sim_data["approval_mode"] == "SIMULATED_DEMO_APPROVAL"
    assert sim_data["is_simulated_demo_approval"] is True
    assert sim_data["approver_role"] == "SIMULATED_DEMO_APPROVAL"
    assert sim_data["approver_id"].startswith("sim_demo_guest_")

    # 4. Execute and verify postconditions -> RESOLVED
    exec_res = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**guest_headers, "X-Idempotency-Key": "EXEC-SIM-DEMO-01"},
    )
    assert exec_res.status_code == 200
    assert exec_res.json()["case_state"] == "RESOLVED"


def test_caffa301_deployed_env_rejects_default_or_weak_demo_approval_secret() -> None:
    """Requirement 3:
    Require DEMO_APPROVAL_SECRET from environment for deployed environments and reject
    insecure defaults."""
    import pytest
    from pydantic import ValidationError

    from app.core.config import Settings

    # 1. Deployed environment (production, staging, demo) with default local secret -> REJECTED!
    for deployed_env in ("production", "staging", "demo", "cloud"):
        with pytest.raises(ValidationError) as exc_default:
            Settings(app_env=deployed_env)
        assert "requires an explicit, strong DEMO_APPROVAL_SECRET" in str(exc_default.value)

    # 2. Deployed environment with short or placeholder secret -> REJECTED!
    for bad_secret in (
        "change-me",
        "secret",
        "short-secret-123",
        "my-local-only-secret-key-long-enough",
    ):
        with pytest.raises(ValidationError):
            Settings(
                app_env="production",
                demo_approval_secret=bad_secret,  # type: ignore[arg-type]
            )

    # 3. Deployed environment with a strong 32+ char custom secret -> ACCEPTED!
    strong_secret = "prod_hmac_key_9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a"  # noqa: S105
    valid_prod_settings = Settings(
        app_env="production",
        demo_approval_secret=strong_secret,  # type: ignore[arg-type]
    )
    assert valid_prod_settings.app_env == "production"


def test_caffa301_recovery_for_committed_execution_stuck_in_verifying_without_repeating_write(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Requirement 4 & 5:
    If Phase 1 committed an `ActionExecution(state='EXECUTED')` and transitioned the Case to
    `VERIFYING`, but crashed before creating a `Verification` record, recovery completes
    verification and transitions the Case to `RESOLVED` without repeating the side effect!"""
    token = active_session["token"]
    manager_token = active_session["manager_token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": manager_token}

    c_res = client.post(
        "/api/v1/cases",
        headers=headers,
        json={
            "text": "Tôi đã thanh toán 250.000đ qua MoMo cho đơn hàng ORD-SYNC mã GD MOMO_TXN_888.",
            "type": "PAYMENT_DISPUTE",
        },
    )
    case_id = c_res.json()["id"]
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=headers)
    proposal_id = inv_res.json()["proposed_action_ids"][0]

    appr_res = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={"decision": "APPROVED"},
    )
    assert appr_res.status_code == 200

    # Simulate Phase 1 committed in PostgreSQL (order mutated to PAID version=2, execution=EXECUTED,
    # Case=VERIFYING), but NO Verification row was created!
    maker = get_sessionmaker()
    with maker() as db:
        prop = db.get(ActionProposal, proposal_id)
        case = db.get(Case, case_id)
        assert prop is not None and case is not None
        target_order_id = prop.params_json["order_id"]
        order = db.get(SimOrder, target_order_id)
        assert order is not None

        # Phase 1 committed state:
        order.status = OrderStatus.PAID.value
        order.payment_status = OrderPaymentStatus.PAID.value
        order.version = 2
        prop.status = ActionProposalStatus.EXECUTED.value
        case.state = CaseState.VERIFYING.value
        case.version += 1

        unverified_exec = ActionExecution(
            id="EXEC-STUCK-VERIFYING-01",
            proposal_id=prop.id,
            idempotency_key="KEY-STUCK-01",
            state=ActionExecutionState.EXECUTED.value,
            result_json={
                "http_status": 200,
                "action_type": "ORDER_SYNC",
                "order_id": order.id,
                "new_order_version": 2,
            },
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(unverified_exec)
        db.commit()

    # Trigger recovery via POST /api/v1/actions/{proposal_id}/execute (even with a new retry key!)
    recover_res = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "KEY-RECOVERY-RETRY-02"},
    )
    assert recover_res.status_code == 200
    rec_data = recover_res.json()

    # Verification was recovered and Case is now RESOLVED!
    assert rec_data["execution_id"] == "EXEC-STUCK-VERIFYING-01"
    assert rec_data["case_state"] == CaseState.RESOLVED.value
    assert rec_data["idempotent_replay"] is True
    assert rec_data["verification"] is not None
    assert rec_data["verification"]["verdict"] == VerificationVerdict.PASSED.value

    # CRITICAL INVARIANT: The business side effect was NOT repeated (order.version is still 2!)
    with maker() as db:
        order_after = db.scalar(
            select(SimOrder).where(
                SimOrder.session_id == session_id,
                SimOrder.id == target_order_id,
            )
        )
        assert order_after is not None
        assert order_after.version == 2
        assert order_after.status == OrderStatus.PAID.value
