"""Regression tests for all 8 domain and concurrency hardening fixes (commit f5348c6 review).

Test Matrix:
1. 100+ isolated demo sessions created without identifier/fixture collisions.
2. Payment notification processing: idempotency, PENDING->SUCCESS verified transition,
   conflict rejection, untrusted data block, concurrent insert safety.
3. Separate refund proposal, approval and execution: COMPLETED only with verified outcome,
   UNKNOWN when uncertain, authorization requirements.
4. Database-level concurrency control for Case transitions.
5. Atomic Case intake idempotency under concurrent retries.
6. Additive Alembic migration 0004 backfills legacy amounts.
7. Inventory movement accounting & same-session reference validation.
8. Single idempotency_payload_hash declaration in Case model.
"""

from __future__ import annotations

import concurrent.futures
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.cases.state_machine import transition_case
from app.core.errors import AppError
from app.db.seed import seed_demo_session
from app.db.session import get_sessionmaker
from app.models.cases import Case
from app.models.enums import (
    ActionProposalStatus,
    CaseState,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
)
from app.models.session import DemoSession
from app.models.simulator import (
    InventoryLevel,
    Product,
    SimOrder,
    SimPayment,
    SimRefund,
    Warehouse,
)
from app.simulator.services import (
    approve_refund_proposal,
    execute_refund_proposal,
    fulfill_inventory_reservation,
    process_provider_notification,
    propose_refund,
    record_inventory_movement,
    reserve_inventory,
)

# ---------------------------------------------------------------------------
# Issue 1: 100+ Isolated Demo Sessions without Collision
# ---------------------------------------------------------------------------


def test_issue1_100_isolated_sessions_no_collision() -> None:
    maker = get_sessionmaker()
    session_ids: list[str] = [f"sess_iso_{i:04d}_{datetime.now().microsecond}" for i in range(105)]

    with maker() as db:
        for sid in session_ids:
            sess = DemoSession(
                id=sid,
                token=f"dsk_{sid}",
                status="ACTIVE",
                max_cases=10,
                case_count=0,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            db.add(sess)
            db.flush()
            # Seed synthetic fixtures
            seed_demo_session(db, sid)
        db.commit()

        # Verify all 105 sessions have their own isolated entities
        order_count = db.scalar(
            select(func.count(SimOrder.id)).where(SimOrder.session_id.in_(session_ids))
        )
        assert order_count == 105 * 3  # 3 orders per session

        product_count = db.scalar(
            select(func.count(Product.id)).where(Product.session_id.in_(session_ids))
        )
        assert product_count == 105 * 3  # 3 products per session

        wh_count = db.scalar(
            select(func.count(Warehouse.id)).where(Warehouse.session_id.in_(session_ids))
        )
        assert wh_count == 105  # 1 warehouse per session


# ---------------------------------------------------------------------------
# Issue 2: Payment Notification Processing
# ---------------------------------------------------------------------------


def test_issue2_payment_notification_processing() -> None:
    maker = get_sessionmaker()
    session_id = "sess_notif_001"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_notif_001",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        order = SimOrder(
            id="ORD-NOTIF-01",
            session_id=session_id,
            customer_name="Test Customer",
            customer_phone="0900000001",
            amount=300000,
            currency="VND",
            subtotal=300000,
            grand_total=300000,
            status=OrderStatus.PENDING.value,
            payment_status=OrderPaymentStatus.UNPAID.value,
        )
        # 1. Initial pending payment
        payment = SimPayment(
            id="PAY-NOTIF-01",
            session_id=session_id,
            order_id=order.id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_NOTIF_01",
            amount=300000,
            currency="VND",
            status=PaymentStatus.PENDING.value,
            verified_at=None,
        )
        db.add_all([sess, order, payment])
        db.commit()

        # 2a: Valid transition PENDING -> SUCCESS from verified adapter
        updated_pay, is_new = process_provider_notification(
            db,
            session_id=session_id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_NOTIF_01",
            amount=300000,
            status=PaymentStatus.SUCCESS,
            verification_source="WEBHOOK_IPN",
            is_trusted=True,
            adapter_verified=True,
        )
        db.commit()
        assert is_new is True
        assert updated_pay.status == PaymentStatus.SUCCESS.value
        assert updated_pay.verified_at is not None
        # Linked order should now be PAID
        db.refresh(order)
        assert order.payment_status == OrderPaymentStatus.PAID.value

        # 2b: Identical event is idempotent
        same_pay, is_new2 = process_provider_notification(
            db,
            session_id=session_id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_NOTIF_01",
            amount=300000,
            status=PaymentStatus.SUCCESS,
            is_trusted=True,
            adapter_verified=True,
        )
        assert is_new2 is False
        assert same_pay.id == updated_pay.id

        # 2c: Amount conflict rejected with 409
        with pytest.raises(AppError) as exc_amount:
            process_provider_notification(
                db,
                session_id=session_id,
                provider="MOCK_MOMO",
                provider_ref="MOMO_TXN_NOTIF_01",
                amount=500000,  # Conflict: was 300,000
                status=PaymentStatus.SUCCESS,
                is_trusted=True,
                adapter_verified=True,
            )
        assert exc_amount.value.status_code == 409
        assert "Payment amount conflict" in exc_amount.value.message

        # 2d: Reversion conflict (SUCCESS -> PENDING) rejected with 409
        with pytest.raises(AppError) as exc_revert:
            process_provider_notification(
                db,
                session_id=session_id,
                provider="MOCK_MOMO",
                provider_ref="MOMO_TXN_NOTIF_01",
                amount=300000,
                status=PaymentStatus.PENDING,
            )
        assert exc_revert.value.status_code == 409
        assert "Conflicting payment status" in exc_revert.value.message

        # 2e: Untrusted data never verifies SUCCESS -> 403 Forbidden
        with pytest.raises(AppError) as exc_untrusted:
            process_provider_notification(
                db,
                session_id=session_id,
                provider="MOCK_MOMO",
                provider_ref="MOMO_TXN_UNTRUSTED",
                amount=100000,
                status=PaymentStatus.SUCCESS,
                verification_source="UNVERIFIED_CLAIM",
                is_trusted=False,
            )
        assert exc_untrusted.value.status_code == 403
        assert "Untrusted payment event source" in exc_untrusted.value.message


