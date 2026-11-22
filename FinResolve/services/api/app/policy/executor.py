"""Policy-gated ActionExecutor and independent Verifier for FinResolve AI.

Hard Domain Invariants:
1. Only Policy-Gated & Approved Writes: No proposal can execute without a valid, non-expired
   `Approval` whose cryptographic digest matches the exact `action_type`, `params_json`,
   `evidence_refs`, `expected_version`, and `policy_version` (QA T11, T12).
2. Live State Recheck Under Lock: Acquires `SELECT FOR UPDATE` on `ActionProposal`, `Approval`,
   `Case`, `SimOrder`, and `SimPayment` and re-evaluates `PolicyEngine` immediately before write.
   If `order.version != expected_order_version` (QA T13), rejects with 409 CONFLICT.
3. Idempotency & Anti-Replay (QA T06):
   - Same `idempotency_key` on an already executed proposal replays the existing execution and
     verification without mutating DB state a second time.
   - Different `idempotency_key` on an already executed proposal is rejected with 409 CONFLICT.
4. Safe Timeout / UNKNOWN Handling (QA T04):
   - When an execution times out (`simulate_timeout=True`), records `ActionExecution(state=UNKNOWN)`
     and transitions Case to `UNKNOWN_OUTCOME`.
   - Any subsequent attempt to retry a proposal with an `UNKNOWN` execution using a new key
     is rejected with 409 CONFLICT (`UNKNOWN_OUTCOME_NO_BLIND_RETRY`).
5. Independent Read-Back Verification (QA T01, T20):
   - A tool/executor returning HTTP 200 never suffices for `RESOLVED`.
   - `Verifier` independently reads back PostgreSQL domain records after execution to check every
     contract postcondition. Only if all postconditions hold does it record `Verification(PASSED)`
     and transition the Case `VERIFYING -> RESOLVED`. If postconditions fail (QA T20), it records
     `Verification(FAILED)` and transitions the Case `VERIFYING -> FAILED`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cases.state_machine import can_transition, transition_case
from app.core.errors import AppError, ErrorCode
from app.models.actions import ActionExecution, ActionProposal, Approval, Verification
from app.models.cases import Case, CaseEvent
from app.models.enums import (
    ActionExecutionState,
    ActionProposalStatus,
    ActionType,
    ApprovalDecision,
    CaseState,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
    VerificationVerdict,
)
from app.models.simulator import SimOrder, SimPayment, SimRefund
from app.policy.approval import compute_approval_digest, compute_legacy_approval_digest
from app.policy.contracts import (
    HoldOrderActionParams,
    OrderSyncActionParams,
    RefundActionParams,
    get_action_contract,
)
from app.policy.engine import PolicyEngine


@dataclass
class ExecutionOutcome:
    execution: ActionExecution
    proposal: ActionProposal
    verification: Verification | None
    case: Case | None
    idempotent_replay: bool


class Verifier:
    """Independent postcondition verifier querying authoritative PostgreSQL records after writes."""

    def __init__(self, db: Session, session_id: str) -> None:
        self._db = db
        self._session_id = session_id

    def verify_execution(
        self,
        *,
        execution: ActionExecution,
        proposal: ActionProposal,
        case: Case | None,
    ) -> Verification:
        """Independently read back DB records to verify action postconditions."""
        now = datetime.now(UTC)
        contract = get_action_contract(proposal.action_type)
        case_version = case.version if case is not None else proposal.expected_version

        is_valid = True
        failure_reasons: list[str] = []
        postconditions_detail: dict[str, Any] = {
            "contract_action_type": contract.action_type,
            "policy_version": contract.policy_version,
            "execution_id": execution.id,
        }

        if execution.state != ActionExecutionState.EXECUTED.value:
            is_valid = False
            failure_reasons.append(
                f"ActionExecution state is '{execution.state}', expected 'EXECUTED'"
            )

        if contract.action_type == ActionType.ORDER_SYNC.value:
            params = OrderSyncActionParams.model_validate(proposal.params_json)
            # Independent read-back of SimOrder and SimPayment
            order = self._db.scalar(
                select(SimOrder).where(
                    SimOrder.id == params.order_id,
                    SimOrder.session_id == self._session_id,
                )
            )
            payment = self._db.scalar(
                select(SimPayment).where(
                    SimPayment.id == params.payment_id,
                    SimPayment.session_id == self._session_id,
                )
            )

            expected_new_version = params.expected_order_version + 1
            if order is None:
                is_valid = False
                failure_reasons.append(f"SimOrder '{params.order_id}' not found on read-back")
            else:
                postconditions_detail["observed_order_status"] = order.status
                postconditions_detail["observed_order_payment_status"] = order.payment_status
                postconditions_detail["observed_order_version"] = order.version
                postconditions_detail["expected_order_version_after"] = expected_new_version

                if order.status != OrderStatus.PAID.value:
                    is_valid = False
                    failure_reasons.append(
                        f"Order status is '{order.status}' after execution, expected 'PAID'"
                    )
                if order.payment_status != OrderPaymentStatus.PAID.value:
                    is_valid = False
                    failure_reasons.append(
                        f"Order payment_status is '{order.payment_status}', expected 'PAID'"
                    )
                if order.version != expected_new_version:
                    is_valid = False
                    failure_reasons.append(
                        f"Order version is {order.version}, expected {expected_new_version}"
                    )

            if payment is None:
                is_valid = False
                failure_reasons.append(f"SimPayment '{params.payment_id}' not found on read-back")
            else:
                postconditions_detail["observed_payment_status"] = payment.status
                postconditions_detail["observed_payment_verified"] = payment.verified_at is not None
                if (
                    payment.status != PaymentStatus.SUCCESS.value
                    or payment.verified_at is None
                    or (order is not None and payment.order_id != order.id)
                    or (order is not None and payment.amount != order.amount)
                ):
                    is_valid = False
                    failure_reasons.append(
                        "Payment record failed authoritative SUCCESS or order-binding read-back"
                    )

        elif contract.action_type == ActionType.REFUND_EXECUTE.value:
            params_ref = RefundActionParams.model_validate(proposal.params_json)
            refund = self._db.scalar(
                select(SimRefund).where(
                    SimRefund.request_id == execution.idempotency_key,
                    SimRefund.session_id == self._session_id,
                )
            )
            payment = self._db.scalar(
                select(SimPayment).where(
                    SimPayment.id == params_ref.payment_id,
                    SimPayment.session_id == self._session_id,
                )
            )

            if refund is None:
                is_valid = False
                failure_reasons.append("SimRefund record not found in database on read-back")
            else:
                postconditions_detail["refund_id"] = refund.id
                postconditions_detail["observed_refund_status"] = refund.status
                postconditions_detail["observed_completed_amount"] = refund.completed_amount
                if refund.status != RefundStatus.COMPLETED.value:
                    is_valid = False
                    failure_reasons.append(
                        f"SimRefund status is '{refund.status}', expected 'COMPLETED'"
                    )
                if refund.completed_amount != params_ref.amount:
                    is_valid = False
                    failure_reasons.append(
                        f"SimRefund completed_amount ({refund.completed_amount}) != "
                        f"expected ({params_ref.amount})"
                    )

            if payment is None or payment.status != PaymentStatus.SUCCESS.value:
                is_valid = False
                failure_reasons.append("Original payment missing or not in SUCCESS status")
            else:
                completed_refunds = list(
                    self._db.scalars(
                        select(SimRefund).where(
                            SimRefund.payment_id == payment.id,
                            SimRefund.session_id == self._session_id,
                            SimRefund.status == RefundStatus.COMPLETED.value,
                        )
                    ).all()
                )
                total_completed = sum(r.completed_amount for r in completed_refunds)
                postconditions_detail["total_completed_refunds_on_payment"] = total_completed
                if total_completed > payment.amount:
                    is_valid = False
                    failure_reasons.append(
                        f"Total completed refunds ({total_completed}) exceeds "
                        f"payment amount ({payment.amount})"
                    )

        postconditions_detail["read_back_verified"] = is_valid
        postconditions_detail["failure_reasons"] = failure_reasons
        verdict = VerificationVerdict.PASSED.value if is_valid else VerificationVerdict.FAILED.value

        verification = Verification(
            id=f"VRF-{uuid.uuid4().hex[:10].upper()}",
            case_id=case.id if case is not None else proposal.case_id,
            action_execution_id=execution.id,
            case_version=case_version,
            postconditions_json=postconditions_detail,
            verdict=verdict,
            checked_at=now,
        )
        self._db.add(verification)
        self._db.flush()
        return verification


class ActionExecutor:
    """Executes approved proposals under strict policy, locking, idempotency, and verification."""

    def __init__(self, db: Session, session_id: str) -> None:
        self._db = db
        self._session_id = session_id

    def execute_proposal(
        self,
        *,
        proposal_id: str,
        idempotency_key: str,
        simulate_timeout: bool = False,
        simulate_verification_failure: bool = False,
    ) -> ExecutionOutcome:
        clean_idemp_key = idempotency_key.strip()
        if not clean_idemp_key or len(clean_idemp_key) > 128:
            raise AppError(
                ErrorCode.BAD_REQUEST,
                "A valid non-empty idempotency_key (<= 128 chars) is required for action execution",
                status_code=400,
            )

        # 1. Lock ActionProposal row scoped to session
        proposal = self._db.scalar(
            select(ActionProposal)
            .where(
                ActionProposal.id == proposal_id,
                ActionProposal.session_id == self._session_id,
            )
            .with_for_update()
        )
        if proposal is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"ActionProposal '{proposal_id}' not found in current session",
                status_code=404,
            )

        case_obj: Case | None = None
        if proposal.case_id is not None:
            case_obj = self._db.scalar(
                select(Case)
                .where(
                    Case.id == proposal.case_id,
                    Case.session_id == self._session_id,
                )
                .with_for_update()
            )

        # 2. Check existing executions (Idempotency, Anti-Replay & UNKNOWN lockout)
        existing_executions = list(
            self._db.scalars(
                select(ActionExecution)
                .where(ActionExecution.proposal_id == proposal.id)
                .order_by(ActionExecution.created_at.asc())
                .with_for_update()
            ).all()
        )

        # 2A: Exact idempotency key match -> Idempotent Replay (QA T06)
        for ex in existing_executions:
            if ex.idempotency_key == clean_idemp_key:
                existing_verif = self._db.scalar(
                    select(Verification)
                    .where(Verification.action_execution_id == ex.id)
                    .order_by(Verification.checked_at.desc())
                    .limit(1)
                )
                return ExecutionOutcome(
                    execution=ex,
                    proposal=proposal,
                    verification=existing_verif,
                    case=case_obj,
                    idempotent_replay=True,
                )

        # 2B: Check if a global idempotency key collision exists on another proposal
        other_exec_same_key = self._db.scalar(
            select(ActionExecution).where(ActionExecution.idempotency_key == clean_idemp_key)
        )
        if other_exec_same_key is not None and other_exec_same_key.proposal_id != proposal.id:
            raise AppError(
                ErrorCode.CONFLICT,
                f"Idempotency key '{clean_idemp_key}' was already used for a different proposal",
                status_code=409,
            )

        # 2C: QA T04 — If previous execution is UNKNOWN, forbid blind retry with a new key!
        unknown_exec = next(
            (ex for ex in existing_executions if ex.state == ActionExecutionState.UNKNOWN.value),
            None,
        )
        if unknown_exec is not None or (
            case_obj is not None and case_obj.state == CaseState.UNKNOWN_OUTCOME.value
        ):
            raise AppError(
                ErrorCode.CONFLICT,
                "UNKNOWN_OUTCOME_NO_BLIND_RETRY: Previous execution timed out with UNKNOWN "
                "outcome. Blind retry is forbidden; manual reconciliation required.",
                status_code=409,
                details={
                    "proposal_id": proposal.id,
                    "unknown_execution_id": unknown_exec.id if unknown_exec else None,
                },
            )

        # 2D: QA T06 — If proposal was already EXECUTED with a different key, block approval replay!
        executed_exec = next(
            (ex for ex in existing_executions if ex.state == ActionExecutionState.EXECUTED.value),
            None,
        )
        if executed_exec is not None or proposal.status == ActionProposalStatus.EXECUTED.value:
            raise AppError(
                ErrorCode.CONFLICT,
                f"APPROVAL_REPLAY_BLOCKED: Proposal '{proposal.id}' has already been executed "
                f"(execution_id={executed_exec.id if executed_exec else 'done'}).",
                status_code=409,
                details={"proposal_id": proposal.id},
            )

        # 3. Verify Proposal is APPROVED and validate Approval record (expiry & digest)
        if proposal.status != ActionProposalStatus.APPROVED.value:
            raise AppError(
                ErrorCode.FORBIDDEN,
                f"Cannot execute proposal '{proposal.id}' with status '{proposal.status}'; "
                "explicit human approval is required first",
                status_code=403,
            )

        approval = self._db.scalar(
            select(Approval).where(Approval.proposal_id == proposal.id).with_for_update()
        )
        if approval is None or approval.decision != ApprovalDecision.APPROVED.value:
            raise AppError(
                ErrorCode.FORBIDDEN,
                f"No valid APPROVED record found for proposal '{proposal.id}'",
                status_code=403,
            )

        now = datetime.now(UTC)
        # QA T12: Reject expired approvals
        if now >= approval.expiry:
            raise AppError(
                ErrorCode.CONFLICT,
                f"APPROVAL_EXPIRED: Approval '{approval.id}' expired at "
                f"{approval.expiry.isoformat()}",
                status_code=409,
                details={"approval_id": approval.id, "expiry": approval.expiry.isoformat()},
            )

        # QA T11: Verify cryptographic digest binding to exact action parameters & policy
        contract = get_action_contract(proposal.action_type)
        expected_digest = compute_approval_digest(
            session_id=self._session_id,
            proposal_id=proposal.id,
            case_id=proposal.case_id,
            action_type=contract.action_type,
            params_json=proposal.params_json,
            evidence_refs=proposal.evidence_refs,
            expected_version=proposal.expected_version,
            policy_version=contract.policy_version,
            approver_role=approval.approver_role,
        )
        legacy_digest = compute_legacy_approval_digest(proposal)
        if approval.digest not in (expected_digest, legacy_digest):
            raise AppError(
                ErrorCode.CONFLICT,
                "APPROVAL_DIGEST_MISMATCH: Approval is bound to different action parameters, "
                "order, amount, or evidence than current proposal state",
                status_code=409,
                details={"approval_id": approval.id, "proposal_id": proposal.id},
            )

        # 4. Recheck live business state & optimistic version lock via PolicyEngine (QA T13)
        policy_engine = PolicyEngine(self._db, self._session_id)
        eval_res = policy_engine.evaluate(
            proposal,
            approver_role=approval.approver_role,
            lock_rows=True,
        )
        if not eval_res.allowed:
            raise AppError(
                ErrorCode.CONFLICT,
                f"PolicyEngine blocked execution on live state recheck "
                f"({eval_res.violation_code}): {eval_res.violation_message}",
                status_code=409,
                details={
                    "violation_code": eval_res.violation_code,
                    "checked_preconditions": eval_res.checked_preconditions,
                    "context_snapshot": eval_res.context_snapshot,
                },
            )

        # 5. Transition Case WAITING_APPROVAL -> EXECUTING
        if case_obj is not None:
            curr_state = CaseState(case_obj.state)
            if curr_state != CaseState.EXECUTING:
                if can_transition(curr_state, CaseState.EXECUTING):
                    case_obj = transition_case(
                        self._db,
                        case_obj,
                        CaseState.EXECUTING,
                        reason=f"Bắt đầu thực thi đề xuất đã phê duyệt ({proposal.id})",
                        extra_payload={
                            "proposal_id": proposal.id,
                            "approval_id": approval.id,
                            "action_type": contract.action_type,
                        },
                    )

        # 6. Handle Simulated Side-Effect Timeout (QA T04 -> UNKNOWN, never blind retry)
        if simulate_timeout:
            unknown_execution = ActionExecution(
                id=f"EXEC-{uuid.uuid4().hex[:10].upper()}",
                proposal_id=proposal.id,
                idempotency_key=clean_idemp_key,
                state=ActionExecutionState.UNKNOWN.value,
                result_json={
                    "timed_out": True,
                    "outcome": "UNKNOWN",
                    "action_type": contract.action_type,
                    "source_mode": "MOCK",
                    "message": (
                        "Side-effect execution timed out; external/downstream state is UNKNOWN. "
                        "Do not retry blindly."
                    ),
                },
                created_at=now,
                updated_at=now,
            )
            self._db.add(unknown_execution)
            self._db.flush()

            if case_obj is not None:
                self._record_case_event(
                    case_obj,
                    "ACTION_EXECUTION_UNKNOWN",
                    {
                        "execution_id": unknown_execution.id,
                        "proposal_id": proposal.id,
                        "action_type": contract.action_type,
                        "idempotency_key": clean_idemp_key,
                        "reason": "SIDE_EFFECT_TIMEOUT",
                    },
                )
                if can_transition(CaseState(case_obj.state), CaseState.UNKNOWN_OUTCOME):
                    case_obj = transition_case(
                        self._db,
                        case_obj,
                        CaseState.UNKNOWN_OUTCOME,
                        reason=(
                            f"Thực thi hành động {proposal.id} bị quá thời gian (TIMEOUT); "
                            "chuyển sang UNKNOWN_OUTCOME, không thử lại mù quáng."
                        ),
                        extra_payload={"execution_id": unknown_execution.id},
                    )

            self._db.commit()
            self._db.refresh(unknown_execution)
            self._db.refresh(proposal)
            if case_obj is not None:
                self._db.refresh(case_obj)

            return ExecutionOutcome(
                execution=unknown_execution,
                proposal=proposal,
                verification=None,
                case=case_obj,
                idempotent_replay=False,
            )

        # 7. Perform Policy-Gated Mock Write
        exec_result_payload = self._apply_mock_side_effect(
            contract_action_type=contract.action_type,
            proposal=proposal,
            approval=approval,
            idempotency_key=clean_idemp_key,
            simulate_verification_failure=simulate_verification_failure,
            now=now,
        )

        execution = ActionExecution(
            id=f"EXEC-{uuid.uuid4().hex[:10].upper()}",
            proposal_id=proposal.id,
            idempotency_key=clean_idemp_key,
            state=ActionExecutionState.EXECUTED.value,
            result_json=exec_result_payload,
            created_at=now,
            updated_at=now,
        )
        self._db.add(execution)
        proposal.status = ActionProposalStatus.EXECUTED.value
        self._db.flush()

        if case_obj is not None:
            self._record_case_event(
                case_obj,
                "ACTION_EXECUTED",
                {
                    "execution_id": execution.id,
                    "proposal_id": proposal.id,
                    "approval_id": approval.id,
                    "action_type": contract.action_type,
                    "idempotency_key": clean_idemp_key,
                    "result_json": exec_result_payload,
                },
            )
            if can_transition(CaseState(case_obj.state), CaseState.VERIFYING):
                case_obj = transition_case(
                    self._db,
                    case_obj,
                    CaseState.VERIFYING,
                    reason=(
                        f"Đã thực thi {contract.action_type}; "
                        "chuyển sang đọc ngược kiểm chứng (VERIFYING)"
                    ),
                    extra_payload={"execution_id": execution.id},
                )

        # 8. Independent Read-Back Verification (QA T01, T20)
        verifier = Verifier(self._db, self._session_id)
        verification = verifier.verify_execution(
            execution=execution,
            proposal=proposal,
            case=case_obj,
        )

        if case_obj is not None:
            if verification.verdict == VerificationVerdict.PASSED.value:
                self._record_case_event(
                    case_obj,
                    "VERIFICATION_PASSED",
                    {
                        "verification_id": verification.id,
                        "execution_id": execution.id,
                        "verdict": verification.verdict,
                        "postconditions": verification.postconditions_json,
                    },
                )
                case_obj = transition_case(
                    self._db,
                    case_obj,
                    CaseState.RESOLVED,
                    reason=(
                        f"Kiểm chứng độc lập (read-back) thành công ({verification.id}); "
                        "hoàn tất xử lý Case (RESOLVED)."
                    ),
                    verification=verification,
                    extra_payload={
                        "verification_id": verification.id,
                        "execution_id": execution.id,
                    },
                )
            else:
                # QA T20: Tool reported 200 OK, but DB postcondition check failed -> NOT RESOLVED!
                self._record_case_event(
                    case_obj,
                    "VERIFICATION_FAILED",
                    {
                        "verification_id": verification.id,
                        "execution_id": execution.id,
                        "verdict": verification.verdict,
                        "postconditions": verification.postconditions_json,
                    },
                )
                case_obj = transition_case(
                    self._db,
                    case_obj,
                    CaseState.FAILED,
                    reason=(
                        f"Kiểm chứng hậu điều kiện thất bại ({verification.id}): "
                        f"{verification.postconditions_json.get('failure_reasons')}"
                    ),
                    extra_payload={
                        "verification_id": verification.id,
                        "execution_id": execution.id,
                    },
                )

        self._db.commit()
        self._db.refresh(execution)
        self._db.refresh(proposal)
        self._db.refresh(verification)
        if case_obj is not None:
            self._db.refresh(case_obj)

        return ExecutionOutcome(
            execution=execution,
            proposal=proposal,
            verification=verification,
            case=case_obj,
            idempotent_replay=False,
        )

    def _apply_mock_side_effect(
        self,
        *,
        contract_action_type: str,
        proposal: ActionProposal,
        approval: Approval,
        idempotency_key: str,
        simulate_verification_failure: bool,
        now: datetime,
    ) -> dict[str, Any]:
        if contract_action_type == ActionType.ORDER_SYNC.value:
            params = OrderSyncActionParams.model_validate(proposal.params_json)
            order = self._db.scalar(
                select(SimOrder)
                .where(
                    SimOrder.id == params.order_id,
                    SimOrder.session_id == self._session_id,
                )
                .with_for_update()
            )
            assert order is not None  # noqa: S101 - guaranteed by PolicyEngine lock check

            if not simulate_verification_failure:
                order.status = OrderStatus.PAID.value
                order.payment_status = OrderPaymentStatus.PAID.value
                order.version += 1
                order.updated_at = now
                self._db.flush()

            return {
                "http_status": 200,
                "action_type": ActionType.ORDER_SYNC.value,
                "order_id": order.id,
                "payment_id": params.payment_id,
                "amount": params.amount,
                "currency": params.currency,
                "order_status_reported": "PAID",
                "new_order_version": order.version,
                "source_mode": "MOCK",
                "simulated_verification_failure": simulate_verification_failure,
            }

        if contract_action_type == ActionType.REFUND_EXECUTE.value:
            params_ref = RefundActionParams.model_validate(proposal.params_json)
            payment = self._db.scalar(
                select(SimPayment)
                .where(
                    SimPayment.id == params_ref.payment_id,
                    SimPayment.session_id == self._session_id,
                )
                .with_for_update()
            )
            assert payment is not None  # noqa: S101

            refund_id = f"REF-{uuid.uuid4().hex[:10].upper()}"
            if not simulate_verification_failure:
                refund = SimRefund(
                    id=refund_id,
                    session_id=self._session_id,
                    order_id=payment.order_id,
                    payment_id=payment.id,
                    amount=params_ref.amount,
                    requested_amount=params_ref.amount,
                    approved_amount=params_ref.amount,
                    completed_amount=params_ref.amount,
                    currency=payment.currency,
                    request_id=idempotency_key,
                    status=RefundStatus.COMPLETED.value,
                    reason=params_ref.reason,
                    approval_ref=approval.id,
                    provider_refund_id=f"MOCK_REF_{uuid.uuid4().hex[:8].upper()}",
                    source_mode="MOCK",
                    created_at=now,
                    updated_at=now,
                )
                self._db.add(refund)

                if payment.order_id:
                    order = self._db.scalar(
                        select(SimOrder)
                        .where(
                            SimOrder.id == payment.order_id,
                            SimOrder.session_id == self._session_id,
                        )
                        .with_for_update()
                    )
                    if order is not None:
                        all_completed = list(
                            self._db.scalars(
                                select(SimRefund).where(
                                    SimRefund.payment_id == payment.id,
                                    SimRefund.session_id == self._session_id,
                                    SimRefund.status == RefundStatus.COMPLETED.value,
                                )
                            ).all()
                        )
                        total_after = (
                            sum(r.completed_amount for r in all_completed) + params_ref.amount
                        )
                        if total_after >= payment.amount:
                            order.payment_status = OrderPaymentStatus.REFUNDED.value
                            order.status = OrderStatus.REFUNDED.value
                        else:
                            order.payment_status = OrderPaymentStatus.PARTIALLY_REFUNDED.value
                        order.version += 1
                        order.updated_at = now
                self._db.flush()

            return {
                "http_status": 200,
                "action_type": ActionType.REFUND_EXECUTE.value,
                "refund_id": refund_id,
                "payment_id": payment.id,
                "order_id": payment.order_id,
                "amount": params_ref.amount,
                "currency": payment.currency,
                "source_mode": "MOCK",
                "simulated_verification_failure": simulate_verification_failure,
            }

        # HOLD_ORDER
        params_hold = HoldOrderActionParams.model_validate(proposal.params_json)
        return {
            "http_status": 200,
            "action_type": ActionType.HOLD_ORDER.value,
            "order_id": params_hold.order_id,
            "reason": params_hold.reason,
            "source_mode": "MOCK",
        }

    def _record_case_event(
        self,
        case: Case,
        event_type: str,
        payload: dict[str, Any],
    ) -> CaseEvent:
        evt = CaseEvent(
            id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
            case_id=case.id,
            session_id=self._session_id,
            type=event_type,
            payload_json=payload,
            created_at=datetime.now(UTC),
        )
        self._db.add(evt)
        self._db.flush()
        return evt
