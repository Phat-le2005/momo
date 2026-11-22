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

from fastapi.testclient import TestClient

from app.core.session import issue_demo_manager_token
from app.db.session import get_sessionmaker
from app.models.cases import Case
from app.models.enums import (
    CaseState,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
    RiskCategory,
    RiskResponse,
    VerificationVerdict,
)
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