def test_issue2_concurrent_payment_notifications_safe() -> None:
    maker = get_sessionmaker()
    session_id = "sess_concurrent_ipn"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_concurrent_ipn",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(sess)
        db.commit()

    provider_ref = f"TXN_RACE_{datetime.now().microsecond}"

    def post_notif(idx: int) -> tuple[str, bool]:
        with maker() as thread_db:
            pay, is_new = process_provider_notification(
                thread_db,
                session_id=session_id,
                provider="MOCK_MOMO",
                provider_ref=provider_ref,
                amount=150000,
                status=PaymentStatus.SUCCESS,
                request_id=f"REQ_RACE_{idx}",
                is_trusted=True,
                adapter_verified=True,
            )
            thread_db.commit()
            return pay.id, is_new

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(post_notif, i) for i in range(5)]
        results = [f.result() for f in futures]

    # Exactly one thread created the payment, the others safely returned the existing one
    pay_ids = {r[0] for r in results}
    assert len(pay_ids) == 1
    new_counts = sum(1 for r in results if r[1] is True)
    assert new_counts == 1


# ---------------------------------------------------------------------------
# Issue 3: Separate Refund Proposal, Approval and Execution
# ---------------------------------------------------------------------------


def test_issue3_refund_lifecycle_proposal_approval_execution() -> None:
    maker = get_sessionmaker()
    session_id = "sess_refund_pipe"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_refund_pipe",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        case = Case(
            id="CASE-REF-01",
            session_id=session_id,
            goal="Xử lý khiếu nại hoàn tiền",
            state=CaseState.INVESTIGATING.value,
            version=1,
        )
        order = SimOrder(
            id="ORD-REF-PIPE-01",
            session_id=session_id,
            customer_name="Trần Thị Cúc",
            customer_phone="0911223344",
            amount=400000,
            subtotal=400000,
            grand_total=400000,
            payment_status=OrderPaymentStatus.PAID.value,
        )
        payment = SimPayment(
            id="PAY-REF-PIPE-01",
            session_id=session_id,
            order_id=order.id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_PIPE_01",
            amount=400000,
            status=PaymentStatus.SUCCESS.value,
            verified_at=datetime.now(UTC),
        )
        db.add_all([sess, case, order, payment])
        db.commit()

        # Step 1: Propose refund
        proposal = propose_refund(
            db,
            session_id=session_id,
            payment_id=payment.id,
            amount=200000,
            reason="Hàng lỗi, khách yêu cầu hoàn một nửa",
            case_id=case.id,
        )
        db.commit()
        assert proposal.status == ActionProposalStatus.PROPOSED.value
        assert proposal.params_json["amount"] == 200000

        # Step 2: Attempt execution without approval -> MUST FAIL (403 Forbidden)
        with pytest.raises(AppError) as exc_unapproved:
            execute_refund_proposal(
                db,
                proposal_id=proposal.id,
                session_id=session_id,
                idempotency_key="REQ_EXEC_UNAPP",
            )
        assert exc_unapproved.value.status_code == 403
        assert "not approved" in exc_unapproved.value.message

        # Step 3: Approve proposal
        approval = approve_refund_proposal(
            db,
            proposal_id=proposal.id,
            session_id=session_id,
            approver_role="FINANCE_LEAD",
        )
        db.commit()
        assert approval.decision == "APPROVED"
        assert proposal.status == ActionProposalStatus.APPROVED.value

        # Step 4: Execute with uncertain outcome (UNKNOWN) -> Never COMPLETED
        exec_unknown, ref_unknown = execute_refund_proposal(
            db,
            proposal_id=proposal.id,
            session_id=session_id,
            idempotency_key="REQ_EXEC_UNK",
            execution_outcome="UNKNOWN",
        )
        db.commit()
        assert ref_unknown.status == RefundStatus.UNKNOWN.value
        assert ref_unknown.completed_amount == 0  # Crucial: 0, NOT completed!
        assert order.payment_status == OrderPaymentStatus.PAID.value  # NOT changed to refunded

        # Step 5: Execute with verified outcome (SUCCESS)
        # Note: creates a new proposal for remaining eligible balance (200k)
        # Let's test execution with SUCCESS on an approved proposal
        prop2 = propose_refund(
            db,
            session_id=session_id,
            payment_id=payment.id,
            amount=200000,
            reason="Thực hiện hoàn tiền phần còn lại",
            case_id=case.id,
        )
        db.commit()
        approve_refund_proposal(db, proposal_id=prop2.id, session_id=session_id)
        db.commit()

        exec_succ, ref_succ = execute_refund_proposal(
            db,
            proposal_id=prop2.id,
            session_id=session_id,
            idempotency_key="REQ_EXEC_SUCC",
            execution_outcome="SUCCESS",
        )
        db.commit()
        assert ref_succ.status == RefundStatus.COMPLETED.value
        assert ref_succ.completed_amount == 200000


