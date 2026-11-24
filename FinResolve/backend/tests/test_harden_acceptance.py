"""Hardened acceptance tests for FinResolve MVP:

Acceptance criteria:
A) Two concurrent reservations cannot oversell the last SKU.
B) Refund exceeding remaining eligible payment amount is rejected.
C) Duplicate provider notification cannot cause duplicate business effect.
D) Missing/ambiguous payment never becomes verified success.
E) Stale Verification or session ID cannot bypass safety.
F) Idempotency: same key with different payload returns 409 CONFLICT.
G) Concurrent session case quota exhaustion cannot exceed max_cases.
"""

from __future__ import annotations

import concurrent.futures
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.cases.state_machine import transition_case
from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.models.actions import Verification
from app.models.cases import Case
from app.models.enums import CaseState, OrderPaymentStatus, PaymentStatus, VerificationVerdict
from app.models.session import DemoSession
from app.models.simulator import (
    InventoryLevel,
    InventoryReservation,
    Product,
    SimOrder,
    SimPayment,
    Warehouse,
)
from app.simulator.services import (
    approve_refund_proposal,
    process_provider_notification,
    process_refund_request,
    propose_refund,
    reserve_inventory,
)


# ---------------------------------------------------------------------------
# Acceptance E1: Session ID cannot authenticate (token-only)
# ---------------------------------------------------------------------------
def test_acceptance_e1_session_id_cannot_authenticate(client: TestClient) -> None:
    # 1. Create a demo session
    res = client.post("/api/v1/demo/sessions")
    assert res.status_code == 201
    session_data = res.json()
    session_id = session_data["session_id"]
    token = session_data["token"]

    # 2. Attempt access using session_id in X-Demo-Session -> MUST be rejected with 401
    neg_res = client.post(
        "/api/v1/cases",
        headers={"X-Demo-Session": session_id},
        json={"text": "Thử đăng nhập bằng session ID công khai"},
    )
    assert neg_res.status_code == 401
    assert neg_res.json()["error"]["code"] == "UNAUTHORIZED"

    # 3. Access using the secret token succeeds
    pos_res = client.post(
        "/api/v1/cases",
        headers={"X-Demo-Session": token},
        json={"text": "Đăng nhập hợp lệ bằng session token bí mật"},
    )
    assert pos_res.status_code == 201


