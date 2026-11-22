"""Comprehensive PostgreSQL integration tests for Prompt 04:
Deterministic Risk Engine, Refund Safety Ledger, and Risk Center APIs.

Acceptance Matrix (prompts/04_risk_refund.md & docs/QA_MATRIX.md):
- T07: Refund already completed -> duplicate proposed refund blocked with
       CONFIRMED_POLICY_VIOLATION (HOLD_ACTION) and non-accusatory Vietnamese CaseEvent.
- T08: Refund pending / in-flight -> reconcile; do not create additional refund blindly
       (ANOMALY / HOLD_ACTION).
- T09: Refund over remaining eligible balance -> blocked safely with
       CONFIRMED_POLICY_VIOLATION (HOLD_ACTION); no negative remainder.
- T10: Legitimate partial refundable amount with valid approval -> Mock executes once,
       updates SimOrder to PARTIALLY_REFUNDED, Verifier checks durable postconditions -> RESOLVED;
       subsequent second attempt exceeding remaining balance is blocked!
- T11 & T12: Approval bound to different order/amount or expired -> blocked.
- T21: Risk signal only (possible anomaly / ambiguity) -> marks REVIEW (not HOLD_ACTION),
       never accuses customer of proven fraud.
- Unrelated eligible order: A truly unrelated eligible order in the same session is NOT falsely
       blocked by another order's refund risk signal.
- Risk Center APIs: `GET /api/v1/risks`, `POST /api/v1/risks/evaluate-refund`, and
       `POST /api/v1/risks/scenarios/seed`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app.core.config import Settings
from app.core.session import issue_demo_manager_token
from app.db.session import get_sessionmaker
from app.models.actions import ActionProposal
from app.models.cases import Case, Evidence
from app.models.enums import (
    ActionProposalStatus,
    ActionType,
    CaseState,
    EvidenceKind,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
    RiskCategory,
    RiskLevel,
    RiskResponse,
    VerificationVerdict,
)
from app.models.risk import RiskSignal
from app.models.session import DemoSession
from app.models.simulator import SimOrder, SimPayment, SimRefund


def test_prompt04_seeded_scenarios_valid_partial_refund_and_blocked_duplicate_refund(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Seed the two Prompt 04 cases via `POST /api/v1/risks/scenarios/seed`:
    1. Valid partial refund proposal (200,000 / 500,000 VND) -> manager-approved & executed once
       in MOCK -> verified PASSED -> Case RESOLVED; invalid second over-limit attempt blocked!
    2. Unsafe duplicate refund on already-refunded order -> blocked with CONFIRMED_POLICY_VIOLATION
       and HOLD_ACTION."""
    token = active_session["token"]
    session_id = active_session["id"]
    guest_headers = {"X-Demo-Session": token}

    maker = get_sessionmaker()
    with maker() as db:
        sess_obj = db.get(DemoSession, session_id)
        assert sess_obj is not None
        finance_token = issue_demo_manager_token(sess_obj, role="FINANCE_LEAD")
    finance_headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": finance_token}

    # Step 1: Seed the two Prompt 04 scenarios
    seed_res = client.post("/api/v1/risks/scenarios/seed", headers=guest_headers)
    assert seed_res.status_code == 201
    seeded = seed_res.json()

    valid_case_id = seeded["valid_partial_refund_case_id"]
    valid_prop_id = seeded["valid_partial_refund_proposal_id"]
    valid_order_id = seeded["valid_partial_refund_order_id"]
    valid_payment_id = seeded["valid_partial_refund_payment_id"]
    blocked_case_id = seeded["blocked_duplicate_refund_case_id"]

    # Step 2: Inspect Risk Center (`GET /api/v1/risks`) for the blocked duplicate refund (QA T07)
    risks_res = client.get("/api/v1/risks", headers=guest_headers)
    assert risks_res.status_code == 200
    risks_data = risks_res.json()
    assert risks_data["total_count"] >= 1
    assert risks_data["hold_action_count"] >= 1
    assert len(risks_data["rules_catalog"]) >= 6

    blocked_signal = next(
        item for item in risks_data["items"] if item["case_id"] == blocked_case_id
    )
    assert blocked_signal["category"] == RiskCategory.CONFIRMED_POLICY_VIOLATION.value
    assert blocked_signal["response"] == RiskResponse.HOLD_ACTION.value
    assert blocked_signal["rule_code"] == "ALREADY_REFUNDED_PAYMENT"
    assert "không phải kết luận gian lận" in blocked_signal["details_json"]["non_accusatory_notice"]

    # Check CaseEvent created for the risk signal on blocked_case_id
    blk_events = client.get(f"/api/v1/cases/{blocked_case_id}/events", headers=guest_headers).json()
    risk_evts = [e for e in blk_events if e["type"] == "RISK_SIGNAL_DETECTED"]
    assert len(risk_evts) == 1
    assert risk_evts[0]["payload_json"]["rule_code"] == "ALREADY_REFUNDED_PAYMENT"
    assert len(risk_evts[0]["payload_json"]["source_refs"]) >= 2

    # Step 3: Approve and execute the valid partial refund proposal (200,000 / 500,000 VND) (QA T10)
    appr_res = client.post(
        f"/api/v1/actions/{valid_prop_id}/approve",
        headers=finance_headers,
        json={"decision": "APPROVED", "reason": "Đồng ý hoàn tiền một phần 200.000 VND"},
    )
    assert appr_res.status_code == 200

    exec_res = client.post(
        f"/api/v1/actions/{valid_prop_id}/execute",
        headers={**finance_headers, "X-Idempotency-Key": "EXEC-PARTIAL-REF-01"},
    )
    assert exec_res.status_code == 200
    exec_data = exec_res.json()
    assert exec_data["execution_state"] == "EXECUTED"
    assert exec_data["case_state"] == CaseState.RESOLVED.value
    assert exec_data["verification"]["verdict"] == VerificationVerdict.PASSED.value
    assert exec_data["verification"]["postconditions_json"]["observed_completed_amount"] == 200000

    # Verify DB state: SimOrder is PARTIALLY_REFUNDED and remaining balance is 300,000 VND
    with maker() as db:
        order_db = db.get(SimOrder, valid_order_id)
        assert order_db is not None
        assert order_db.payment_status == OrderPaymentStatus.PARTIALLY_REFUNDED.value

    eval_after = client.post(
        "/api/v1/risks/evaluate-refund",
        headers=guest_headers,
        json={
            "payment_id": valid_payment_id,
            "order_id": valid_order_id,
            "requested_amount": 300000,
        },
    )
    assert eval_after.status_code == 200
    ledger_after = eval_after.json()["ledger"]
    assert ledger_after["eligible_amount"] == 500000
    assert ledger_after["settled_refunds"] == 200000
    assert ledger_after["pending_or_held_refunds"] == 0
    assert ledger_after["refundable_remainder"] == 300000
    assert eval_after.json()["allowed"] is True

    # Step 4: Attempting a second refund for 350,000 VND (exceeds 300,000 VND remainder - QA T09)
    #         MUST be blocked by RiskEngine & PolicyEngine!
    eval_over = client.post(
        "/api/v1/risks/evaluate-refund",
        headers=guest_headers,
        json={
            "payment_id": valid_payment_id,
            "order_id": valid_order_id,
            "requested_amount": 350000,
            "case_id": valid_case_id,
        },
    )
    assert eval_over.status_code == 200
    over_data = eval_over.json()
    assert over_data["allowed"] is False
    assert over_data["recommended_response"] == RiskResponse.HOLD_ACTION.value
    assert over_data["signals"][0]["category"] == RiskCategory.CONFIRMED_POLICY_VIOLATION.value
    assert over_data["signals"][0]["rule_code"] == "CUMULATIVE_REFUND_LIMIT_EXCEEDED"