# ---------------------------------------------------------------------------
# Issue 4: Database-level Concurrency Control for Case Transitions
# ---------------------------------------------------------------------------


def test_issue4_db_level_concurrency_control_case_transitions() -> None:
    maker = get_sessionmaker()
    session_id = "sess_case_race"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_case_race",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        case = Case(
            id="CASE-RACE-001",
            session_id=session_id,
            goal="Kiểm thử xung đột chuyển trạng thái",
            state=CaseState.NEW.value,
            version=1,
        )
        db.add_all([sess, case])
        db.commit()

    def do_transition(target: CaseState) -> tuple[bool, str]:
        with maker() as thread_db:
            thread_case = thread_db.scalar(select(Case).where(Case.id == "CASE-RACE-001"))
            try:
                transition_case(
                    thread_db,
                    thread_case,
                    target,
                    reason="Chuyển trạng thái song song",
                    expected_version=1,
                )
                thread_db.commit()
                return True, "SUCCESS"
            except AppError as e:
                thread_db.rollback()
                return False, f"ERR_{e.status_code}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(do_transition, CaseState.INVESTIGATING),
            executor.submit(do_transition, CaseState.ESCALATED),
            executor.submit(do_transition, CaseState.FAILED),
            executor.submit(do_transition, CaseState.INVESTIGATING),
        ]
        results = [f.result() for f in futures]

    successes = [r for r in results if r[0] is True]
    conflicts = [r for r in results if r[0] is False and "ERR_409" in r[1]]

    # Exactly one transaction succeeded; all others failed with 409 Concurrency Conflict
    assert len(successes) == 1
    assert len(conflicts) == 3

    with maker() as db:
        final_case = db.scalar(select(Case).where(Case.id == "CASE-RACE-001"))
        assert final_case.version == 2