# ---------------------------------------------------------------------------
# Acceptance E2: Stale Verification cannot bypass safety
# ---------------------------------------------------------------------------
def test_acceptance_e2_stale_verification_rejected() -> None:
    maker = get_sessionmaker()
    with maker() as db:
        sess = DemoSession(
            id="sess_stale_test",
            token="dsk_stale_test",
            status="ACTIVE",
            max_cases=10,
            case_count=1,
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        db.add(sess)

        case = Case(
            id="CASE-STALE-001",
            session_id=sess.id,
            type="PAYMENT_DISPUTE",
            goal="Test stale verification prevention",
            state=CaseState.NEW.value,
            version=1,
            owner="guest",
        )
        db.add(case)
        db.commit()

        # Step 1: Transition NEW -> INVESTIGATING -> EXECUTING -> VERIFYING (version becomes 4)
        transition_case(db, case, CaseState.INVESTIGATING, reason="Investigating")
        transition_case(db, case, CaseState.EXECUTING, reason="Executing")
        transition_case(db, case, CaseState.VERIFYING, reason="Verifying")
        db.commit()
        assert case.version == 4

        # Step 2: Create a stale verification bound to version 1
        stale_verif = Verification(
            id="VRF-STALE-001",
            case_id=case.id,
            action_execution_id=None,
            case_version=1,  # Stale: case is at version 4!
            postconditions_json={"status": "PAID"},
            verdict=VerificationVerdict.PASSED.value,
        )
        db.add(stale_verif)
        db.commit()

        # Step 3: Attempt VERIFYING -> RESOLVED with the stale verification -> MUST FAIL with 409
        with pytest.raises(AppError) as exc_info:
            transition_case(
                db,
                case,
                CaseState.RESOLVED,
                reason="Thử giải quyết bằng verification cũ",
                verification=stale_verif,
            )
        assert exc_info.value.status_code == 409
        assert "fresh relevant PASSED verification" in exc_info.value.message

        # Step 4: Create a fresh verification for current version 4
        fresh_verif = Verification(
            id="VRF-FRESH-001",
            case_id=case.id,
            action_execution_id=None,
            case_version=4,  # Matches current version
            postconditions_json={"status": "PAID"},
            verdict=VerificationVerdict.PASSED.value,
        )
        db.add(fresh_verif)
        db.commit()

        # Step 5: Transition with fresh verification succeeds
        transition_case(
            db,
            case,
            CaseState.RESOLVED,
            reason="Giải quyết với verification mới",
            verification=fresh_verif,
        )
        db.commit()
        assert case.state == CaseState.RESOLVED.value
        assert case.version == 5


# ---------------------------------------------------------------------------
# Acceptance A: Two concurrent reservations cannot oversell the last SKU
# ---------------------------------------------------------------------------
def test_acceptance_a_concurrent_reservations_cannot_oversell() -> None:
    maker = get_sessionmaker()
    session_id = "sess_concurrent_inv"

    # Setup: 1 SKU with on_hand=1, reserved=0
    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_concurrent_inv",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        wh = Warehouse(
            id="WH-TEST-01",
            session_id=session_id,
            code="WH-TEST-01",
            name="Kho Test",
        )
        prod = Product(
            id="PROD-LAST-UNIT",
            session_id=session_id,
            name="Sản phẩm cuối cùng",
            sku="SKU-LAST-01",
            price=100000,
            currency="VND",
        )
        inv = InventoryLevel(
            id="INV-LAST-01",
            session_id=session_id,
            product_id=prod.id,
            warehouse_id=wh.id,
            on_hand=1,
            reserved=0,
            version=1,
        )
        db.add_all([sess, wh, prod])
        db.flush()
        db.add(inv)
        db.commit()

    results: list[dict[str, str | bool]] = []

    def attempt_reservation(worker_id: int) -> None:
        with maker() as worker_db:
            try:
                reserve_inventory(
                    worker_db,
                    session_id=session_id,
                    product_id="PROD-LAST-UNIT",
                    warehouse_id="WH-TEST-01",
                    quantity=1,
                )
                worker_db.commit()
                results.append({"worker": str(worker_id), "success": True})
            except AppError as e:
                worker_db.rollback()
                results.append({"worker": str(worker_id), "success": False, "error": e.code})

    # Launch 2 concurrent workers attempting to reserve the 1 available unit
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt_reservation, i) for i in (1, 2)]
        concurrent.futures.wait(futures)

    # Verify results: exactly 1 worker succeeded, exactly 1 worker failed with CONFLICT
    successes = [r for r in results if r["success"]]
    failures = [r for r in results if not r["success"]]

    assert len(successes) == 1, f"Expected 1 success, got {len(successes)}"
    assert len(failures) == 1, f"Expected 1 failure, got {len(failures)}"
    assert failures[0]["error"] == "CONFLICT"

    # Verify DB state: reserved is 1, on_hand is 1 (never oversold to 2)
    with maker() as db:
        final_inv = db.get(InventoryLevel, "INV-LAST-01")
        assert final_inv is not None
        assert final_inv.reserved == 1
        assert final_inv.on_hand == 1
        assert final_inv.available == 0

        reservations = (
            db.query(InventoryReservation)
            .filter(InventoryReservation.product_id == "PROD-LAST-UNIT")
            .all()
        )
        assert len(reservations) == 1