def test_t08_pending_refund_not_duplicated_and_t21_ambiguity_marks_review_not_fraud(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """QA T08, T21 & Unrelated Eligible Order Check:
    - T08: When a payment has an in-flight PENDING refund, RiskEngine blocks creating a new
           overlapping refund (IN_FLIGHT_PENDING_REFUND -> HOLD_ACTION), remainder >= 0.
    - T21: When a request has an ambiguity signal only, RiskEngine marks REVIEW (never accuses
           customer of fraud) and allows human review.
    - Unrelated eligible order in the same session is NOT falsely blocked!"""
    token = active_session["token"]
    session_id = active_session["id"]
    guest_headers = {"X-Demo-Session": token}

    maker = get_sessionmaker()
    with maker() as db:
        # 1. Order with an in-flight PENDING refund (QA T08)
        pend_order = SimOrder(
            id="ORD-PEND-REF-01",
            session_id=session_id,
            customer_name="Ngô Gia Bảo",
            customer_phone="0918111222",
            amount=400000,
            currency="VND",
            subtotal=400000,
            grand_total=400000,
            status=OrderStatus.PAID.value,
            payment_status=OrderPaymentStatus.PAID.value,
        )
        pend_pay = SimPayment(
            id="PAY-PEND-REF-01",
            session_id=session_id,
            order_id=pend_order.id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_PENDREF_01",
            result_code=0,
            amount=400000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=datetime.now(UTC),
        )
        in_flight_ref = SimRefund(
            id="REF-IN-FLIGHT-01",
            session_id=session_id,
            order_id=pend_order.id,
            payment_id=pend_pay.id,
            amount=250000,
            requested_amount=250000,
            approved_amount=250000,
            completed_amount=0,
            currency="VND",
            request_id="REQ-PEND-REF-01",
            status=RefundStatus.PENDING.value,
            reason="Đang chờ cổng thanh toán xử lý hoàn tiền",
        )

        # 2. Truly unrelated eligible order in the same session
        unrel_order = SimOrder(
            id="ORD-UNREL-ELIG-01",
            session_id=session_id,
            customer_name="Trịnh Công Sơn",
            customer_phone="0919333444",
            amount=350000,
            currency="VND",
            subtotal=350000,
            grand_total=350000,
            status=OrderStatus.PAID.value,
            payment_status=OrderPaymentStatus.PAID.value,
        )
        unrel_pay = SimPayment(
            id="PAY-UNREL-ELIG-01",
            session_id=session_id,
            order_id=unrel_order.id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_UNREL_01",
            result_code=0,
            amount=350000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=datetime.now(UTC),
        )
        c_t21 = Case(
            id="CASE-T21-REVIEW-01",
            session_id=session_id,
            type="REFUND_REQUEST",
            goal="Kiểm tra tín hiệu REVIEW không quy kết gian lận",
            state=CaseState.INVESTIGATING.value,
            version=1,
        )
        db.add_all([pend_order, pend_pay, in_flight_ref, unrel_order, unrel_pay, c_t21])
        db.commit()

    # Check QA T08: Pending refund holds 250,000 VND (remainder = 150,000 VND), and new refund is
    # blocked with IN_FLIGHT_PENDING_REFUND (HOLD_ACTION)
    res_t08 = client.post(
        "/api/v1/risks/evaluate-refund",
        headers=guest_headers,
        json={
            "payment_id": "PAY-PEND-REF-01",
            "order_id": "ORD-PEND-REF-01",
            "requested_amount": 100000,
        },
    )
    assert res_t08.status_code == 200
    body_t08 = res_t08.json()
    assert body_t08["allowed"] is False
    assert body_t08["recommended_response"] == RiskResponse.HOLD_ACTION.value
    assert body_t08["ledger"]["eligible_amount"] == 400000
    assert body_t08["ledger"]["settled_refunds"] == 0
    assert body_t08["ledger"]["pending_or_held_refunds"] == 250000
    assert body_t08["ledger"]["refundable_remainder"] == 150000
    assert body_t08["signals"][0]["rule_code"] == "IN_FLIGHT_PENDING_REFUND"

    # Check duplicate request_id reuse detection (SUSPECTED_ABUSE -> HOLD_ACTION)
    res_reuse = client.post(
        "/api/v1/risks/evaluate-refund",
        headers=guest_headers,
        json={
            "payment_id": "PAY-UNREL-ELIG-01",
            "order_id": "ORD-UNREL-ELIG-01",
            "requested_amount": 100000,
            "request_id": "REQ-PEND-REF-01",  # Reused request_id!
        },
    )
    assert res_reuse.status_code == 200
    assert res_reuse.json()["allowed"] is False
    assert res_reuse.json()["signals"][0]["category"] == RiskCategory.SUSPECTED_ABUSE.value
    assert res_reuse.json()["signals"][0]["rule_code"] == "DUPLICATE_REFUND_REQUEST_ID"

    # Check QA T21: Ambiguity on eligible order marks REVIEW (not HOLD_ACTION), never accuses fraud
    res_t21 = client.post(
        "/api/v1/risks/evaluate-refund",
        headers=guest_headers,
        json={
            "payment_id": "PAY-UNREL-ELIG-01",
            "order_id": "ORD-UNREL-ELIG-01",
            "requested_amount": 150000,
            "case_id": "CASE-T21-REVIEW-01",
            "has_ambiguity": True,
        },
    )
    assert res_t21.status_code == 200
    body_t21 = res_t21.json()
    assert body_t21["allowed"] is True
    assert body_t21["recommended_response"] == RiskResponse.REVIEW.value
    assert body_t21["signals"][0]["category"] == RiskCategory.ANOMALY.value
    assert body_t21["signals"][0]["response"] == RiskResponse.REVIEW.value
    assert "không coi là bằng chứng gian lận" in body_t21["explanation_vi"]

    # Check unrelated eligible order without ambiguity: allowed=True, OBSERVE, 0 false block!
    res_unrel_clean = client.post(
        "/api/v1/risks/evaluate-refund",
        headers=guest_headers,
        json={
            "payment_id": "PAY-UNREL-ELIG-01",
            "order_id": "ORD-UNREL-ELIG-01",
            "requested_amount": 150000,
            "has_ambiguity": False,
        },
    )
    assert res_unrel_clean.status_code == 200
    assert res_unrel_clean.json()["allowed"] is True
    assert res_unrel_clean.json()["recommended_response"] == RiskResponse.OBSERVE.value
    assert len(res_unrel_clean.json()["signals"]) == 0


def test_refund_execute_rejected_under_lock_when_pending_or_unknown_refund_exists_with_balance(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Hardening 2: PolicyEngine/ActionExecutor must enforce RiskEngine safety under database lock
    immediately before REFUND_EXECUTE, rejecting any new refund when the payment has PENDING or
    UNKNOWN refunds even if numeric balance remains and /risks/evaluate-refund was never called."""
    token = active_session["token"]
    session_id = active_session["id"]
    now = datetime.now(UTC)

    maker = get_sessionmaker()
    with maker() as db:
        sess_obj = db.get(DemoSession, session_id)
        assert sess_obj is not None
        finance_token = issue_demo_manager_token(sess_obj, role="FINANCE_LEAD")

        ord_lock = SimOrder(
            id="ORD-LOCK-INFLIGHT-01",
            session_id=session_id,
            customer_name="Lê Minh Khôi",
            customer_phone="0905123456",
            amount=500000,
            currency="VND",
            subtotal=500000,
            grand_total=500000,
            status=OrderStatus.PAID.value,
            payment_status=OrderPaymentStatus.PAID.value,
        )
        pay_lock = SimPayment(
            id="PAY-LOCK-INFLIGHT-01",
            session_id=session_id,
            order_id=ord_lock.id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_LOCK_01",
            result_code=0,
            amount=500000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=now,
        )
        case_lock = Case(
            id="CASE-LOCK-INFLIGHT-01",
            session_id=session_id,
            type="REFUND_REQUEST",
            goal="Kiểm tra chặn hoàn tiền khi có lệnh PENDING/UNKNOWN dù còn số dư",
            state=CaseState.WAITING_APPROVAL.value,
            version=2,
        )
        ev_lock = Evidence(
            id="EVD-LOCK-INFLIGHT-01",
            case_id=case_lock.id,
            session_id=session_id,
            kind=EvidenceKind.OBSERVED_FACT.value,
            source="PROVIDER_LEDGER_MOCK",
            source_ref=f"sim_payments:{pay_lock.id}",
            payload_json={"payment_id": pay_lock.id, "amount": 500000, "status": "SUCCESS"},
            confidence=1.0,
            observed_at=now,
        )
        prop_lock = ActionProposal(
            id="PROP-LOCK-INFLIGHT-01",
            case_id=case_lock.id,
            session_id=session_id,
            action_type=ActionType.REFUND_EXECUTE.value,
            params_json={
                "payment_id": pay_lock.id,
                "order_id": ord_lock.id,
                "merchant_id": "MERCHANT_MOCK_01",
                "amount": 100000,  # Only 100k requested out of 500k!
                "currency": "VND",
                "reason": "Hoàn tiền một phần 100.000 VND",
            },
            evidence_refs=[ev_lock.id],
            expected_version=2,
            risk_level=RiskLevel.MEDIUM.value,
            status=ActionProposalStatus.PROPOSED.value,
            created_at=now,
        )
        db.add_all([ord_lock, pay_lock, case_lock, ev_lock, prop_lock])
        db.commit()

    finance_headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": finance_token}

    # 1. Approve proposal while no in-flight refund exists yet
    appr_res = client.post(
        "/api/v1/actions/PROP-LOCK-INFLIGHT-01/approve",
        headers=finance_headers,
        json={"decision": "APPROVED", "reason": "Phê duyệt hoàn 100.000 VND"},
    )
    assert appr_res.status_code == 200

    # 2. Before execution, an in-flight PENDING refund of 150,000 VND appears on the payment.
    #    Note: Remaining numeric balance is 500,000 - 150,000 = 350,000 VND (>= 100,000 VND!),
    #    and the frontend never called /risks/evaluate-refund.
    with maker() as db:
        inflight = SimRefund(
            id="REF-INFLIGHT-LOCK-01",
            session_id=session_id,
            order_id="ORD-LOCK-INFLIGHT-01",
            payment_id="PAY-LOCK-INFLIGHT-01",
            amount=150000,
            requested_amount=150000,
            approved_amount=150000,
            completed_amount=0,
            currency="VND",
            request_id="REQ-INFLIGHT-LOCK-01",
            status=RefundStatus.PENDING.value,
            reason="Lệnh hoàn đang chờ cổng thanh toán",
        )
        db.add(inflight)
        db.commit()

    exec_pending = client.post(
        "/api/v1/actions/PROP-LOCK-INFLIGHT-01/execute",
        headers={**finance_headers, "X-Idempotency-Key": "EXEC-INFLIGHT-PEND-01"},
    )
    assert exec_pending.status_code == 409
    assert exec_pending.json()["error"]["details"]["violation_code"] == "IN_FLIGHT_PENDING_REFUND"

    # 3. Transition the in-flight refund to UNKNOWN status (still 350,000 VND numeric balance left)
    with maker() as db:
        ref_row = db.get(SimRefund, "REF-INFLIGHT-LOCK-01")
        assert ref_row is not None
        ref_row.status = RefundStatus.UNKNOWN.value
        db.commit()

    exec_unknown = client.post(
        "/api/v1/actions/PROP-LOCK-INFLIGHT-01/execute",
        headers={**finance_headers, "X-Idempotency-Key": "EXEC-INFLIGHT-UNK-01"},
    )
    assert exec_unknown.status_code == 409
    assert exec_unknown.json()["error"]["details"]["violation_code"] == "IN_FLIGHT_PENDING_REFUND"

    # Verify no additional SimRefund was created
    with maker() as db:
        all_refs = list(
            db.scalars(
                select(SimRefund).where(SimRefund.payment_id == "PAY-LOCK-INFLIGHT-01")
            ).all()
        )
        assert len(all_refs) == 1


def test_tenant_isolation_rejects_cross_session_and_nonexistent_case_ids(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Hardening 3: Every case_id must be validated against the authenticated session before
    reading or writing RiskSignal/CaseEvent. Cross-session and nonexistent Case IDs return 404."""
    sess_a_token = active_session["token"]
    sess_a_headers = {"X-Demo-Session": sess_a_token}

    # Create a Case in Session A
    case_a_res = client.post(
        "/api/v1/cases",
        headers=sess_a_headers,
        json={"text": "Khiếu nại của phiên A cho đơn ORD-REF-01"},
    )
    assert case_a_res.status_code == 201
    case_a_id = case_a_res.json()["id"]

    # Create isolated Session B
    sess_b_res = client.post("/api/v1/demo/sessions")
    assert sess_b_res.status_code == 201
    sess_b_token = sess_b_res.json()["token"]
    sess_b_headers = {"X-Demo-Session": sess_b_token}

    # 1. Session B attempting to write RiskSignal/CaseEvent to Session A's case_id -> 404 NOT_FOUND
    cross_eval = client.post(
        "/api/v1/risks/evaluate-refund",
        headers=sess_b_headers,
        json={
            "requested_amount": 500000,
            "case_id": case_a_id,
        },
    )
    assert cross_eval.status_code == 404
    assert cross_eval.json()["error"]["code"] == "NOT_FOUND"

    # 2. Session B attempting to write RiskSignal/CaseEvent to nonexistent case_id -> 404 NOT_FOUND
    missing_eval = client.post(
        "/api/v1/risks/evaluate-refund",
        headers=sess_b_headers,
        json={
            "requested_amount": 500000,
            "case_id": "CASE-DOES-NOT-EXIST-999",
        },
    )
    assert missing_eval.status_code == 404
    assert missing_eval.json()["error"]["code"] == "NOT_FOUND"

    # 3. Session B attempting to filter GET /api/v1/risks by Session A's case_id -> 404 NOT_FOUND
    cross_list = client.get(f"/api/v1/risks?case_id={case_a_id}", headers=sess_b_headers)
    assert cross_list.status_code == 404

    # Verify 0 RiskSignals were leaked onto Session A's Case
    maker = get_sessionmaker()
    with maker() as db:
        leaked_sigs = list(
            db.scalars(select(RiskSignal).where(RiskSignal.case_id == case_a_id)).all()
        )
        assert len(leaked_sigs) == 0


def test_docker_and_production_secret_validation() -> None:
    """Hardening 1: APP_ENV=docker or production requires a strong >= 32-char DEMO_APPROVAL_SECRET
    and rejects empty, short, or known insecure default secrets."""
    with pytest.raises(ValidationError, match="DEMO_APPROVAL_SECRET"):
        Settings(
            app_env="docker",
            demo_approval_secret="finresolve-dev-only-hmac-approval-secret-2026-change-me",
        )

    with pytest.raises(ValidationError, match="DEMO_APPROVAL_SECRET"):
        Settings(
            app_env="docker",
            demo_approval_secret="too-short-secret",
        )

    with pytest.raises(ValidationError, match="DEMO_APPROVAL_SECRET"):
        Settings(
            app_env="production",
            demo_approval_secret="",
        )

    valid_docker_settings = Settings(
        app_env="docker",
        demo_approval_secret="finresolve_docker_hmac_signing_key_v1_9f8e7d6c5b4a3f2e",
    )
    assert valid_docker_settings.app_env == "docker"


def test_independent_post_commit_verification_and_idempotent_refund_replay(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Hardening 4 & 6:
    - Phase 1 commits the domain mutation and transitions Case to VERIFYING before Phase 2 opens
      a fresh PostgreSQL session to re-read SimOrder/SimPayment/SimRefund.
    - If post-commit domain postconditions fail in the fresh PostgreSQL session, Verification
      is FAILED and Case transitions to FAILED (never RESOLVED).
    - When post-commit domain postconditions pass, idempotent replay with the same key returns
      the existing verified outcome without creating a second refund; a different key returns 409.
    """
    token = active_session["token"]
    guest_headers = {"X-Demo-Session": token}

    # Seed scenarios via API
    seed_res = client.post("/api/v1/risks/scenarios/seed", headers=guest_headers)
    assert seed_res.status_code == 201
    seeded = seed_res.json()
    prop_id = seeded["valid_partial_refund_proposal_id"]
    pay_id = seeded["valid_partial_refund_payment_id"]

    # Approve via explicitly labeled SIMULATED_DEMO_APPROVAL flow
    appr_res = client.post(
        f"/api/v1/actions/{prop_id}/simulate-demo-approval",
        headers=guest_headers,
        json={
            "reason": "Phê duyệt mô phỏng tự phục vụ cho kịch bản hoàn tiền một phần",
        },
    )
    assert appr_res.status_code == 200
    appr_data = appr_res.json()
    assert appr_data["approver_role"] == "SIMULATED_DEMO_APPROVAL"
    assert appr_data["approval_mode"] == "SIMULATED_DEMO_APPROVAL"
    assert appr_data["is_simulated_demo_approval"] is True

    # Execute proposal and verify post-commit read-back in a fresh PostgreSQL session
    exec_1 = client.post(
        f"/api/v1/actions/{prop_id}/execute",
        headers={**guest_headers, "X-Idempotency-Key": "IDEMP-REF-KEY-01"},
    )
    assert exec_1.status_code == 200
    exec_1_body = exec_1.json()
    assert exec_1_body["idempotent_replay"] is False
    assert exec_1_body["case_state"] == CaseState.RESOLVED.value
    assert exec_1_body["verification"]["verdict"] == VerificationVerdict.PASSED.value
    post_json = exec_1_body["verification"]["postconditions_json"]
    assert post_json["post_commit_durable_read_back"] is True
    assert post_json["read_back_verified"] is True
    assert post_json["observed_completed_amount"] == 200000

    # Idempotent replay with same X-Idempotency-Key returns same execution & 0 extra refunds
    exec_replay = client.post(
        f"/api/v1/actions/{prop_id}/execute",
        headers={**guest_headers, "X-Idempotency-Key": "IDEMP-REF-KEY-01"},
    )
    assert exec_replay.status_code == 200
    assert exec_replay.json()["idempotent_replay"] is True
    assert exec_replay.json()["execution_id"] == exec_1_body["execution_id"]

    # Different X-Idempotency-Key on already executed proposal is rejected with 409 CONFLICT
    exec_diff_key = client.post(
        f"/api/v1/actions/{prop_id}/execute",
        headers={**guest_headers, "X-Idempotency-Key": "IDEMP-REF-KEY-02-DIFF"},
    )
    assert exec_diff_key.status_code == 409

    maker = get_sessionmaker()
    with maker() as db:
        refunds = list(db.scalars(select(SimRefund).where(SimRefund.payment_id == pay_id)).all())
        assert len(refunds) == 1

    # Also test post-commit read-back failure: seed a second batch with force_new=True,
    # approve, and execute with simulate_verification_failure=True so Phase 1 commits
    # ActionExecution(EXECUTED) without writing SimRefund; Phase 2 fresh PostgreSQL session
    # detects missing SimRefund and marks Verification=FAILED and Case=FAILED (never RESOLVED!)
    seed_fail_batch = client.post(
        "/api/v1/risks/scenarios/seed",
        headers=guest_headers,
        json={"force_new": True},
    )
    assert seed_fail_batch.status_code == 201
    fail_prop_id = seed_fail_batch.json()["valid_partial_refund_proposal_id"]

    appr_fail = client.post(
        f"/api/v1/actions/{fail_prop_id}/simulate-demo-approval",
        headers=guest_headers,
        json={"reason": "Phê duyệt kiểm tra post-commit verification failure"},
    )
    assert appr_fail.status_code == 200

    exec_fail = client.post(
        f"/api/v1/actions/{fail_prop_id}/execute",
        headers={**guest_headers, "X-Idempotency-Key": "IDEMP-REF-FAIL-VERIF-01"},
        json={"simulate_verification_failure": True},
    )
    assert exec_fail.status_code == 200
    exec_fail_body = exec_fail.json()
    assert exec_fail_body["execution_state"] == "EXECUTED"
    assert exec_fail_body["verification"]["verdict"] == VerificationVerdict.FAILED.value
    assert (
        exec_fail_body["verification"]["postconditions_json"]["post_commit_durable_read_back"]
        is True
    )
    assert exec_fail_body["verification"]["postconditions_json"]["read_back_verified"] is False
    assert exec_fail_body["case_state"] == CaseState.FAILED.value


def test_hold_action_clearance_lifecycle_and_scenario_seed_idempotency_rate_limit(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Hardening 5:
    - POST /api/v1/risks/scenarios/seed is idempotent by default (replaying 200 OK with
      idempotent_replay=True) and enforces MAX_RISK_SCENARIO_SEEDS_PER_SESSION (429 RATE_LIMITED)
      when force_new=True is called repeatedly.
    - Active HOLD_ACTION RiskSignal blocks ActionProposal approval on that Case until cleared
      via POST /api/v1/risks/{signal_id}/resolve (ACTIVE -> CLEARED)."""
    token = active_session["token"]
    session_id = active_session["id"]
    guest_headers = {"X-Demo-Session": token}

    maker = get_sessionmaker()
    with maker() as db:
        sess_obj = db.get(DemoSession, session_id)
        assert sess_obj is not None
        finance_token = issue_demo_manager_token(sess_obj, role="FINANCE_LEAD")
    finance_headers = {"X-Demo-Session": token, "X-Demo-Manager-Token": finance_token}

    # 1. First seed creates scenarios (201 CREATED, idempotent_replay=False)
    seed_1 = client.post("/api/v1/risks/scenarios/seed", headers=guest_headers)
    assert seed_1.status_code == 201
    assert seed_1.json()["idempotent_replay"] is False

    # 2. Second seed without force_new replays existing scenarios (200 OK, idempotent_replay=True)
    seed_2 = client.post("/api/v1/risks/scenarios/seed", headers=guest_headers)
    assert seed_2.status_code == 200
    assert seed_2.json()["idempotent_replay"] is True
    assert (
        seed_2.json()["valid_partial_refund_case_id"]
        == seed_1.json()["valid_partial_refund_case_id"]
    )

    valid_case_id = seed_1.json()["valid_partial_refund_case_id"]
    valid_prop_id = seed_1.json()["valid_partial_refund_proposal_id"]
    valid_pay_id = seed_1.json()["valid_partial_refund_payment_id"]
    valid_ord_id = seed_1.json()["valid_partial_refund_order_id"]

    # 3. Create an active HOLD_ACTION signal on valid_case_id (e.g. duplicate request_id check)
    #    First insert a dummy refund on another payment to trigger DUPLICATE_REFUND_REQUEST_ID,
    #    or evaluate an over-limit request on valid_case_id to persist an active HOLD_ACTION signal.
    hold_eval = client.post(
        "/api/v1/risks/evaluate-refund",
        headers=guest_headers,
        json={
            "payment_id": valid_pay_id,
            "order_id": valid_ord_id,
            "requested_amount": 900000,  # Exceeds 500k -> creates ACTIVE HOLD_ACTION signal on case
            "case_id": valid_case_id,
        },
    )
    assert hold_eval.status_code == 200
    signal_id = hold_eval.json()["signals"][0]["signal_id"]
    assert signal_id is not None

    # Verify active HOLD_ACTION signal blocks approving the 200k valid proposal on valid_case_id
    blocked_appr = client.post(
        f"/api/v1/actions/{valid_prop_id}/approve",
        headers=finance_headers,
        json={"decision": "APPROVED", "reason": "Thử phê duyệt khi đang có HOLD_ACTION"},
    )
    assert blocked_appr.status_code == 409
    assert blocked_appr.json()["error"]["details"]["violation_code"] == "RISK_HOLD_ACTION_ACTIVE"

    # 4. Manager resolves/clears the HOLD_ACTION signal via POST /api/v1/risks/{signal_id}/resolve
    resolve_res = client.post(
        f"/api/v1/risks/{signal_id}/resolve",
        headers=finance_headers,
        json={
            "new_response": "REVIEW",
            "resolution_note": (
                "Đã kiểm tra yêu cầu 900k là nhập nhầm; mở khóa đề xuất 200k hợp lệ."
            ),
        },
    )
    assert resolve_res.status_code == 200
    resolved_sig = resolve_res.json()
    assert resolved_sig["lifecycle_status"] == "CLEARED"
    assert resolved_sig["response"] == "REVIEW"

    # Now approving the 200k proposal succeeds!
    unblocked_appr = client.post(
        f"/api/v1/actions/{valid_prop_id}/approve",
        headers=finance_headers,
        json={"decision": "APPROVED", "reason": "Phê duyệt hoàn 200k sau khi giải tỏa HOLD_ACTION"},
    )
    assert unblocked_appr.status_code == 200

    # 5. Verify scenario seed rate limit when force_new=True is called beyond limit (3 batches)
    seed_b2 = client.post(
        "/api/v1/risks/scenarios/seed",
        headers=guest_headers,
        json={"force_new": True},
    )
    assert seed_b2.status_code == 201
    seed_b3 = client.post(
        "/api/v1/risks/scenarios/seed",
        headers=guest_headers,
        json={"force_new": True},
    )
    assert seed_b3.status_code == 201
    seed_b4_rate_limited = client.post(
        "/api/v1/risks/scenarios/seed",
        headers=guest_headers,
        json={"force_new": True},
    )
    assert seed_b4_rate_limited.status_code == 429
    assert seed_b4_rate_limited.json()["error"]["code"] == "RATE_LIMITED"