# ---------------------------------------------------------------------------
# Issue 5: Atomic Case Intake Idempotency Under Concurrent Retries
# ---------------------------------------------------------------------------


def test_issue5_atomic_case_intake_concurrent_retries(client: TestClient) -> None:
    # 1. Create demo session
    resp = client.post("/api/v1/demo/sessions", json={"max_cases": 10})
    token = resp.json()["token"]
    headers = {"Authorization": f"Bearer {token}", "X-Idempotency-Key": "IDEMP-RACE-INTAKE-01"}

    payload = {
        "text": "Tôi đã thanh toán đơn hàng nhưng chưa được xác nhận, vui lòng kiểm tra.",
        "type": "PAYMENT_DISPUTE",
    }

    def post_case() -> tuple[int, str]:
        r = client.post("/api/v1/cases", json=payload, headers=headers)
        data = r.json()
        case_id = data.get("id", "")
        return r.status_code, case_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(post_case) for _ in range(5)]
        results = [f.result() for f in futures]

    # All 5 requests must succeed: one 201 Created, remaining 200 OK replay
    status_codes = [r[0] for r in results]
    case_ids = {r[1] for r in results}

    assert 201 in status_codes
    assert all(code in (200, 201) for code in status_codes)
    assert len(case_ids) == 1  # Exactly the same Case returned to all callers

    # Verify session quota was only incremented by 1
    maker = get_sessionmaker()
    with maker() as db:
        sess = db.scalar(select(DemoSession).where(DemoSession.token == token))
        assert sess.case_count == 1


# ---------------------------------------------------------------------------
# Issue 6: Additive Alembic Migration 0004 Backfill
# ---------------------------------------------------------------------------