# ---------------------------------------------------------------------------
# Acceptance B: Refund exceeding remaining eligible payment is rejected
# ---------------------------------------------------------------------------
def test_acceptance_b_refund_exceeding_remaining_eligible_rejected() -> None:
    maker = get_sessionmaker()
    session_id = "sess_refund_test"
    payment_id = "PAY-REFUND-001"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_refund_test",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        order = SimOrder(
            id="ORD-REFUND-001",
            session_id=session_id,
            customer_name="Trần Văn Test",
            customer_phone="0909999999",
            amount=500000,
            grand_total=500000,
            payment_status=OrderPaymentStatus.PAID.value,
        )
        pay = SimPayment(
            id=payment_id,
            session_id=session_id,
            order_id=order.id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_REFUND_01",
            amount=500000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=datetime.now(UTC),
        )
        db.add_all([sess, order, pay])
        db.commit()

        # Step 0: Calling process_refund_request without real approval must fail with 403
        with pytest.raises(AppError) as exc_unauth:
            process_refund_request(
                db,
                session_id=session_id,
                payment_id=payment_id,
                amount=200000,
                reason="Hủy hàng",
            )
        assert exc_unauth.value.status_code == 403
        assert "cannot bypass real Approval" in exc_unauth.value.message

        # Step 1: Partial refund for 200,000 VND (eligible remaining: 300,000 VND)
        prop1 = propose_refund(
            db,
            session_id=session_id,
            payment_id=payment_id,
            amount=200000,
            reason="Hủy một phần hàng",
        )
        db.commit()
        appr1 = approve_refund_proposal(db, proposal_id=prop1.id, session_id=session_id)
        db.commit()

        ref1 = process_refund_request(
            db,
            session_id=session_id,
            payment_id=payment_id,
            amount=200000,
            reason="Hủy một phần hàng",
            approval_id=appr1.id,
            is_authorized=True,
        )
        db.commit()
        assert ref1.completed_amount == 200000
        assert order.payment_status == OrderPaymentStatus.PARTIALLY_REFUNDED.value

        # Step 2: Attempting second refund for 350,000 VND (exceeds 300,000 VND)
        # MUST FAIL with 409
        with pytest.raises(AppError) as exc_info:
            propose_refund(
                db,
                session_id=session_id,
                payment_id=payment_id,
                amount=350000,
                reason="Hoàn vượt mức còn lại",
            )
        assert exc_info.value.status_code == 409
        assert "exceeds remaining eligible refundable balance" in exc_info.value.message

        # Step 3: Legitimate second refund for remaining 300,000 VND succeeds
        prop2 = propose_refund(
            db,
            session_id=session_id,
            payment_id=payment_id,
            amount=300000,
            reason="Hoàn phần còn lại",
        )
        db.commit()
        appr2 = approve_refund_proposal(db, proposal_id=prop2.id, session_id=session_id)
        db.commit()

        ref2 = process_refund_request(
            db,
            session_id=session_id,
            payment_id=payment_id,
            amount=300000,
            reason="Hoàn phần còn lại",
            approval_id=appr2.id,
            is_authorized=True,
        )
        db.commit()
        assert ref2.completed_amount == 300000
        assert order.payment_status == OrderPaymentStatus.REFUNDED.value

        # Step 4: Attempting third refund after fully refunded -> MUST FAIL with 409
        with pytest.raises(AppError) as exc_info2:
            propose_refund(
                db,
                session_id=session_id,
                payment_id=payment_id,
                amount=10000,
                reason="Hoàn thêm khi đã hết tiền",
            )
        assert exc_info2.value.status_code == 409
        assert "already been fully refunded" in exc_info2.value.message


# ---------------------------------------------------------------------------
# Acceptance C: Duplicate provider notification cannot cause duplicate effect
# ---------------------------------------------------------------------------
def test_acceptance_c_duplicate_provider_notification_deduplicated() -> None:
    maker = get_sessionmaker()
    session_id = "sess_ipn_test"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_ipn_test",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        db.add(sess)
        db.commit()

        # First IPN webhook notification from verified provider adapter
        pay1, is_new1 = process_provider_notification(
            db,
            session_id=session_id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_IPN_DUPLICATE_01",
            amount=250000,
            status=PaymentStatus.SUCCESS,
            request_id="REQ_IPN_001",
            is_trusted=True,
            adapter_verified=True,
        )
        db.commit()
        assert is_new1 is True
        assert pay1.status == PaymentStatus.SUCCESS.value

        # Second duplicate IPN webhook notification with identical provider_ref
        pay2, is_new2 = process_provider_notification(
            db,
            session_id=session_id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_IPN_DUPLICATE_01",
            amount=250000,
            status=PaymentStatus.SUCCESS,
            request_id="REQ_IPN_001",
            is_trusted=True,
            adapter_verified=True,
        )
        db.commit()
        assert is_new2 is False
        assert pay2.id == pay1.id

        # Verify exactly 1 payment record exists in DB
        payments = (
            db.query(SimPayment)
            .filter(
                SimPayment.session_id == session_id,
                SimPayment.provider_ref == "MOMO_TXN_IPN_DUPLICATE_01",
            )
            .all()
        )
        assert len(payments) == 1


