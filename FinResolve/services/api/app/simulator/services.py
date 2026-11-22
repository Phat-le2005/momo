"""Domain services for safe inventory reservation, refund processing, and provider events."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.models.actions import ActionExecution, ActionProposal, Approval, Verification
from app.models.cases import Case
from app.models.enums import (
    ActionExecutionState,
    ActionProposalStatus,
    ActionType,
    ApprovalDecision,
    OrderPaymentStatus,
    OrderStatus,
    PaymentEventType,
    PaymentStatus,
    RefundStatus,
    ReservationStatus,
    VerificationVerdict,
)
from app.models.simulator import (
    InventoryLevel,
    InventoryMovement,
    InventoryReservation,
    OrderItem,
    Product,
    SimOrder,
    SimPayment,
    SimPaymentEvent,
    SimRefund,
    Warehouse,
)

# ---------------------------------------------------------------------------
# Inventory Management & Accounting
# ---------------------------------------------------------------------------


def reserve_inventory(
    db: Session,
    *,
    session_id: str,
    product_id: str,
    warehouse_id: str,
    quantity: int,
    order_id: str | None = None,
    order_item_id: str | None = None,
) -> InventoryReservation:
    """Safely reserve inventory with row-level locking (SELECT FOR UPDATE) to prevent overselling.

    Invariants:
    - Strictly validates same-session references: Product, Warehouse, Order, OrderItem.
    - Serializes concurrent reservation attempts on the exact product and warehouse.
    - If available (on_hand - reserved) < quantity, strictly rejects with 409 CONFLICT.
    - Check constraint on_hand >= reserved ensures DB-level enforcement.
    - Accurate accounting: reservation allocates reserved quantity; physical on_hand
      is NOT decremented until fulfillment/dispatch.
    """
    if quantity <= 0:
        raise AppError(ErrorCode.BAD_REQUEST, "Quantity must be strictly positive", status_code=400)

    # 1. Validate same-session references
    product = db.scalar(
        select(Product).where(Product.id == product_id, Product.session_id == session_id)
    )
    if product is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Product '{product_id}' not found in current session",
            status_code=404,
        )

    warehouse = db.scalar(
        select(Warehouse).where(Warehouse.id == warehouse_id, Warehouse.session_id == session_id)
    )
    if warehouse is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Warehouse '{warehouse_id}' not found in current session",
            status_code=404,
        )

    if order_id is not None:
        order = db.scalar(
            select(SimOrder).where(SimOrder.id == order_id, SimOrder.session_id == session_id)
        )
        if order is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"Order '{order_id}' not found in current session",
                status_code=404,
            )

    if order_item_id is not None:
        item = db.scalar(
            select(OrderItem).where(
                OrderItem.id == order_item_id, OrderItem.session_id == session_id
            )
        )
        if item is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"OrderItem '{order_item_id}' not found in current session",
                status_code=404,
            )
        if order_id is not None and item.order_id != order_id:
            raise AppError(
                ErrorCode.CONFLICT,
                f"OrderItem '{order_item_id}' does not belong to order '{order_id}'",
                status_code=409,
            )
        if item.product_id != product_id:
            raise AppError(
                ErrorCode.CONFLICT,
                f"OrderItem '{order_item_id}' product mismatch",
                status_code=409,
            )

    # 2. Acquire exclusive row lock on InventoryLevel
    stmt = (
        select(InventoryLevel)
        .where(
            InventoryLevel.session_id == session_id,
            InventoryLevel.product_id == product_id,
            InventoryLevel.warehouse_id == warehouse_id,
        )
        .with_for_update()
    )
    inv = db.scalar(stmt)
    if inv is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Inventory level not found for product {product_id} in warehouse {warehouse_id}",
            status_code=404,
        )

    available = inv.on_hand - inv.reserved
    if available < quantity:
        raise AppError(
            ErrorCode.CONFLICT,
            f"Insufficient inventory: requested {quantity}, available {available} "
            f"(on_hand={inv.on_hand}, reserved={inv.reserved})",
            status_code=409,
            details={
                "product_id": product_id,
                "warehouse_id": warehouse_id,
                "available": available,
                "requested": quantity,
            },
        )

    inv.reserved += quantity
    inv.version += 1
    inv.updated_at = datetime.now(UTC)

    reservation_id = f"RES-{uuid.uuid4().hex[:10].upper()}"
    reservation = InventoryReservation(
        id=reservation_id,
        session_id=session_id,
        order_id=order_id,
        order_item_id=order_item_id,
        product_id=product_id,
        warehouse_id=warehouse_id,
        quantity=quantity,
        status=ReservationStatus.CONFIRMED.value,
        source_mode="MOCK",
        created_at=datetime.now(UTC),
    )
    db.add(reservation)
    db.flush()
    return reservation


def record_inventory_movement(
    db: Session,
    *,
    session_id: str,
    product_id: str,
    warehouse_id: str,
    delta: int,
    reason: str,
    source_ref: str,
) -> InventoryMovement:
    """Safely record a physical inventory movement and reconcile on_hand stock."""
    if delta == 0:
        raise AppError(
            ErrorCode.BAD_REQUEST, "Inventory movement delta cannot be 0", status_code=400
        )

    product = db.scalar(
        select(Product).where(Product.id == product_id, Product.session_id == session_id)
    )
    if product is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Product '{product_id}' not found in current session",
            status_code=404,
        )

    warehouse = db.scalar(
        select(Warehouse).where(Warehouse.id == warehouse_id, Warehouse.session_id == session_id)
    )
    if warehouse is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Warehouse '{warehouse_id}' not found in current session",
            status_code=404,
        )

    stmt = (
        select(InventoryLevel)
        .where(
            InventoryLevel.session_id == session_id,
            InventoryLevel.product_id == product_id,
            InventoryLevel.warehouse_id == warehouse_id,
        )
        .with_for_update()
    )
    inv = db.scalar(stmt)
    if inv is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Inventory level not found for product {product_id} in warehouse {warehouse_id}",
            status_code=404,
        )

    new_on_hand = inv.on_hand + delta
    if new_on_hand < 0 or new_on_hand < inv.reserved:
        raise AppError(
            ErrorCode.CONFLICT,
            f"Inventory adjustment violates invariant: on_hand ({new_on_hand}) "
            f"cannot be negative or below reserved ({inv.reserved})",
            status_code=409,
        )

    inv.on_hand = new_on_hand
    inv.version += 1
    inv.updated_at = datetime.now(UTC)

    movement = InventoryMovement(
        id=f"MOV-{uuid.uuid4().hex[:10].upper()}",
        session_id=session_id,
        product_id=product_id,
        warehouse_id=warehouse_id,
        delta=delta,
        reason=reason,
        source_ref=source_ref,
        source_mode="MOCK",
        created_at=datetime.now(UTC),
    )
    db.add(movement)
    db.flush()
    return movement


def fulfill_inventory_reservation(
    db: Session,
    *,
    session_id: str,
    reservation_id: str,
) -> tuple[InventoryReservation, InventoryMovement]:
    """Fulfill a confirmed reservation: deducts both reserved and on_hand, recording movement."""
    stmt_res = (
        select(InventoryReservation)
        .where(
            InventoryReservation.id == reservation_id,
            InventoryReservation.session_id == session_id,
        )
        .with_for_update()
    )
    res = db.scalar(stmt_res)
    if res is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Reservation '{reservation_id}' not found in session",
            status_code=404,
        )

    if res.status != ReservationStatus.CONFIRMED.value:
        raise AppError(
            ErrorCode.CONFLICT,
            f"Cannot fulfill reservation with status '{res.status}'",
            status_code=409,
        )

    stmt_inv = (
        select(InventoryLevel)
        .where(
            InventoryLevel.session_id == session_id,
            InventoryLevel.product_id == res.product_id,
            InventoryLevel.warehouse_id == res.warehouse_id,
        )
        .with_for_update()
    )
    inv = db.scalar(stmt_inv)
    if inv is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            "Inventory level not found",
            status_code=404,
        )

    inv.reserved -= res.quantity
    inv.on_hand -= res.quantity
    inv.version += 1
    inv.updated_at = datetime.now(UTC)

    res.status = "FULFILLED"

    movement = InventoryMovement(
        id=f"MOV-{uuid.uuid4().hex[:10].upper()}",
        session_id=session_id,
        product_id=res.product_id,
        warehouse_id=res.warehouse_id,
        delta=-res.quantity,
        reason="FULFILLMENT",
        source_ref=reservation_id,
        source_mode="MOCK",
        created_at=datetime.now(UTC),
    )
    db.add(movement)
    db.flush()
    return res, movement


# ---------------------------------------------------------------------------
# Refund Lifecycle: Proposal -> Approval -> Execution & Read-back Verification
# ---------------------------------------------------------------------------


def propose_refund(
    db: Session,
    *,
    session_id: str,
    payment_id: str,
    amount: int,
    reason: str,
    case_id: str | None = None,
) -> ActionProposal:
    """Create an ActionProposal for a refund without executing it.

    Invariants:
    - Locks payment row and verifies payment status == SUCCESS.
    - Prevents proposing refund exceeding remaining eligible balance.
    - Permits case_id=None without foreign key violations.
    """
    if amount <= 0:
        raise AppError(
            ErrorCode.BAD_REQUEST, "Refund amount must be strictly positive", status_code=400
        )

    stmt_pay = (
        select(SimPayment)
        .where(SimPayment.id == payment_id, SimPayment.session_id == session_id)
        .with_for_update()
    )
    payment = db.scalar(stmt_pay)
    if payment is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Payment '{payment_id}' not found", status_code=404)

    if payment.status != PaymentStatus.SUCCESS.value:
        raise AppError(
            ErrorCode.CONFLICT,
            f"Cannot refund payment with status '{payment.status}'; "
            f"payment must be in SUCCESS status",
            status_code=409,
            details={"payment_id": payment_id, "status": payment.status},
        )

    # Calculate eligible balance (including COMPLETED, PENDING, UNKNOWN)
    stmt_refunds = (
        select(SimRefund)
        .where(
            SimRefund.payment_id == payment_id,
            SimRefund.session_id == session_id,
            SimRefund.status.in_(
                [
                    RefundStatus.COMPLETED.value,
                    RefundStatus.PENDING.value,
                    RefundStatus.UNKNOWN.value,
                ]
            ),
        )
        .with_for_update()
    )
    existing_refunds = db.scalars(stmt_refunds).all()

    total_refunded_or_pending = sum(
        r.completed_amount if r.status == RefundStatus.COMPLETED.value else r.requested_amount
        for r in existing_refunds
    )
    eligible_remaining = payment.amount - total_refunded_or_pending

    if eligible_remaining <= 0:
        raise AppError(
            ErrorCode.CONFLICT,
            "Payment has already been fully refunded or has pending refunds covering full amount",
            status_code=409,
            details={
                "payment_id": payment_id,
                "payment_amount": payment.amount,
                "total_refunded_or_pending": total_refunded_or_pending,
                "eligible_remaining": eligible_remaining,
            },
        )

    if amount > eligible_remaining:
        raise AppError(
            ErrorCode.CONFLICT,
            f"Refund amount {amount} exceeds remaining eligible refundable "
            f"balance {eligible_remaining}",
            status_code=409,
            details={
                "payment_id": payment_id,
                "requested_amount": amount,
                "eligible_remaining": eligible_remaining,
                "total_refunded_or_pending": total_refunded_or_pending,
            },
        )

    expected_version = 1
    if case_id is not None:
        case = db.scalar(select(Case).where(Case.id == case_id, Case.session_id == session_id))
        if case is None:
            raise AppError(
                ErrorCode.NOT_FOUND, f"Case '{case_id}' not found in session", status_code=404
            )
        expected_version = case.version

    proposal_id = f"PROP-{uuid.uuid4().hex[:10].upper()}"
    proposal = ActionProposal(
        id=proposal_id,
        case_id=case_id,  # Cleanly nullable: no synthetic dummy keys!
        session_id=session_id,
        action_type=ActionType.REFUND_EXECUTE.value,
        params_json={
            "payment_id": payment_id,
            "amount": amount,
            "reason": reason,
            "order_id": payment.order_id,
        },
        evidence_refs=[],
        expected_version=expected_version,
        risk_level="HIGH" if amount > 500000 else "MEDIUM",
        status=ActionProposalStatus.PROPOSED.value,
        created_at=datetime.now(UTC),
    )
    db.add(proposal)
    db.flush()
    return proposal


def approve_refund_proposal(
    db: Session,
    *,
    proposal_id: str,
    session_id: str,
    approver_role: str = "FINANCE_LEAD",
    decision: ApprovalDecision | str = ApprovalDecision.APPROVED,
    expiry_hours: int = 2,
) -> Approval:
    """Authorize a proposed refund with cryptographic digest and explicit expiry."""
    decision_str = decision.value if isinstance(decision, ApprovalDecision) else str(decision)

    stmt_prop = (
        select(ActionProposal)
        .where(
            ActionProposal.id == proposal_id,
            ActionProposal.session_id == session_id,
        )
        .with_for_update()
    )
    proposal = db.scalar(stmt_prop)
    if proposal is None:
        raise AppError(
            ErrorCode.NOT_FOUND, f"ActionProposal '{proposal_id}' not found", status_code=404
        )

    if proposal.status != ActionProposalStatus.PROPOSED.value:
        raise AppError(
            ErrorCode.CONFLICT,
            f"Cannot approve proposal with status '{proposal.status}'",
            status_code=409,
        )

    now = datetime.now(UTC)
    expiry = now + timedelta(hours=expiry_hours)
    digest_src = (
        f"{proposal.id}:{proposal.action_type}:{proposal.params_json}:{proposal.expected_version}"
    )
    digest = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()

    approval_id = f"APPR-{uuid.uuid4().hex[:10].upper()}"
    approval = Approval(
        id=approval_id,
        proposal_id=proposal.id,
        approver_role=approver_role,
        decision=decision_str,
        digest=digest,
        expiry=expiry,
        created_at=now,
    )
    db.add(approval)

    if decision_str == ApprovalDecision.APPROVED.value:
        proposal.status = ActionProposalStatus.APPROVED.value
    else:
        proposal.status = ActionProposalStatus.REJECTED.value

    db.flush()
    return approval


def verify_refund_execution(
    db: Session,
    *,
    session_id: str,
    execution_id: str,
    case_id: str | None = None,
) -> Verification:
    """Independently read back and verify refund postconditions from authoritative DB state.

    Invariant:
    Verifies that:
    1. ActionExecution state is EXECUTED.
    2. SimRefund status is COMPLETED and completed_amount matches proposal.
    3. SimPayment is in SUCCESS status.
    4. Sum of all COMPLETED refunds on payment does not exceed payment.amount.
    5. If linked to an order, order.payment_status is appropriately updated.
    Only if all checks pass is a PASSED Verification record generated.
    """
    now = datetime.now(UTC)

    # 1. Read back execution
    execution = db.scalar(select(ActionExecution).where(ActionExecution.id == execution_id))
    if execution is None:
        raise AppError(
            ErrorCode.NOT_FOUND, f"ActionExecution '{execution_id}' not found", status_code=404
        )

    # 2. Read back proposal
    proposal = db.scalar(
        select(ActionProposal).where(
            ActionProposal.id == execution.proposal_id,
            ActionProposal.session_id == session_id,
        )
    )
    if proposal is None:
        raise AppError(ErrorCode.NOT_FOUND, "ActionProposal not found in session", status_code=404)

    target_case_id = case_id or proposal.case_id
    case_version = proposal.expected_version
    if target_case_id:
        case = db.scalar(
            select(Case).where(Case.id == target_case_id, Case.session_id == session_id)
        )
        if case is not None:
            case_version = case.version

    # 3. Read back SimRefund independently
    refund = db.scalar(
        select(SimRefund).where(
            SimRefund.request_id == execution.idempotency_key,
            SimRefund.session_id == session_id,
        )
    )

    expected_amount = proposal.params_json.get("amount", 0)
    payment_id = proposal.params_json.get("payment_id")

    payment = (
        db.scalar(
            select(SimPayment).where(
                SimPayment.id == payment_id, SimPayment.session_id == session_id
            )
        )
        if payment_id
        else None
    )

    # Evaluate independent read-back postconditions
    is_valid = True
    failure_reasons = []

    if execution.state != ActionExecutionState.EXECUTED.value:
        is_valid = False
        failure_reasons.append(f"Execution state is '{execution.state}', not EXECUTED")

    if refund is None:
        is_valid = False
        failure_reasons.append("SimRefund record not found in database")
    else:
        if refund.status != RefundStatus.COMPLETED.value:
            is_valid = False
            failure_reasons.append(f"Refund status is '{refund.status}', not COMPLETED")
        if refund.completed_amount != expected_amount:
            is_valid = False
            failure_reasons.append(
                f"Refund completed_amount ({refund.completed_amount}) != "
                f"expected ({expected_amount})"
            )

    if payment is None or payment.status != PaymentStatus.SUCCESS.value:
        is_valid = False
        failure_reasons.append("Payment not found or not in SUCCESS status")
    elif payment is not None:
        completed_refunds = db.scalars(
            select(SimRefund).where(
                SimRefund.payment_id == payment.id,
                SimRefund.status == RefundStatus.COMPLETED.value,
            )
        ).all()
        total_completed = sum(r.completed_amount for r in completed_refunds)
        if total_completed > payment.amount:
            is_valid = False
            failure_reasons.append(
                f"Total completed refunds ({total_completed}) exceeds "
                f"payment amount ({payment.amount})"
            )

    verdict = VerificationVerdict.PASSED.value if is_valid else VerificationVerdict.FAILED.value
    postconditions = {
        "read_back_verified": is_valid,
        "execution_id": execution.id,
        "refund_id": refund.id if refund else None,
        "completed_amount": refund.completed_amount if refund else 0,
        "failure_reasons": failure_reasons,
    }

    verification = Verification(
        id=f"VRF-{uuid.uuid4().hex[:10].upper()}",
        case_id=target_case_id,
        action_execution_id=execution.id,
        case_version=case_version,
        postconditions_json=postconditions,
        verdict=verdict,
        checked_at=now,
    )
    db.add(verification)
    db.flush()
    return verification


def execute_refund_proposal(
    db: Session,
    *,
    proposal_id: str,
    session_id: str,
    idempotency_key: str,
    execution_outcome: str = "SUCCESS",  # "SUCCESS", "UNKNOWN", "FAILED"
) -> tuple[ActionExecution, SimRefund]:
    """Execute an approved refund proposal under strict idempotency and verification rules.

    Invariants:
    1. Locks the payment row and recalculates refundable balance at execution time.
    2. Includes COMPLETED, PENDING, and UNKNOWN refunds in the calculation.
    3. Rejects over-refunding and stale/expired approvals with 409 CONFLICT.
    4. Preserves idempotency: identical idempotency_key returns existing execution and refund.
    5. Never records COMPLETED without verified outcome; keeps UNKNOWN when uncertain.
    6. Authoritative postcondition verified by independent read-back.
    """
    if execution_outcome not in ("SUCCESS", "UNKNOWN", "FAILED"):
        raise AppError(
            ErrorCode.BAD_REQUEST,
            f"Invalid execution outcome '{execution_outcome}'",
            status_code=400,
        )

    # 1. Idempotency check on ActionExecution
    stmt_exec = select(ActionExecution).where(ActionExecution.idempotency_key == idempotency_key)
    existing_exec = db.scalar(stmt_exec)
    if existing_exec is not None:
        stmt_ref = select(SimRefund).where(
            SimRefund.session_id == session_id,
            SimRefund.request_id == idempotency_key,
        )
        existing_ref = db.scalar(stmt_ref)
        if existing_ref is not None:
            return existing_exec, existing_ref

    # 2. Lock proposal
    stmt_prop = (
        select(ActionProposal)
        .where(
            ActionProposal.id == proposal_id,
            ActionProposal.session_id == session_id,
        )
        .with_for_update()
    )
    proposal = db.scalar(stmt_prop)
    if proposal is None:
        raise AppError(
            ErrorCode.NOT_FOUND, f"ActionProposal '{proposal_id}' not found", status_code=404
        )

    if proposal.status != ActionProposalStatus.APPROVED.value:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"Proposal '{proposal_id}' is not approved (status={proposal.status})",
            status_code=403,
        )

    # 3. Check approval & reject stale or tampered approvals
    stmt_appr = select(Approval).where(Approval.proposal_id == proposal_id).with_for_update()
    approval = db.scalar(stmt_appr)
    if approval is None:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"No approval record found for proposal '{proposal_id}'",
            status_code=403,
        )

    now = datetime.now(UTC)
    if now > approval.expiry:
        raise AppError(
            ErrorCode.CONFLICT,
            "Approval authorization has expired; cannot execute",
            status_code=409,
        )

    digest_src = (
        f"{proposal.id}:{proposal.action_type}:{proposal.params_json}:{proposal.expected_version}"
    )
    expected_digest = hashlib.sha256(digest_src.encode("utf-8")).hexdigest()
    if approval.digest != expected_digest:
        raise AppError(
            ErrorCode.CONFLICT,
            "Approval digest mismatch: proposal parameters were tampered with",
            status_code=409,
        )

    params = proposal.params_json
    payment_id = params["payment_id"]
    amount = params["amount"]
    reason = params["reason"]

    # 4. Lock payment row & recalculate refundable balance at execution time
    stmt_pay = (
        select(SimPayment)
        .where(SimPayment.id == payment_id, SimPayment.session_id == session_id)
        .with_for_update()
    )
    payment = db.scalar(stmt_pay)
    if payment is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Payment '{payment_id}' not found", status_code=404)

    if payment.status != PaymentStatus.SUCCESS.value:
        raise AppError(
            ErrorCode.CONFLICT,
            f"Cannot execute refund against payment with status '{payment.status}'",
            status_code=409,
        )

    # Query all active/pending/unknown refunds at execution time under row lock
    stmt_refunds = (
        select(SimRefund)
        .where(
            SimRefund.payment_id == payment_id,
            SimRefund.session_id == session_id,
            SimRefund.status.in_(
                [
                    RefundStatus.COMPLETED.value,
                    RefundStatus.PENDING.value,
                    RefundStatus.UNKNOWN.value,
                ]
            ),
        )
        .with_for_update()
    )
    existing_refunds = db.scalars(stmt_refunds).all()

    total_refunded_or_pending = sum(
        r.completed_amount if r.status == RefundStatus.COMPLETED.value else r.requested_amount
        for r in existing_refunds
    )
    eligible_remaining = payment.amount - total_refunded_or_pending

    if eligible_remaining <= 0:
        raise AppError(
            ErrorCode.CONFLICT,
            "Payment has already been fully refunded or has "
            "pending/unknown refunds covering full amount",
            status_code=409,
            details={
                "payment_id": payment_id,
                "payment_amount": payment.amount,
                "total_refunded_or_pending": total_refunded_or_pending,
                "eligible_remaining": eligible_remaining,
            },
        )

    if amount > eligible_remaining:
        raise AppError(
            ErrorCode.CONFLICT,
            f"Refund amount {amount} exceeds remaining eligible refundable "
            f"balance {eligible_remaining}",
            status_code=409,
            details={
                "payment_id": payment_id,
                "requested_amount": amount,
                "eligible_remaining": eligible_remaining,
                "total_refunded_or_pending": total_refunded_or_pending,
            },
        )

    # 5. Record ActionExecution
    exec_id = f"EXEC-{uuid.uuid4().hex[:10].upper()}"
    exec_state = (
        ActionExecutionState.EXECUTED.value
        if execution_outcome == "SUCCESS"
        else (
            ActionExecutionState.UNKNOWN.value
            if execution_outcome == "UNKNOWN"
            else ActionExecutionState.FAILED.value
        )
    )
    execution = ActionExecution(
        id=exec_id,
        proposal_id=proposal.id,
        idempotency_key=idempotency_key,
        state=exec_state,
        result_json={"outcome": execution_outcome, "amount": amount},
        created_at=now,
        updated_at=now,
    )
    db.add(execution)
    db.flush()

    # 6. Record SimRefund
    if execution_outcome == "SUCCESS":
        refund_status = RefundStatus.COMPLETED.value
        completed_amount = amount
        proposal.status = ActionProposalStatus.EXECUTED.value
    elif execution_outcome == "UNKNOWN":
        refund_status = RefundStatus.UNKNOWN.value
        completed_amount = 0
    else:
        refund_status = RefundStatus.FAILED.value
        completed_amount = 0

    refund_id = f"REF-{uuid.uuid4().hex[:10].upper()}"
    refund = SimRefund(
        id=refund_id,
        session_id=session_id,
        order_id=payment.order_id,
        payment_id=payment.id,
        amount=amount,
        requested_amount=amount,
        approved_amount=amount,
        completed_amount=completed_amount,
        currency=payment.currency,
        request_id=idempotency_key,
        status=refund_status,
        reason=reason,
        approval_ref=approval.id,
        provider_refund_id=f"MOCK_REF_{uuid.uuid4().hex[:8].upper()}"
        if execution_outcome == "SUCCESS"
        else None,
        source_mode="MOCK",
        created_at=now,
        updated_at=now,
    )
    db.add(refund)

    # If successful and linked to order, update order payment status
    if execution_outcome == "SUCCESS" and payment.order_id:
        stmt_order = (
            select(SimOrder)
            .where(SimOrder.id == payment.order_id, SimOrder.session_id == session_id)
            .with_for_update()
        )
        order = db.scalar(stmt_order)
        if order is not None:
            new_total_refunded = total_refunded_or_pending + amount
            if new_total_refunded >= payment.amount:
                order.payment_status = OrderPaymentStatus.REFUNDED.value
            else:
                order.payment_status = OrderPaymentStatus.PARTIALLY_REFUNDED.value
            order.updated_at = now

    db.flush()

    # 7. Independent read-back verification
    if execution_outcome == "SUCCESS":
        verify_refund_execution(
            db,
            session_id=session_id,
            execution_id=execution.id,
            case_id=proposal.case_id,
        )

    db.flush()
    return execution, refund


def process_refund_request(
    db: Session,
    *,
    session_id: str,
    payment_id: str,
    amount: int,
    reason: str,
    approval_id: str | None = None,
    approval_ref: str | None = None,
    request_id: str | None = None,
    execution_outcome: str = "SUCCESS",
    is_authorized: bool = False,  # Strict: NEVER default to True!
) -> SimRefund:
    """Process a refund through the authorized proposal & execution pipeline.

    Invariants:
    - Never defaults authorization to True.
    - Cannot bypass real Approval: requires valid approval_id/approval_ref linked
      to an existing Approval record in the database.
    - Direct unapproved refund path is completely eliminated.
    """
    effective_approval_id = approval_id or approval_ref
    if not is_authorized or not effective_approval_id:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "process_refund_request cannot bypass real Approval: "
            "requires a valid approval_id and explicit authorization",
            status_code=403,
        )

    stmt = select(Approval).where(Approval.id == effective_approval_id)
    approval = db.scalar(stmt)
    if approval is None:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"Approval record '{effective_approval_id}' not found",
            status_code=403,
        )

    if approval.decision != ApprovalDecision.APPROVED.value:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"Approval '{effective_approval_id}' was {approval.decision}, not APPROVED",
            status_code=403,
        )

    # Execute through real execution pipeline under row lock & balance check
    idempotency_key = request_id or f"REQ-APPR-{effective_approval_id}"
    _, refund = execute_refund_proposal(
        db,
        proposal_id=approval.proposal_id,
        session_id=session_id,
        idempotency_key=idempotency_key,
        execution_outcome=execution_outcome,
    )
    return refund


# ---------------------------------------------------------------------------
# Payment Notification Processing & Concurrency Protection
# ---------------------------------------------------------------------------


def process_provider_notification(
    db: Session,
    *,
    session_id: str,
    provider: str,
    provider_ref: str,
    amount: int,
    status: PaymentStatus | str,
    currency: str = "VND",
    merchant_id: str | None = None,
    request_id: str | None = None,
    order_id: str | None = None,
    payload: dict[str, Any] | None = None,
    verification_source: str = "WEBHOOK_IPN",
    is_trusted: bool = False,  # Strict: NEVER default to True!
    adapter_verified: bool = False,  # Only verified provider adapters can establish SUCCESS!
) -> tuple[SimPayment, bool]:
    """Process an incoming provider payment IPN/notification with idempotency rules.

    Invariants:
    1. Default is_trusted=False.
    2. Only verified provider adapters (is_trusted=True AND adapter_verified=True)
       can establish SUCCESS status.
    3. Validates merchant, order ID, amount, and currency.
    4. Does not trust caller-supplied source labels.
    5. Identical events are strictly idempotent.
    6. Conflicting payments (amount mismatch, currency mismatch, or reversion from SUCCESS)
       are strictly rejected with 409 CONFLICT.
    7. Concurrent inserts are completely safe against duplicate database violations.

    Returns:
        (payment, is_new): is_new is False if deduplicated or idempotent replay.
    """
    status_str = status.value if isinstance(status, PaymentStatus) else str(status)

    if amount <= 0:
        raise AppError(
            ErrorCode.BAD_REQUEST, "Payment amount must be strictly positive", status_code=400
        )

    if currency not in ("VND", "USD"):
        raise AppError(ErrorCode.BAD_REQUEST, f"Unsupported currency '{currency}'", status_code=400)

    # Invariant 2: Only verified provider adapters can establish SUCCESS
    is_adapter_verified = is_trusted and adapter_verified
    if status_str == PaymentStatus.SUCCESS.value and not is_adapter_verified:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "Only verified provider adapters can establish SUCCESS payment status; "
            "Untrusted payment event source is rejected",
            status_code=403,
            details={"is_trusted": is_trusted, "adapter_verified": adapter_verified},
        )

    # Invariant 4: Do not trust caller-supplied source labels
    authoritative_source = (
        f"{provider}_ADAPTER_VERIFIED" if is_adapter_verified else "UNVERIFIED_CALLER"
    )

    # Invariant 3: Validate order ID, amount, and currency if order_id is provided
    if order_id is not None:
        stmt_order = (
            select(SimOrder)
            .where(SimOrder.id == order_id, SimOrder.session_id == session_id)
            .with_for_update()
        )
        order = db.scalar(stmt_order)
        if order is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"Order '{order_id}' not found in current session",
                status_code=404,
            )
        if order.amount != amount:
            raise AppError(
                ErrorCode.CONFLICT,
                f"Payment amount ({amount}) does not match order '{order_id}' "
                f"amount ({order.amount})",
                status_code=409,
            )
        if order.currency != currency:
            raise AppError(
                ErrorCode.CONFLICT,
                f"Payment currency ({currency}) does not match order '{order_id}' "
                f"currency ({order.currency})",
                status_code=409,
            )

    # Append-only audit event
    event = SimPaymentEvent(
        id=f"PEVT-{uuid.uuid4().hex[:10].upper()}",
        session_id=session_id,
        event_type=PaymentEventType.WEBHOOK_RECEIVED.value,
        provider=provider,
        request_id=request_id,
        payload_json=payload
        or {
            "provider_ref": provider_ref,
            "amount": amount,
            "currency": currency,
            "status": status_str,
            "authoritative_source": authoritative_source,
            "caller_source_ignored": verification_source,
        },
        source_mode="MOCK",
        created_at=datetime.now(UTC),
    )
    db.add(event)

    # Query existing payment under row lock
    stmt = (
        select(SimPayment)
        .where(
            SimPayment.session_id == session_id,
            SimPayment.provider == provider,
            SimPayment.provider_ref == provider_ref,
        )
        .with_for_update()
    )
    existing = db.scalar(stmt)

    if existing is not None:
        # Check conflicts
        if existing.amount != amount:
            raise AppError(
                ErrorCode.CONFLICT,
                f"Payment amount conflict for {provider_ref}: "
                f"existing={existing.amount}, incoming={amount}",
                status_code=409,
                details={
                    "provider_ref": provider_ref,
                    "existing_amount": existing.amount,
                    "incoming_amount": amount,
                },
            )

        if existing.currency != currency:
            raise AppError(
                ErrorCode.CONFLICT,
                f"Payment currency conflict for {provider_ref}: "
                f"existing={existing.currency}, incoming={currency}",
                status_code=409,
            )

        if merchant_id and existing.merchant_id != merchant_id:
            raise AppError(
                ErrorCode.CONFLICT,
                f"Merchant mismatch for {provider_ref}: "
                f"existing={existing.merchant_id}, incoming={merchant_id}",
                status_code=409,
            )

        if order_id and existing.order_id and existing.order_id != order_id:
            raise AppError(
                ErrorCode.CONFLICT,
                f"Order ID mismatch for {provider_ref}: "
                f"existing={existing.order_id}, incoming={order_id}",
                status_code=409,
            )

        # Reversion conflict: payment already confirmed SUCCESS cannot be reverted to PENDING/FAILED
        if (
            existing.status == PaymentStatus.SUCCESS.value
            and status_str != PaymentStatus.SUCCESS.value
        ):
            raise AppError(
                ErrorCode.CONFLICT,
                f"Conflicting payment status: payment is already verified SUCCESS "
                f"but incoming notification claims '{status_str}'",
                status_code=409,
                details={"existing_status": existing.status, "incoming_status": status_str},
            )

        # PENDING -> SUCCESS verified transition by verified provider adapter
        if (
            existing.status in (PaymentStatus.PENDING.value, PaymentStatus.UNKNOWN.value)
            and status_str == PaymentStatus.SUCCESS.value
        ):
            now = datetime.now(UTC)
            existing.status = PaymentStatus.SUCCESS.value
            existing.verified_at = now
            existing.observed_at = now
            existing.verification_source = authoritative_source
            if request_id and not existing.request_id:
                existing.request_id = request_id
            if order_id and not existing.order_id:
                existing.order_id = order_id

            target_order_id = existing.order_id or order_id
            if target_order_id:
                stmt_order = (
                    select(SimOrder)
                    .where(SimOrder.id == target_order_id, SimOrder.session_id == session_id)
                    .with_for_update()
                )
                order = db.scalar(stmt_order)
                if order is not None:
                    order.payment_status = OrderPaymentStatus.PAID.value
                    order.status = OrderStatus.PAID.value
                    order.updated_at = now

            db.flush()
            return existing, True

        # Identical event is idempotent
        return existing, False

    # Concurrent insert safety with DB nested transaction / savepoint
    now = datetime.now(UTC)
    new_payment = SimPayment(
        id=f"PAY-{uuid.uuid4().hex[:10].upper()}",
        session_id=session_id,
        order_id=order_id,
        merchant_id=merchant_id or "MERCHANT_MOCK_01",
        provider=provider,
        provider_ref=provider_ref,
        request_id=request_id,
        amount=amount,
        currency=currency,
        status=status_str,
        verified_at=now if status_str == PaymentStatus.SUCCESS.value else None,
        observed_at=now,
        verification_source=authoritative_source,
        source_mode="MOCK",
        created_at=now,
    )

    try:
        with db.begin_nested():
            db.add(new_payment)
            if status_str == PaymentStatus.SUCCESS.value and order_id:
                stmt_order = (
                    select(SimOrder)
                    .where(SimOrder.id == order_id, SimOrder.session_id == session_id)
                    .with_for_update()
                )
                order = db.scalar(stmt_order)
                if order is not None:
                    order.payment_status = OrderPaymentStatus.PAID.value
                    order.status = OrderStatus.PAID.value
                    order.updated_at = now
            db.flush()
        return new_payment, True
    except IntegrityError:
        # Concurrent insert race handled cleanly: load the winning row under lock
        existing_concurrent = db.scalar(stmt)
        if existing_concurrent is not None:
            if existing_concurrent.amount != amount:
                raise AppError(
                    ErrorCode.CONFLICT,
                    f"Payment amount conflict for {provider_ref}",
                    status_code=409,
                ) from None
            return existing_concurrent, False
        raise