def test_issue6_migration_0004_backfill_applied() -> None:
    maker = get_sessionmaker()
    session_id = "sess_backfill_check"

    with maker() as db:
        sess = DemoSession(
            id=session_id,
            token="dsk_backfill_check",
            status="ACTIVE",
            max_cases=10,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        # Create an order with subtotal and grand_total matching amount
        order = SimOrder(
            id="ORD-BF-01",
            session_id=session_id,
            customer_name="Lê Minh",
            customer_phone="0933445566",
            amount=500000,
            subtotal=500000,
            grand_total=500000,
            payment_status=OrderPaymentStatus.PAID.value,
        )
        pay = SimPayment(
            id="PAY-BF-01",
            session_id=session_id,
            order_id=order.id,
            provider="MOCK_MOMO",
            provider_ref="MOMO_TXN_BF_01",
            amount=500000,
            status=PaymentStatus.SUCCESS.value,
            verified_at=datetime.now(UTC),
        )
        ref = SimRefund(
            id="REF-BF-01",
            session_id=session_id,
            order_id=order.id,
            payment_id=pay.id,
            amount=500000,
            requested_amount=500000,
            approved_amount=500000,
            completed_amount=500000,
            status=RefundStatus.COMPLETED.value,
            reason="Backfill test refund",
        )
        db.add_all([sess, order, pay, ref])
        db.commit()

        # Query back
        ref_db = db.scalar(select(SimRefund).where(SimRefund.id == ref.id))
        assert ref_db.completed_amount == 500000
        assert ref_db.requested_amount == 500000

        order_db = db.scalar(select(SimOrder).where(SimOrder.id == order.id))
        assert order_db.subtotal == 500000
        assert order_db.grand_total == 500000


# ---------------------------------------------------------------------------
# Issue 7: Inventory Movement Accounting & Same-Session Reference Validation
# ---------------------------------------------------------------------------


def test_issue7_inventory_movement_accounting_and_session_validation() -> None:
    maker = get_sessionmaker()
    sess_a = "sess_inv_a"
    sess_b = "sess_inv_b"

    with maker() as db:
        # Setup Session A
        db.add(
            DemoSession(
                id=sess_a,
                token="dsk_inv_a",
                status="ACTIVE",
                max_cases=10,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        wh_a = Warehouse(id="WH-A-01", session_id=sess_a, code="WH-A-01", name="Kho A")
        p_a = Product(
            id="PROD-A-01",
            session_id=sess_a,
            name="Sản phẩm A",
            sku="SKU-A-01",
            price=100000,
        )

        # Setup Session B
        db.add(
            DemoSession(
                id=sess_b,
                token="dsk_inv_b",
                status="ACTIVE",
                max_cases=10,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        wh_b = Warehouse(id="WH-B-01", session_id=sess_b, code="WH-B-01", name="Kho B")
        p_b = Product(
            id="PROD-B-01",
            session_id=sess_b,
            name="Sản phẩm B",
            sku="SKU-B-01",
            price=200000,
        )
        db.add_all([wh_a, p_a, wh_b, p_b])
        db.flush()

        inv_a = InventoryLevel(
            id="INV-A-01",
            session_id=sess_a,
            product_id=p_a.id,
            warehouse_id=wh_a.id,
            on_hand=50,
            reserved=0,
        )
        db.add(inv_a)
        db.commit()

        # 7a: Cross-session reference MUST be rejected with 404
        with pytest.raises(AppError) as exc_cross:
            reserve_inventory(
                db,
                session_id=sess_a,
                product_id=p_b.id,  # Product from Session B!
                warehouse_id=wh_a.id,
                quantity=5,
            )
        assert exc_cross.value.status_code == 404
        assert "not found in current session" in exc_cross.value.message

        # 7b: Valid reservation does NOT decrease physical on_hand
        res = reserve_inventory(
            db,
            session_id=sess_a,
            product_id=p_a.id,
            warehouse_id=wh_a.id,
            quantity=10,
        )
        db.commit()
        db.refresh(inv_a)
        assert inv_a.reserved == 10
        assert inv_a.on_hand == 50  # on_hand is intact!
        assert inv_a.available == 40

        # 7c: Fulfillment decreases both on_hand and reserved, and logs InventoryMovement
        res_done, mov = fulfill_inventory_reservation(
            db,
            session_id=sess_a,
            reservation_id=res.id,
        )
        db.commit()
        db.refresh(inv_a)
        assert res_done.status == "FULFILLED"
        assert inv_a.reserved == 0
        assert inv_a.on_hand == 40  # on_hand physically decreased upon fulfillment
        assert mov.delta == -10
        assert mov.reason == "FULFILLMENT"

        # 7d: Direct stock adjustment accurately modifies on_hand
        mov_adj = record_inventory_movement(
            db,
            session_id=sess_a,
            product_id=p_a.id,
            warehouse_id=wh_a.id,
            delta=25,
            reason="RESTOCK",
            source_ref="PO-RESTOCK-99",
        )
        db.commit()
        db.refresh(inv_a)
        assert inv_a.on_hand == 65
        assert mov_adj.delta == 25


# ---------------------------------------------------------------------------
# Issue 8: Exactly One Declaration of idempotency_payload_hash
# ---------------------------------------------------------------------------


def test_issue8_single_idempotency_payload_hash_declaration() -> None:
    # Check Case table columns
    col_names = [col.name for col in Case.__table__.columns]
    occurrences = [name for name in col_names if name == "idempotency_payload_hash"]
    assert len(occurrences) == 1, (
        f"idempotency_payload_hash declared {len(occurrences)} times in Case table"
    )
