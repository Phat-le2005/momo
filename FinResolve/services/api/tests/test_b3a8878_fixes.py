"""Regression tests for business-safety issues review (commit b3a8878).

Test Matrix:
1. Direct refund path removal: process_refund_request cannot bypass real Approval.
   is_authorized defaults to False.
2. Two competing approved refund proposals: second execution is rejected at execution time
   when remaining refundable balance is exceeded.
3. propose_refund(case_id=None) works cleanly without foreign-key violation.
4. execute_refund_proposal rejects stale (expired) and tampered approvals at execution time.
5. Idempotent re-execution returns existing ActionExecution and SimRefund.
6. Independent read-back verification: postconditions verified by independent DB read-back.
7. process_provider_notification: default is_trusted=False, only verified provider adapters
   can establish SUCCESS, validates merchant, order ID, amount, and currency.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.models.actions import ActionProposal
from app.models.cases import Case
from app.models.enums import (
    ActionProposalStatus,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
    VerificationVerdict,
)
from app.models.session import DemoSession
from app.models.simulator import SimOrder, SimPayment
from app.simulator.services import (
    approve_refund_proposal,
    execute_refund_proposal,
    process_provider_notification,
    process_refund_request,
    propose_refund,
    verify_refund_execution,
)

# ---------------------------------------------------------------------------
# 1. Remove Unsafe Direct Refund Path & Never Default is_authorized to True
# ---------------------------------------------------------------------------


def test_process_refund_request_cannot_bypass_real_approval() -> None:
    maker = get_sessionmaker()
    session_id = "sess_safe_refund_01"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_safe_refund_01",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        pay = SimPayment(
            id="PAY-SAFE-01",
            session_id=session_id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_SAFE_01",
            amount=500000,
            status=PaymentStatus.SUCCESS.value,
            verified_at=datetime.now(UTC),
        )
        db.add_all([sess, pay])
        db.commit()

        # 1a. Missing approval_id and default authorization -> MUST FAIL with 403
        with pytest.raises(AppError) as exc_default:
            process_refund_request(
                db,
                session_id=session_id,
                payment_id=pay.id,
                amount=100000,
                reason="Direct refund attempt",
            )
        assert exc_default.value.status_code == 403
        assert "cannot bypass real Approval" in exc_default.value.message

        # 1b. Non-existent approval_id -> MUST FAIL with 403
        with pytest.raises(AppError) as exc_fake_appr:
            process_refund_request(
                db,
                session_id=session_id,
                payment_id=pay.id,
                amount=100000,
                reason="Fake approval attempt",
                approval_id="APPR-NON-EXISTENT",
                is_authorized=True,
            )
        assert exc_fake_appr.value.status_code == 403
        assert "not found" in exc_fake_appr.value.message

        # 1c. With real proposal and real approval -> succeeds through real pipeline
        prop = propose_refund(
            db,
            session_id=session_id,
            payment_id=pay.id,
            amount=100000,
            reason="Hợp lệ có phê duyệt",
        )
        db.commit()
        appr = approve_refund_proposal(db, proposal_id=prop.id, session_id=session_id)
        db.commit()

        ref = process_refund_request(
            db,
            session_id=session_id,
            payment_id=pay.id,
            amount=100000,
            reason="Hợp lệ có phê duyệt",
            approval_id=appr.id,
            is_authorized=True,
        )
        db.commit()
        assert ref.status == RefundStatus.COMPLETED.value
        assert ref.completed_amount == 100000


# ---------------------------------------------------------------------------
# 2. Two Competing Approved Refund Proposals for One Payment
# ---------------------------------------------------------------------------


def test_two_competing_approved_refund_proposals_rejected_at_execution_time() -> None:
    maker = get_sessionmaker()
    session_id = "sess_competing_proposals"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_competing_proposals",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        # Payment has total amount = 400,000 VND
        pay = SimPayment(
            id="PAY-COMPETE-01",
            session_id=session_id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_COMPETE_01",
            amount=400000,
            status=PaymentStatus.SUCCESS.value,
            verified_at=datetime.now(UTC),
        )
        db.add_all([sess, pay])
        db.commit()

        # Step 1: Create and approve Proposal A for 300,000 VND
        prop_a = propose_refund(
            db,
            session_id=session_id,
            payment_id=pay.id,
            amount=300000,
            reason="Proposal A: Khách yêu cầu hoàn hàng",
        )
        db.commit()
        appr_a = approve_refund_proposal(db, proposal_id=prop_a.id, session_id=session_id)
        db.commit()
        assert appr_a.decision == "APPROVED"
        assert prop_a.status == ActionProposalStatus.APPROVED.value

        # Step 2: Create and approve Proposal B for 300,000 VND
        # Note: at proposal time, if nothing has executed yet, Proposal B is approved
        prop_b = propose_refund(
            db,
            session_id=session_id,
            payment_id=pay.id,
            amount=300000,
            reason="Proposal B: Đơn vị vận chuyển yêu cầu hoàn tiền",
        )
        db.commit()
        appr_b = approve_refund_proposal(db, proposal_id=prop_b.id, session_id=session_id)
        db.commit()
        assert appr_b.decision == "APPROVED"
        assert prop_b.status == ActionProposalStatus.APPROVED.value

        # Step 3: Execute Proposal A -> succeeds (remaining eligible balance becomes 100,000)
        exec_a, ref_a = execute_refund_proposal(
            db,
            proposal_id=prop_a.id,
            session_id=session_id,
            idempotency_key="REQ-EXEC-COMPETE-A",
        )
        db.commit()
        assert ref_a.status == RefundStatus.COMPLETED.value
        assert ref_a.completed_amount == 300000

        # Step 4: Execute Proposal B -> MUST FAIL with 409 Conflict at execution time!
        # Recalculated balance under row lock is 100,000, but proposal B requests 300,000.
        with pytest.raises(AppError) as exc_compete:
            execute_refund_proposal(
                db,
                proposal_id=prop_b.id,
                session_id=session_id,
                idempotency_key="REQ-EXEC-COMPETE-B",
            )
        assert exc_compete.value.status_code == 409
        assert "exceeds remaining eligible refundable balance" in exc_compete.value.message


# ---------------------------------------------------------------------------
# 3. propose_refund(case_id=None) Foreign-Key Behavior
# ---------------------------------------------------------------------------


def test_propose_refund_with_case_id_none() -> None:
    maker = get_sessionmaker()
    session_id = "sess_case_id_none"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_case_id_none",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        pay = SimPayment(
            id="PAY-NO-CASE-01",
            session_id=session_id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_NOCASE_01",
            amount=200000,
            status=PaymentStatus.SUCCESS.value,
            verified_at=datetime.now(UTC),
        )
        db.add_all([sess, pay])
        db.commit()

        # Must succeed with case_id=None without any foreign-key constraint violation
        prop = propose_refund(
            db,
            session_id=session_id,
            payment_id=pay.id,
            amount=100000,
            reason="Refund without intake case",
            case_id=None,
        )
        db.commit()
        assert prop.id.startswith("PROP-")
        assert prop.case_id is None

        # Verify proposal stored in DB has case_id as NULL
        prop_db = db.scalar(select(ActionProposal).where(ActionProposal.id == prop.id))
        assert prop_db is not None
        assert prop_db.case_id is None


# ---------------------------------------------------------------------------
# 4. Reject Stale and Tampered Approvals at Execution Time
# ---------------------------------------------------------------------------


def test_execute_refund_proposal_rejects_stale_and_tampered_approvals() -> None:
    maker = get_sessionmaker()
    session_id = "sess_stale_appr"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_stale_appr",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        pay = SimPayment(
            id="PAY-STALE-01",
            session_id=session_id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_STALE_01",
            amount=300000,
            status=PaymentStatus.SUCCESS.value,
            verified_at=datetime.now(UTC),
        )
        db.add_all([sess, pay])
        db.commit()

        # 4a. Stale / expired approval
        prop1 = propose_refund(
            db,
            session_id=session_id,
            payment_id=pay.id,
            amount=100000,
            reason="Refund to expire",
        )
        db.commit()
        appr1 = approve_refund_proposal(db, proposal_id=prop1.id, session_id=session_id)
        # Artificially expire the approval
        appr1.expiry = datetime.now(UTC) - timedelta(minutes=5)
        db.commit()

        with pytest.raises(AppError) as exc_expired:
            execute_refund_proposal(
                db,
                proposal_id=prop1.id,
                session_id=session_id,
                idempotency_key="REQ-EXPIRED",
            )
        assert exc_expired.value.status_code == 409
        assert "expired" in exc_expired.value.message

        # 4b. Tampered approval digest
        prop2 = propose_refund(
            db,
            session_id=session_id,
            payment_id=pay.id,
            amount=100000,
            reason="Refund to tamper",
        )
        db.commit()
        appr2 = approve_refund_proposal(db, proposal_id=prop2.id, session_id=session_id)
        # Corrupt the approval digest
        appr2.digest = hashlib.sha256(b"tampered").hexdigest()
        db.commit()

        with pytest.raises(AppError) as exc_tamper:
            execute_refund_proposal(
                db,
                proposal_id=prop2.id,
                session_id=session_id,
                idempotency_key="REQ-TAMPERED",
            )
        assert exc_tamper.value.status_code == 409
        assert "digest mismatch" in exc_tamper.value.message


# ---------------------------------------------------------------------------
# 5. Independent Read-Back Verification
# ---------------------------------------------------------------------------


def test_independent_read_back_verification() -> None:
    maker = get_sessionmaker()
    session_id = "sess_readback"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_readback",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        case = Case(
            id="CASE-RB-01",
            session_id=session_id,
            goal="Test read back verification",
            state="INVESTIGATING",
            version=1,
        )
        order = SimOrder(
            id="ORD-RB-01",
            session_id=session_id,
            customer_name="Trần Thị Duyên",
            customer_phone="0988001122",
            amount=200000,
            subtotal=200000,
            grand_total=200000,
            payment_status=OrderPaymentStatus.PAID.value,
        )
        pay = SimPayment(
            id="PAY-RB-01",
            session_id=session_id,
            order_id=order.id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_RB_01",
            amount=200000,
            status=PaymentStatus.SUCCESS.value,
            verified_at=datetime.now(UTC),
        )
        db.add_all([sess, case, order, pay])
        db.commit()

        prop = propose_refund(
            db,
            session_id=session_id,
            payment_id=pay.id,
            amount=200000,
            reason="Readback test refund",
            case_id=case.id,
        )
        db.commit()
        approve_refund_proposal(db, proposal_id=prop.id, session_id=session_id)
        db.commit()

        exec_res, ref = execute_refund_proposal(
            db,
            proposal_id=prop.id,
            session_id=session_id,
            idempotency_key="REQ-EXEC-RB",
        )
        db.commit()

        # Independent read-back should find consistent DB state and generate PASSED verification
        vrf = verify_refund_execution(
            db,
            session_id=session_id,
            execution_id=exec_res.id,
            case_id=case.id,
        )
        db.commit()
        assert vrf.verdict == VerificationVerdict.PASSED.value
        assert vrf.postconditions_json["read_back_verified"] is True
        assert vrf.postconditions_json["completed_amount"] == 200000

        # Idempotency check: re-executing with the same idempotency key returns existing records
        exec_replay, ref_replay = execute_refund_proposal(
            db,
            proposal_id=prop.id,
            session_id=session_id,
            idempotency_key="REQ-EXEC-RB",
        )
        assert exec_replay.id == exec_res.id
        assert ref_replay.id == ref.id


# ---------------------------------------------------------------------------
# 6. process_provider_notification: Strict Verification & Validation
# ---------------------------------------------------------------------------


def test_provider_notification_adapter_verification_and_validation() -> None:
    maker = get_sessionmaker()
    session_id = "sess_notif_strict"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_notif_strict",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        order = SimOrder(
            id="ORD-STRICT-01",
            session_id=session_id,
            customer_name="Phạm Văn Nam",
            customer_phone="0977112233",
            amount=250000,
            currency="VND",
            subtotal=250000,
            grand_total=250000,
            status=OrderStatus.PENDING.value,
            payment_status=OrderPaymentStatus.UNPAID.value,
        )
        db.add_all([sess, order])
        db.commit()

        # 6a. Default is_trusted=False: attempting SUCCESS raises 403 Forbidden
        with pytest.raises(AppError) as exc_untrusted:
            process_provider_notification(
                db,
                session_id=session_id,
                provider="MOCK_MOMO",
                provider_ref="MOMO_TXN_UNTRUSTED_01",
                amount=250000,
                status=PaymentStatus.SUCCESS,
            )
        assert exc_untrusted.value.status_code == 403
        assert (
            "Only verified provider adapters can establish SUCCESS" in exc_untrusted.value.message
        )

        # 6b. Caller passes fake verification_source without adapter_verified -> 403 Forbidden
        with pytest.raises(AppError) as exc_fake_source:
            process_provider_notification(
                db,
                session_id=session_id,
                provider="MOCK_MOMO",
                provider_ref="MOMO_TXN_FAKE_SOURCE",
                amount=250000,
                status=PaymentStatus.SUCCESS,
                verification_source="PROVIDER_QUERY_AUTHORITATIVE",
                is_trusted=False,
                adapter_verified=False,
            )
        assert exc_fake_source.value.status_code == 403

        # 6c. Order ID amount mismatch -> 409 Conflict
        with pytest.raises(AppError) as exc_mismatch:
            process_provider_notification(
                db,
                session_id=session_id,
                provider="MOCK_MOMO",
                provider_ref="MOMO_TXN_MISMATCH",
                amount=500000,  # Order is 250,000!
                order_id=order.id,
                status=PaymentStatus.SUCCESS,
                is_trusted=True,
                adapter_verified=True,
            )
        assert exc_mismatch.value.status_code == 409
        assert "does not match order" in exc_mismatch.value.message

        # 6d. Legitimate verified adapter notification succeeds and updates order
        pay, is_new = process_provider_notification(
            db,
            session_id=session_id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_VALID_01",
            amount=250000,
            order_id=order.id,
            status=PaymentStatus.SUCCESS,
            is_trusted=True,
            adapter_verified=True,
        )
        db.commit()
        assert is_new is True
        assert pay.status == PaymentStatus.SUCCESS.value
        assert pay.verification_source == "MOCK_MOMO_ADAPTER_VERIFIED"

        db.refresh(order)
        assert order.payment_status == OrderPaymentStatus.PAID.value
        assert order.status == OrderStatus.PAID.value