# ---------------------------------------------------------------------------
# Acceptance D: Missing / ambiguous payment never becomes verified success
# ---------------------------------------------------------------------------
def test_acceptance_d_ambiguous_payment_never_verified() -> None:
    maker = get_sessionmaker()
    session_id = "sess_ambig_test"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_ambig_test",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        db.add(sess)

        # 1. Payment with UNKNOWN status
        pay_unknown, _ = process_provider_notification(
            db,
            session_id=session_id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_UNKNOWN_01",
            amount=100000,
            status=PaymentStatus.UNKNOWN,
        )

        # 2. Payment with AUTHORIZED status (not captured yet)
        pay_auth, _ = process_provider_notification(
            db,
            session_id=session_id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_AUTH_01",
            amount=150000,
            status=PaymentStatus.AUTHORIZED,
        )
        db.commit()

        # Hard Domain Invariant: UNKNOWN and AUTHORIZED payments MUST NOT have verified_at set
        assert pay_unknown.status == PaymentStatus.UNKNOWN.value
        assert pay_unknown.verified_at is None

        assert pay_auth.status == PaymentStatus.AUTHORIZED.value
        assert pay_auth.verified_at is None


# ---------------------------------------------------------------------------
# Acceptance F: Idempotency with different payload returns 409 CONFLICT
# ---------------------------------------------------------------------------
def test_acceptance_f_idempotency_different_payload_returns_conflict(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    key = "idemp-diff-test-99"

    # 1. First request with payload A
    res1 = client.post(
        "/api/v1/cases",
        headers={"X-Demo-Session": token, "X-Idempotency-Key": key},
        json={
            "text": "Khiếu nại A: Tôi đã trả tiền nhưng chưa nhận hàng",
            "type": "PAYMENT_DISPUTE",
        },
    )
    assert res1.status_code == 201

    # 2. Second request with same key but DIFFERENT payload B -> MUST return 409 CONFLICT
    res2 = client.post(
        "/api/v1/cases",
        headers={"X-Demo-Session": token, "X-Idempotency-Key": key},
        json={"text": "Khiếu nại B: Hoàn toàn khác nội dung khiếu nại A", "type": "REFUND_REQUEST"},
    )
    assert res2.status_code == 409
    assert res2.json()["error"]["code"] == "CONFLICT"
    assert "different request payload" in res2.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Acceptance G: Concurrent session quota exhaustion cannot exceed max_cases
# ---------------------------------------------------------------------------
def test_acceptance_g_concurrent_quota_exhaustion_on_postgres(client: TestClient) -> None:
    # Create session with quota max_cases = 2
    sess_res = client.post("/api/v1/demo/sessions", json={"max_cases": 2})
    assert sess_res.status_code == 201
    token = sess_res.json()["token"]
    session_id = sess_res.json()["session_id"]

    results: list[int] = []

    def make_case_request(i: int) -> None:
        tclient = TestClient(client.app)
        r = tclient.post(
            "/api/v1/cases",
            headers={"X-Demo-Session": token},
            json={"text": f"Khiếu nại đồng thời #{i}"},
        )
        results.append(r.status_code)

    # Launch 4 concurrent requests trying to create cases in this session
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(make_case_request, i) for i in range(4)]
        concurrent.futures.wait(futures)

    # Exactly 2 requests must succeed (201 Created), remaining 2 must be 429
    created_count = sum(1 for code in results if code == 201)
    rate_limited_count = sum(1 for code in results if code == 429)

    assert created_count == 2, f"Expected 2 created cases, got {created_count}"
    assert rate_limited_count == 2, f"Expected 2 rate limited requests, got {rate_limited_count}"

    # Verify DB session record has case_count == 2
    maker = get_sessionmaker()
    with maker() as db:
        session_row = db.get(DemoSession, session_id)
        assert session_row is not None
        assert session_row.case_count == 2
