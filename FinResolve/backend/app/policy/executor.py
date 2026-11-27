"""Policy-gated ActionExecutor, Verification Recovery, and Post-Commit Verifier for FinResolve AI.

Hard Domain Invariants:
1. Only Policy-Gated & Approved Writes: No proposal can execute without a valid, non-expired
   `Approval` whose v2 cryptographic digest matches the exact `session_id`, `proposal_id`,
   `case_id`, `action_type`, `params_json`, `evidence_refs`, `expected_version`,
   `policy_version`, `approver_role`, and `approver_id` (QA T11, T12). Legacy digests are
   strictly rejected.
2. Live State Recheck Under Lock: Acquires `SELECT FOR UPDATE` on `ActionProposal`, `Approval`,
   `Case`, `SimOrder`, and `SimPayment` and re-evaluates `PolicyEngine(..., for_execution=True)`
   immediately before write.
   - If `order.version != expected_order_version` (QA T13), rejects with 409 CONFLICT.
   - `HOLD_ORDER` execution is disabled (`409 HOLD_ORDER_EXECUTION_DISABLED`).
   - `REFUND_EXECUTE` recalculates remaining refundable balance across `COMPLETED`, `PENDING`,
     and `UNKNOWN` refunds under row lock before creating a mock refund.
3. Idempotency, Anti-Replay & Verification Recovery (QA T06 & Hardening 4):
   - If a committed `ActionExecution(state='EXECUTED')` exists but lacks a `Verification` record
     (or the Case remains in `VERIFYING`), recovery completes post-commit read-back verification
     and transitions the Case to `RESOLVED` (or `FAILED`) without repeating the side effect!
   - Same `idempotency_key` on an already verified execution replays the existing outcome.
   - Different `idempotency_key` on an already verified execution is rejected with 409 CONFLICT.
4. Safe Timeout / UNKNOWN Handling (QA T04):
   - When an execution times out (`simulate_timeout=True`), records `ActionExecution(state=UNKNOWN)`
     and transitions Case to `UNKNOWN_OUTCOME`.
   - Any subsequent attempt to retry a proposal with an `UNKNOWN` execution using a new key
     is rejected with 409 CONFLICT (`UNKNOWN_OUTCOME_NO_BLIND_RETRY`).
5. Separate Post-Commit Durable Read-Back Verification (QA T01, T10, T20):
   - Phase 1 commits the transactional execution (`SimOrder` mutation or `SimRefund` insert,
     `ActionExecution`, and `EXECUTING -> VERIFYING` transition) to PostgreSQL.
   - Phase 2 opens an independent SQLAlchemy database session via `get_sessionmaker()()` to
     read back durable committed rows from PostgreSQL.
   - Never defaults `is_valid` to True; only explicit verified branches (`ORDER_SYNC`,
     `REFUND_EXECUTE`) can achieve `PASSED`. A no-op `HOLD_ORDER` always evaluates to `FAILED`.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_core.telemetry import assert_execution_fencing_in_txn
from app.cases.state_machine import can_transition, transition_case
from app.core.errors import AppError, ErrorCode
from app.db.session import get_sessionmaker
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
from app.policy.approval import compute_approval_digest
from app.policy.contracts import (
    OrderSyncActionParams,
    RefundActionParams,
    get_action_contract,
)
from app.policy.engine import PolicyEngine

_LOCKS_GUARD = threading.Lock()
_PROPOSAL_EXEC_LOCKS: dict[str, threading.Lock] = {}


def _get_proposal_exec_lock(proposal_id: str) -> threading.Lock:
    """Return a per-proposal mutex so concurrent callers serialize across the Phase 1 commit
    and Phase 2 independent post-commit PostgreSQL verification session."""
    with _LOCKS_GUARD:
        lock = _PROPOSAL_EXEC_LOCKS.get(proposal_id)
        if lock is None:
            lock = threading.Lock()
            _PROPOSAL_EXEC_LOCKS[proposal_id] = lock
        return lock


@dataclass
class ExecutionOutcome:
    execution: ActionExecution
    proposal: ActionProposal
    verification: Verification | None
    case: Case | None
    idempotent_replay: bool


class Verifier:
    """Independent postcondition verifier querying durable committed PostgreSQL records."""

    def __init__(self, db: Session, session_id: str) -> None:
        self._db = db
        self._session_id = session_id

    def verify_execution(
        self,
        *,
        execution: ActionExecution,
        proposal: ActionProposal,
        case: Case | None,
        use_separate_readback_session: bool = True,
    ) -> Verification:
        """Independently read back durable DB records to verify action postconditions."""
        now = datetime.now(UTC)
        contract = get_action_contract(proposal.action_type)
        case_version = case.version if case is not None else proposal.expected_version

        failure_reasons: list[str] = []
        postconditions_detail: dict[str, Any] = {
            "contract_action_type": contract.action_type,
            "policy_version": contract.policy_version,
            "execution_id": execution.id,
            "post_commit_durable_read_back": use_separate_readback_session,
        }

        maker = get_sessionmaker()
        read_session = maker() if use_separate_readback_session else self._db
        try:
            persisted_exec = read_session.scalar(
                select(ActionExecution).where(ActionExecution.id == execution.id)
            )
            exec_to_check = persisted_exec or execution

            exec_state_ok = exec_to_check.state == ActionExecutionState.EXECUTED.value
            if not exec_state_ok:
                failure_reasons.append(
                    f"ActionExecution state is '{exec_to_check.state}', expected 'EXECUTED'"
                )

            branch_verified = False

            if contract.action_type == ActionType.ORDER_SYNC.value:
                params = OrderSyncActionParams.model_validate(proposal.params_json)
                order = read_session.scalar(
                    select(SimOrder).where(
                        SimOrder.id == params.order_id,
                        SimOrder.session_id == self._session_id,
                    )
                )
                payment = read_session.scalar(
                    select(SimPayment).where(
                        SimPayment.id == params.payment_id,
                        SimPayment.session_id == self._session_id,
                    )
                )

                expected_new_version = params.expected_order_version + 1
                order_checks_ok = True
                if order is None:
                    order_checks_ok = False
                    failure_reasons.append(
                        f"SimOrder '{params.order_id}' not found on durable read-back"
                    )
                else:
                    postconditions_detail["observed_order_status"] = order.status
                    postconditions_detail["observed_order_payment_status"] = order.payment_status
                    postconditions_detail["observed_order_version"] = order.version
                    postconditions_detail["expected_order_version_after"] = expected_new_version

                    if order.status != OrderStatus.PAID.value:
                        order_checks_ok = False
                        failure_reasons.append(
                            f"Order status is '{order.status}' after execution, expected 'PAID'"
                        )
                    if order.payment_status != OrderPaymentStatus.PAID.value:
                        order_checks_ok = False
                        failure_reasons.append(
                            f"Order payment_status is '{order.payment_status}', expected 'PAID'"
                        )
                    if order.version != expected_new_version:
                        order_checks_ok = False
                        failure_reasons.append(
                            f"Order version is {order.version}, expected {expected_new_version}"
                        )

                payment_checks_ok = True
                if payment is None:
                    payment_checks_ok = False
                    failure_reasons.append(
                        f"SimPayment '{params.payment_id}' not found on durable read-back"
                    )
                else:
                    postconditions_detail["observed_payment_status"] = payment.status
                    postconditions_detail["observed_payment_verified"] = (
                        payment.verified_at is not None
                    )
                    if (
                        payment.status != PaymentStatus.SUCCESS.value
                        or payment.verified_at is None
                        or payment.result_code != 0
                        or (order is not None and payment.order_id != order.id)
                        or (order is not None and payment.amount != order.amount)
                    ):
                        payment_checks_ok = False
                        failure_reasons.append(
                            "Payment record failed authoritative SUCCESS or order-binding read-back"
                        )

                branch_verified = exec_state_ok and order_checks_ok and payment_checks_ok

            elif contract.action_type == ActionType.REFUND_EXECUTE.value:
                params_ref = RefundActionParams.model_validate(proposal.params_json)
                refund = read_session.scalar(
                    select(SimRefund).where(
                        SimRefund.request_id == execution.idempotency_key,
                        SimRefund.session_id == self._session_id,
                    )
                )
                payment = read_session.scalar(
                    select(SimPayment).where(
                        SimPayment.id == params_ref.payment_id,
                        SimPayment.session_id == self._session_id,
                    )
                )

                refund_checks_ok = True
                if refund is None:
                    refund_checks_ok = False
                    failure_reasons.append("SimRefund record not found in database on read-back")
                else:
                    postconditions_detail["refund_id"] = refund.id
                    postconditions_detail["observed_refund_status"] = refund.status
                    postconditions_detail["observed_completed_amount"] = refund.completed_amount
                    if refund.status != RefundStatus.COMPLETED.value:
                        refund_checks_ok = False
                        failure_reasons.append(
                            f"SimRefund status is '{refund.status}', expected 'COMPLETED'"
                        )
                    if refund.completed_amount != params_ref.amount:
                        refund_checks_ok = False
                        failure_reasons.append(
                            f"SimRefund completed_amount ({refund.completed_amount}) != "
                            f"expected ({params_ref.amount})"
                        )

                pay_checks_ok = True
                if payment is None or payment.status != PaymentStatus.SUCCESS.value:
                    pay_checks_ok = False
                    failure_reasons.append("Original payment missing or not in SUCCESS status")
                else:
                    completed_refunds = list(
                        read_session.scalars(
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
                        pay_checks_ok = False
                        failure_reasons.append(
                            f"Total completed refunds ({total_completed}) exceeds "
                            f"payment amount ({payment.amount})"
                        )

                branch_verified = exec_state_ok and refund_checks_ok and pay_checks_ok

            else:
                branch_verified = False
                failure_reasons.append(
                    f"HOLD_ORDER_NO_OP_CANNOT_VERIFY_PASSED: Action '{contract.action_type}' "
                    "does not persist a verifiable domain postcondition"
                )
        finally:
            if use_separate_readback_session:
                read_session.close()

        is_valid = branch_verified and len(failure_reasons) == 0
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

    def recover_verification(
        self,
        *,
        proposal_id: str,
    ) -> ExecutionOutcome:
        """Recover verification for a committed ActionExecution that remained in VERIFYING
        without a Verification record. Never repeats the business side effect."""
        with _get_proposal_exec_lock(proposal_id):
            return self._recover_verification_locked(proposal_id=proposal_id)

    def _recover_verification_locked(
        self,
        *,
        proposal_id: str,
    ) -> ExecutionOutcome:
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
            # Prompt 12 Section 14: NEVER execute simulator mutations on a MiniStore Commerce Case!
            if case_obj is not None and case_obj.source_system == "COMMERCE_HTTP":
                raise AppError(
                    ErrorCode.FORBIDDEN,
                    (
                        "FORBIDDEN_SIMULATOR_EXECUTION_ON_COMMERCE_CASE: "
                        "MiniStore Commerce cases are read-only by default in FinResolve. "
                        "Simulator ActionExecutor cannot mutate external Commerce state."
                    ),
                    status_code=403,
                )

        executed_exec = self._db.scalar(
            select(ActionExecution)
            .where(
                ActionExecution.proposal_id == proposal.id,
                ActionExecution.state == ActionExecutionState.EXECUTED.value,
            )
            .order_by(ActionExecution.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        if executed_exec is None:
            raise AppError(
                ErrorCode.CONFLICT,
                f"No committed EXECUTED record found to recover for proposal '{proposal.id}'",
                status_code=409,
            )

        existing_verif = self._db.scalar(
            select(Verification)
            .where(Verification.action_execution_id == executed_exec.id)
            .order_by(Verification.checked_at.desc())
            .limit(1)
        )
        if existing_verif is not None and (
            case_obj is None or case_obj.state != CaseState.VERIFYING.value
        ):
            return ExecutionOutcome(
                execution=executed_exec,
                proposal=proposal,
                verification=existing_verif,
                case=case_obj,
                idempotent_replay=True,
            )

        return self._run_post_commit_verification_phase(
            execution=executed_exec,
            proposal=proposal,
            case_obj=case_obj,
            is_recovery=True,
        )

    def execute_proposal(
        self,
        *,
        proposal_id: str,
        idempotency_key: str,
        simulate_timeout: bool = False,
        simulate_verification_failure: bool = False,
    ) -> ExecutionOutcome:
        with _get_proposal_exec_lock(proposal_id):
            return self._execute_proposal_locked(
                proposal_id=proposal_id,
                idempotency_key=idempotency_key,
                simulate_timeout=simulate_timeout,
                simulate_verification_failure=simulate_verification_failure,
            )

    def _execute_proposal_locked(
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
            # Prompt 12 Section 14: NEVER execute simulator mutations on a MiniStore Commerce Case!
            if case_obj is not None and case_obj.source_system == "COMMERCE_HTTP":
                raise AppError(
                    ErrorCode.FORBIDDEN,
                    (
                        "FORBIDDEN_SIMULATOR_EXECUTION_ON_COMMERCE_CASE: "
                        "MiniStore Commerce cases are read-only by default in FinResolve. "
                        "Simulator ActionExecutor cannot mutate external Commerce state."
                    ),
                    status_code=403,
                )

        # 2. Check existing executions (Idempotency, Recovery, Anti-Replay & UNKNOWN lockout)
        existing_executions = list(
            self._db.scalars(
                select(ActionExecution)
                .where(ActionExecution.proposal_id == proposal.id)
                .order_by(ActionExecution.created_at.asc())
                .with_for_update()
            ).all()
        )

        # 2A: Hardening 4 — Check if a committed EXECUTED record remained in VERIFYING
        #     without a Verification record. If so, recover verification WITHOUT repeating
        #     the business side effect!
        executed_exec = next(
            (ex for ex in existing_executions if ex.state == ActionExecutionState.EXECUTED.value),
            None,
        )
        if executed_exec is not None:
            existing_verif = self._db.scalar(
                select(Verification)
                .where(Verification.action_execution_id == executed_exec.id)
                .order_by(Verification.checked_at.desc())
                .limit(1)
            )
            if existing_verif is None:
                return self._run_post_commit_verification_phase(
                    execution=executed_exec,
                    proposal=proposal,
                    case_obj=case_obj,
                    is_recovery=True,
                )

            # Already verified: if same idempotency_key -> idempotent replay (QA T06)
            if executed_exec.idempotency_key == clean_idemp_key:
                return ExecutionOutcome(
                    execution=executed_exec,
                    proposal=proposal,
                    verification=existing_verif,
                    case=case_obj,
                    idempotent_replay=True,
                )

            # Already verified and caller passed a different idempotency_key -> block replay!
            raise AppError(
                ErrorCode.CONFLICT,
                f"APPROVAL_REPLAY_BLOCKED: Proposal '{proposal.id}' has already been executed "
                f"(execution_id={executed_exec.id}).",
                status_code=409,
                details={"proposal_id": proposal.id},
            )

        # 2B: Exact idempotency key match on non-EXECUTED (e.g. UNKNOWN) record
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

        # 2C: Check if a global idempotency key collision exists on another proposal
        other_exec_same_key = self._db.scalar(
            select(ActionExecution).where(ActionExecution.idempotency_key == clean_idemp_key)
        )
        if other_exec_same_key is not None and other_exec_same_key.proposal_id != proposal.id:
            raise AppError(
                ErrorCode.CONFLICT,
                f"Idempotency key '{clean_idemp_key}' was already used for a different proposal",
                status_code=409,
            )

        # 2D: QA T04 — If previous execution is UNKNOWN, forbid blind retry with a new key!
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

        if proposal.status == ActionProposalStatus.EXECUTED.value:
            raise AppError(
                ErrorCode.CONFLICT,
                f"APPROVAL_REPLAY_BLOCKED: Proposal '{proposal.id}' has already been executed.",
                status_code=409,
                details={"proposal_id": proposal.id},
            )

        # 3. Verify Proposal is APPROVED and validate Approval record (expiry & strict v2 digest)
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

        contract = get_action_contract(proposal.action_type)
        if approval.policy_version != contract.policy_version:
            raise AppError(
                ErrorCode.CONFLICT,
                f"APPROVAL_POLICY_VERSION_MISMATCH: Approval policy_version "
                f"'{approval.policy_version}' != current contract '{contract.policy_version}'",
                status_code=409,
            )

        # Hardening 4 & QA T11: Strictly verify v2 digest binding (reject legacy digests!)
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
            approver_id=approval.approver_id,
        )
        if approval.digest != expected_digest:
            raise AppError(
                ErrorCode.CONFLICT,
                "APPROVAL_DIGEST_MISMATCH: Approval digest does not match current proposal "
                "parameters, evidence, policy version, or approver identity (legacy digests "
                "and tampered parameters are rejected)",
                status_code=409,
                details={"approval_id": approval.id, "proposal_id": proposal.id},
            )

        # 4. Recheck live business state & optimistic version lock via PolicyEngine (QA T13)
        #    Note: for SIMULATED_DEMO_APPROVAL, pass approver_role=None so PolicyEngine
        #    validates all domain/state preconditions while permitting simulated demo role.
        role_for_policy = (
            None if approval.approver_role == "SIMULATED_DEMO_APPROVAL" else approval.approver_role
        )
        policy_engine = PolicyEngine(self._db, self._session_id)
        eval_res = policy_engine.evaluate(
            proposal,
            approver_role=role_for_policy,
            lock_rows=True,
            for_execution=True,
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

        # 4.5. Verify worker fencing ownership inside the SAME DB transaction before ANY write
        assert_execution_fencing_in_txn(self._db, session_id=self._session_id)

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

        # 7. Phase 1: Perform Policy-Gated Mock Write & Commit to Durable Storage
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

        # 8. Commit Phase 1 (durable mock write, ActionExecution(EXECUTED), and Case(VERIFYING))
        #    to PostgreSQL BEFORE independent verification runs!
        self._db.commit()
        self._db.refresh(execution)
        self._db.refresh(proposal)
        if case_obj is not None:
            self._db.refresh(case_obj)

        # 9. Phase 2: Use a fresh independent PostgreSQL session to re-read committed Order,
        #    Payment, and Refund rows and verify postconditions before marking Case RESOLVED.
        return self._run_post_commit_verification_phase(
            execution=execution,
            proposal=proposal,
            case_obj=case_obj,
            is_recovery=False,
        )

    def _run_post_commit_verification_phase(
        self,
        *,
        execution: ActionExecution,
        proposal: ActionProposal,
        case_obj: Case | None,
        is_recovery: bool,
    ) -> ExecutionOutcome:
        """Open a fresh independent PostgreSQL session after Phase 1 commit to re-read the
        actual committed SimOrder, SimPayment, and SimRefund rows, verify all postconditions,
        and only then transition Case from VERIFYING to RESOLVED (or FAILED)."""
        verifier = Verifier(self._db, self._session_id)
        verification = verifier.verify_execution(
            execution=execution,
            proposal=proposal,
            case=case_obj,
            use_separate_readback_session=True,
        )

        if case_obj is not None:
            if case_obj.state != CaseState.VERIFYING.value:
                if can_transition(CaseState(case_obj.state), CaseState.VERIFYING):
                    case_obj = transition_case(
                        self._db,
                        case_obj,
                        CaseState.VERIFYING,
                        reason="Khôi phục bước kiểm chứng hậu điều kiện (VERIFYING)",
                        extra_payload={"execution_id": execution.id, "recovered": is_recovery},
                    )
                    verification.case_version = case_obj.version
                    self._db.flush()

            if verification.verdict == VerificationVerdict.PASSED.value:
                self._record_case_event(
                    case_obj,
                    "VERIFICATION_PASSED",
                    {
                        "verification_id": verification.id,
                        "execution_id": execution.id,
                        "verdict": verification.verdict,
                        "recovered": is_recovery,
                        "postconditions": verification.postconditions_json,
                    },
                )
                case_obj = transition_case(
                    self._db,
                    case_obj,
                    CaseState.RESOLVED,
                    reason=(
                        f"Kiểm chứng độc lập sau commit (post-commit read-back) thành công "
                        f"({verification.id}); hoàn tất xử lý Case (RESOLVED)."
                    ),
                    verification=verification,
                    extra_payload={
                        "verification_id": verification.id,
                        "execution_id": execution.id,
                        "recovered": is_recovery,
                    },
                )
            else:
                self._record_case_event(
                    case_obj,
                    "VERIFICATION_FAILED",
                    {
                        "verification_id": verification.id,
                        "execution_id": execution.id,
                        "verdict": verification.verdict,
                        "recovered": is_recovery,
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
                        "recovered": is_recovery,
                    },
                )

        # Commit Phase 2 (Verification record and VERIFYING -> RESOLVED/FAILED transition)
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
            idempotent_replay=is_recovery,
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
            assert order is not None  # noqa: S101

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
                "idempotency_key": idempotency_key,
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
                all_completed_before = list(
                    self._db.scalars(
                        select(SimRefund).where(
                            SimRefund.payment_id == payment.id,
                            SimRefund.session_id == self._session_id,
                            SimRefund.status == RefundStatus.COMPLETED.value,
                        )
                    ).all()
                )
                settled_before = sum(r.completed_amount for r in all_completed_before)

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
                    order_ref = self._db.scalar(
                        select(SimOrder)
                        .where(
                            SimOrder.id == payment.order_id,
                            SimOrder.session_id == self._session_id,
                        )
                        .with_for_update()
                    )
                    if order_ref is not None:
                        total_after = settled_before + params_ref.amount
                        if total_after >= payment.amount:
                            order_ref.payment_status = OrderPaymentStatus.REFUNDED.value
                            order_ref.status = OrderStatus.REFUNDED.value
                        else:
                            order_ref.payment_status = OrderPaymentStatus.PARTIALLY_REFUNDED.value
                        order_ref.version += 1
                        order_ref.updated_at = now
                self._db.flush()

            return {
                "http_status": 200,
                "action_type": ActionType.REFUND_EXECUTE.value,
                "refund_id": refund_id,
                "payment_id": payment.id,
                "order_id": payment.order_id,
                "amount": params_ref.amount,
                "currency": payment.currency,
                "idempotency_key": idempotency_key,
                "source_mode": "MOCK",
                "simulated_verification_failure": simulate_verification_failure,
            }

        raise AppError(
            ErrorCode.CONFLICT,
            f"Execution of action '{contract_action_type}' is disabled",
            status_code=409,
        )

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
