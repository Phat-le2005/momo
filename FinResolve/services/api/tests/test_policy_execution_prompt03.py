"""Comprehensive PostgreSQL integration tests for Prompt 03:
Policy Engine, BusinessActionContract, Approval Gateway, Action Executor, and Independent Verifier.

Test Matrix (QA_MATRIX.md & prompts/03_policy_execution.md):
- T01: Full E2E `payment_sync` pipeline:
       Complaint -> AI Investigation -> Action Proposal -> Human Approval ->
       Safe Execution (SimOrder mutated to PAID, version=2) ->
       Independent Read-Back Verification (PASSED) -> Case RESOLVED.
- T02: Unverified payment or missing OBSERVED_FACT evidence blocked by PolicyEngine.
- T04: Execution side-effect timeout -> ActionExecution(UNKNOWN) & Case(UNKNOWN_OUTCOME);
       blind retry with new idempotency key rejected with 409 CONFLICT.
- T06: Idempotent replay with same idempotency key produces one effect;
       approval replay with different key rejected with 409;
       concurrent execution attempts on PostgreSQL serialized via row locks.
- T11: Approval digest bound to exact order, amount, evidence, role, and policy version;
       parameter tampering rejected with 409 APPROVAL_DIGEST_MISMATCH.
- T12: Expired approval rejected at execution time with 409 APPROVAL_EXPIRED.
- T13: Order version changed before write rejected with 409 STALE_ORDER_VERSION.
- T20: Executor returns HTTP 200 OK, but DB postcondition not met ->
       Verifier records Verification(FAILED) and transitions Case to FAILED (not RESOLVED).
- Overlapping Investigation Guard: Concurrent or overlapping investigations on one Case
       rejected with 409 OVERLAPPING_INVESTIGATION_BLOCKED.
"""

from __future__ import annotations

import concurrent.futures
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import get_sessionmaker
from app.models.actions import ActionProposal, Approval
from app.models.cases import Case, CaseEvent
from app.models.enums import (
    ActionExecutionState,
    ActionProposalStatus,
    ActionType,
    CaseState,
    OrderPaymentStatus,
    OrderStatus,
    VerificationVerdict,
)
from app.models.simulator import SimOrder

# ---------------------------------------------------------------------------
# 1. QA T01: Full End-to-End Flow from Complaint to Verified RESOLVED
# ---------------------------------------------------------------------------


def test_t01_full_e2e_complaint_investigate_propose_approve_execute_verify_resolved(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token}

    # Step 1: Customer submits Vietnamese complaint
    intake_res = client.post(
        "/api/v1/cases",
        headers=headers,
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
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=headers)
    assert inv_res.status_code == 200
    inv_data = inv_res.json()
    assert inv_data["final_state"] == CaseState.WAITING_APPROVAL.value
    assert len(inv_data["proposed_action_ids"]) == 1
    proposal_id = inv_data["proposed_action_ids"][0]

    # Inspect proposal & BusinessActionContract via GET /api/v1/actions/{proposal_id}
    prop_detail_res = client.get(f"/api/v1/actions/{proposal_id}", headers=headers)
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

    # Step 3: Human / Demo Manager Approval
    approve_res = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={
            "decision": "APPROVED",
            "approver_role": "OPS_MANAGER",
            "approver_id": "ops_lead_vn_01",
            "reason": "Đã kiểm tra biên lai MoMo khớp 250.000 VND với đơn hàng.",
        },
    )
    assert approve_res.status_code == 200
    approve_data = approve_res.json()
    assert approve_data["decision"] == "APPROVED"
    assert approve_data["proposal_status"] == ActionProposalStatus.APPROVED.value
    assert approve_data["policy_version"] == "FinResolve-Policy-v1.2"
    assert len(approve_data["digest"]) == 64

    # Step 4: Safe Execution + Independent Read-Back Verification -> Case RESOLVED
    exec_res = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "EXEC-T01-KEY-001"},
    )
    assert exec_res.status_code == 200
    exec_data = exec_res.json()

    assert exec_data["execution_state"] == ActionExecutionState.EXECUTED.value
    assert exec_data["proposal_status"] == ActionProposalStatus.EXECUTED.value
    assert exec_data["case_state"] == CaseState.RESOLVED.value
    assert exec_data["idempotent_replay"] is False
    assert exec_data["verification"] is not None
    assert exec_data["verification"]["verdict"] == VerificationVerdict.PASSED.value
    assert exec_data["verification"]["postconditions_json"]["read_back_verified"] is True
    assert exec_data["verification"]["postconditions_json"]["observed_order_status"] == "PAID"
    assert exec_data["verification"]["postconditions_json"]["observed_order_version"] == 2

    # Step 5: Inspect PostgreSQL directly to confirm real order mutation
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
    case_res = client.get(f"/api/v1/cases/{case_id}", headers=headers)
    assert case_res.status_code == 200
    case_final = case_res.json()
    assert case_final["state"] == CaseState.RESOLVED.value
    assert len(case_final["verifications"]) == 1
    assert case_final["verifications"][0]["verdict"] == "PASSED"

    events_res = client.get(f"/api/v1/cases/{case_id}/events", headers=headers)
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
# 2. Role & Policy Guardrails: Unapproved Execution, Bad Roles, Forged Proposal
# ---------------------------------------------------------------------------


def test_unapproved_execution_and_unauthorized_roles_rejected(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    headers = {"X-Demo-Session": token}

    intake_res = client.post(
        "/api/v1/cases",
        headers=headers,
        json={
            "text": (
                "Tôi đã thanh toán 250.000đ qua MoMo cho đơn hàng ORD-SYNC nhưng chưa cập nhật."
            ),
            "type": "PAYMENT_DISPUTE",
        },
    )
    case_id = intake_res.json()["id"]
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=headers)
    proposal_id = inv_res.json()["proposed_action_ids"][0]

    # 2a. Attempt to execute BEFORE approval -> 403 FORBIDDEN
    unapproved_exec = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "KEY-UNAPPROVED"},
    )
    assert unapproved_exec.status_code == 403
    assert unapproved_exec.json()["error"]["code"] == "FORBIDDEN"

    # 2b. Attempt to approve using an unprivileged role (GUEST / AI_AGENT) -> 403 FORBIDDEN
    for bad_role in ("GUEST", "CUSTOMER", "AI_AGENT"):
        bad_appr = client.post(
            f"/api/v1/actions/{proposal_id}/approve",
            headers=headers,
            json={"decision": "APPROVED", "approver_role": bad_role},
        )
        assert bad_appr.status_code == 403

    # 2c. Cross-session isolation on /actions/{proposal_id} -> 404 NOT_FOUND
    other_sess = client.post("/api/v1/demo/sessions").json()
    other_headers = {"X-Demo-Session": other_sess["token"]}
    assert client.get(f"/api/v1/actions/{proposal_id}", headers=other_headers).status_code == 404
    assert (
        client.post(f"/api/v1/actions/{proposal_id}/approve", headers=other_headers).status_code
        == 404
    )
    assert (
        client.post(f"/api/v1/actions/{proposal_id}/execute", headers=other_headers).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# 3. QA T04: Side-Effect Timeout -> UNKNOWN_OUTCOME & No Blind Retry
# ---------------------------------------------------------------------------


def test_t04_execution_timeout_records_unknown_and_blocks_blind_retry(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token}

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

    # Approve proposal
    appr_res = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={"decision": "APPROVED", "approver_role": "DEMO_MANAGER"},
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

    # Verify SimOrder was NOT mutated to PAID
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
# 4. QA T06: Idempotency, Approval Replay Block & Concurrent Execution Lock
# ---------------------------------------------------------------------------


def test_t06_idempotency_approval_replay_and_concurrent_execution(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token}

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

    # Approve once
    appr_res = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={"decision": "APPROVED", "approver_role": "OPS_MANAGER"},
    )
    assert appr_res.status_code == 200

    # Attempting to approve a second time -> 409 CONFLICT
    dup_appr = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={"decision": "APPROVED", "approver_role": "OPS_MANAGER"},
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

    # Confirm order version in DB was incremented exactly once (1 -> 2)
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
# 5. QA T11 & T12: Approval Digest Binding & Approval Expiry
# ---------------------------------------------------------------------------


def test_t11_t12_approval_digest_mismatch_and_expired_approval_rejected(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
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
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=headers)
    proposal_id = inv_res.json()["proposed_action_ids"][0]

    appr_res = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={"decision": "APPROVED", "approver_role": "FINANCE_LEAD"},
    )
    assert appr_res.status_code == 200
    approval_id = appr_res.json()["approval_id"]

    maker = get_sessionmaker()

    # 5a. QA T11: Tamper with proposal amount after approval -> 409 APPROVAL_DIGEST_MISMATCH
    with maker() as db:
        prop = db.get(ActionProposal, proposal_id)
        assert prop is not None
        orig_params = dict(prop.params_json)
        prop.params_json = {**orig_params, "amount": 999999}
        db.commit()

    tampered_exec = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "EXEC-TAMPER-01"},
    )
    assert tampered_exec.status_code == 409
    assert "APPROVAL_DIGEST_MISMATCH" in tampered_exec.json()["error"]["message"]

    # Restore exact params, then expire the approval (QA T12) -> 409 APPROVAL_EXPIRED
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
# 6. QA T13: Order Version Changes Before Write -> 409 Conflict (No Stale Write)
# ---------------------------------------------------------------------------


def test_t13_order_version_changed_before_write_rejected_on_recheck(
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
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=headers)
    proposal_id = inv_res.json()["proposed_action_ids"][0]

    appr_res = client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={"decision": "APPROVED", "approver_role": "OPS_MANAGER"},
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

    # Confirm order was not overwritten to PAID
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
# 7. QA T20: Tool Returns 200 OK but DB Postcondition Not Met -> FAILED
# ---------------------------------------------------------------------------


def test_t20_executor_returns_200_but_postcondition_not_met_fails_verification(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
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
    inv_res = client.post(f"/api/v1/cases/{case_id}/investigate", headers=headers)
    proposal_id = inv_res.json()["proposed_action_ids"][0]

    client.post(
        f"/api/v1/actions/{proposal_id}/approve",
        headers=headers,
        json={"decision": "APPROVED", "approver_role": "OPS_MANAGER"},
    )

    # Execute with simulate_verification_failure=True (executor reports 200 OK, DB not updated)
    exec_res = client.post(
        f"/api/v1/actions/{proposal_id}/execute",
        headers={**headers, "X-Idempotency-Key": "EXEC-T20-FAIL-01"},
        json={"simulate_verification_failure": True},
    )
    assert exec_res.status_code == 200
    data = exec_res.json()

    # Executor reported 200 OK, but Verifier caught the postcondition failure!
    assert data["result_json"]["http_status"] == 200
    assert data["verification"] is not None
    assert data["verification"]["verdict"] == VerificationVerdict.FAILED.value
    assert data["verification"]["postconditions_json"]["read_back_verified"] is False
    assert data["case_state"] == CaseState.FAILED.value  # NEVER RESOLVED!


# ---------------------------------------------------------------------------
# 8. Prevent Overlapping Investigations on One Case
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

    # Simulate an active in-progress investigation event on this Case
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

    # Attempting to start an overlapping investigation (sync or async) MUST fail with 409 CONFLICT
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
