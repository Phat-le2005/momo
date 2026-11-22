"""Deterministic PolicyEngine for FinResolve AI.

Hard Domain Invariants:
1. Evaluates tenant/session isolation, source_mode == MOCK, role permissions,
   authoritative OBSERVED_FACT evidence provenance, and live database state.
2. Never grants privilege from model text or untrusted CLAIM evidence.
3. Rechecks current business state and optimistic concurrency version (`order.version`)
   immediately before both approval and execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import SourceMode, get_settings
from app.models.actions import ActionProposal
from app.models.cases import Case, Evidence
from app.models.enums import (
    ActionType,
    CaseState,
    EvidenceKind,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
    RiskResponse,
)
from app.models.risk import RiskSignal
from app.models.simulator import SimOrder, SimPayment, SimRefund
from app.policy.contracts import (
    POLICY_VERSION,
    BusinessActionContract,
    HoldOrderActionParams,
    OrderSyncActionParams,
    RefundActionParams,
    get_action_contract,
)


@dataclass
class PolicyEvaluationResult:
    allowed: bool
    policy_version: str
    action_type: str
    contract: BusinessActionContract
    violation_code: str | None = None
    violation_message: str | None = None
    checked_preconditions: list[str] = field(default_factory=list)
    context_snapshot: dict[str, Any] = field(default_factory=dict)


class PolicyEngine:
    """Deterministic policy evaluator for action proposals prior to approval and execution."""

    def __init__(self, db: Session, session_id: str) -> None:
        self._db = db
        self._session_id = session_id
        self._settings = get_settings()

    def evaluate(
        self,
        proposal: ActionProposal,
        *,
        approver_role: str | None = None,
        lock_rows: bool = False,
    ) -> PolicyEvaluationResult:
        """Evaluate an ActionProposal against its BusinessActionContract and live DB state."""
        contract = get_action_contract(proposal.action_type)
        checked: list[str] = []

        # 1. Session scope verification
        if proposal.session_id != self._session_id:
            return self._deny(
                contract,
                "SESSION_SCOPE_MISMATCH",
                "Proposal does not belong to the active authenticated demo session",
                checked,
            )
        checked.append("session_ownership_verified")

        # 2. Synthetic MOCK source_mode check
        if self._settings.source_mode != SourceMode.MOCK or (
            self._settings.source_mode.value not in contract.allowed_source_modes
        ):
            return self._deny(
                contract,
                "UNSUPPORTED_SOURCE_MODE",
                "Only synthetic MOCK source_mode is permitted for action execution",
                checked,
            )
        checked.append("source_mode_is_mock")

        # 3. Approver role check (if evaluating an approval or verifying approver role)
        if approver_role is not None:
            norm_role = approver_role.strip().upper()
            if norm_role not in contract.required_approver_roles:
                return self._deny(
                    contract,
                    "UNAUTHORIZED_APPROVER_ROLE",
                    f"Role '{approver_role}' is not authorized for '{contract.action_type}'. "
                    f"Required roles: {sorted(contract.required_approver_roles)}",
                    checked,
                )
            checked.append("approver_role_authorized")

        # 4. Validate structured action parameters against contract input_schema
        try:
            validated_params = contract.validate_params(proposal.params_json)
        except ValidationError as exc:
            return self._deny(
                contract,
                "INVALID_ACTION_PARAMS",
                f"ActionProposal params_json failed contract schema validation: {exc}",
                checked,
            )
        checked.append("action_params_schema_valid")

        # 5. If linked to a Case, check Case ownership, active HOLD_ACTION risk signals,
        #    and authoritative OBSERVED_FACT evidence provenance.
        if proposal.case_id is not None:
            stmt_case = select(Case).where(
                Case.id == proposal.case_id,
                Case.session_id == self._session_id,
            )
            if lock_rows:
                stmt_case = stmt_case.with_for_update()
            case_obj = self._db.scalar(stmt_case)
            if case_obj is None:
                return self._deny(
                    contract,
                    "CASE_NOT_FOUND_IN_SESSION",
                    f"Linked Case '{proposal.case_id}' not found in current session",
                    checked,
                )

            if case_obj.state in (CaseState.RESOLVED.value, CaseState.FAILED.value):
                return self._deny(
                    contract,
                    "CASE_ALREADY_TERMINAL",
                    f"Cannot approve or execute action on terminal Case state '{case_obj.state}'",
                    checked,
                )

            # Check for any active HOLD_ACTION RiskSignal on this Case
            hold_signals = list(
                self._db.scalars(
                    select(RiskSignal).where(
                        RiskSignal.case_id == case_obj.id,
                        RiskSignal.session_id == self._session_id,
                        RiskSignal.response == RiskResponse.HOLD_ACTION.value,
                    )
                ).all()
            )
            if hold_signals:
                categories = [s.category for s in hold_signals]
                return self._deny(
                    contract,
                    "RISK_HOLD_ACTION_ACTIVE",
                    f"Case '{case_obj.id}' has active HOLD_ACTION risk signal(s): {categories}",
                    checked,
                )
            checked.append("no_active_hold_action_risk_signal")

            # Check authoritative OBSERVED_FACT evidence provenance
            if not proposal.evidence_refs:
                return self._deny(
                    contract,
                    "MISSING_EVIDENCE_REFS",
                    "Case-bound ActionProposal must reference authoritative OBSERVED_FACT evidence",
                    checked,
                )

            ev_rows = list(
                self._db.scalars(
                    select(Evidence).where(
                        Evidence.case_id == case_obj.id,
                        Evidence.session_id == self._session_id,
                        Evidence.id.in_(proposal.evidence_refs),
                    )
                ).all()
            )
            observed_sources = {
                e.source for e in ev_rows if e.kind == EvidenceKind.OBSERVED_FACT.value
            }
            missing_sources = contract.required_evidence_sources - observed_sources
            if missing_sources:
                return self._deny(
                    contract,
                    "INSUFFICIENT_OBSERVED_FACT_PROVENANCE",
                    f"Proposal is missing required authoritative OBSERVED_FACT sources: "
                    f"{sorted(missing_sources)}",
                    checked,
                )
            checked.append("authoritative_observed_fact_evidence_present")

        # 6. Action-specific live DB preconditions
        if isinstance(validated_params, OrderSyncActionParams):
            return self._evaluate_order_sync(
                contract=contract,
                params=validated_params,
                checked=checked,
                lock_rows=lock_rows,
            )
        if isinstance(validated_params, RefundActionParams):
            return self._evaluate_refund(
                contract=contract,
                params=validated_params,
                checked=checked,
                lock_rows=lock_rows,
            )
        if isinstance(validated_params, HoldOrderActionParams):
            return self._evaluate_hold_order(
                contract=contract,
                params=validated_params,
                checked=checked,
                lock_rows=lock_rows,
            )

        return self._deny(contract, "UNHANDLED_CONTRACT", "Unhandled contract type", checked)

    def _evaluate_order_sync(
        self,
        *,
        contract: BusinessActionContract,
        params: OrderSyncActionParams,
        checked: list[str],
        lock_rows: bool,
    ) -> PolicyEvaluationResult:
        stmt_order = select(SimOrder).where(
            SimOrder.id == params.order_id,
            SimOrder.session_id == self._session_id,
        )
        stmt_pay = select(SimPayment).where(
            SimPayment.id == params.payment_id,
            SimPayment.session_id == self._session_id,
        )
        if lock_rows:
            stmt_order = stmt_order.with_for_update()
            stmt_pay = stmt_pay.with_for_update()

        order = self._db.scalar(stmt_order)
        if order is None:
            return self._deny(
                contract,
                "ORDER_NOT_FOUND",
                f"Order '{params.order_id}' not found in session",
                checked,
            )

        payment = self._db.scalar(stmt_pay)
        if payment is None:
            return self._deny(
                contract,
                "PAYMENT_NOT_FOUND",
                f"Payment '{params.payment_id}' not found in session",
                checked,
            )

        if order.source_mode != "MOCK" or payment.source_mode != "MOCK":
            return self._deny(
                contract,
                "NON_MOCK_ENTITY_FORBIDDEN",
                "Order and Payment must both have source_mode == 'MOCK'",
                checked,
            )

        # Verify payment belongs to this exact order
        if payment.order_id != order.id:
            return self._deny(
                contract,
                "CROSS_ORDER_PAYMENT_MISMATCH",
                f"Payment '{payment.id}' belongs to order '{payment.order_id}', "
                f"not target order '{order.id}'",
                checked,
            )
        checked.append("payment_bound_to_exact_order_id")

        # Verify payment is authoritative SUCCESS
        if (
            payment.status != PaymentStatus.SUCCESS.value
            or payment.verified_at is None
            or payment.result_code != 0
        ):
            return self._deny(
                contract,
                "PAYMENT_NOT_VERIFIED_SUCCESS",
                f"Payment '{payment.id}' is not an authoritative verified SUCCESS record "
                f"(status={payment.status}, verified_at={payment.verified_at})",
                checked,
            )
        checked.append("payment_status_is_verified_success")

        # Verify merchant, amount, and currency match
        if (
            not payment.merchant_id
            or payment.merchant_id != params.merchant_id
            or payment.amount != order.amount
            or payment.amount != params.amount
            or payment.currency != order.currency
            or payment.currency != params.currency
        ):
            return self._deny(
                contract,
                "PAYMENT_ORDER_AMOUNT_OR_MERCHANT_MISMATCH",
                "Payment amount, currency, or merchant_id does not match Order and proposal params",
                checked,
            )
        checked.append("payment_merchant_and_amount_match_order")

        # Verify no ambiguous second payment exists for this order
        order_payments = list(
            self._db.scalars(
                select(SimPayment).where(
                    SimPayment.session_id == self._session_id,
                    SimPayment.order_id == order.id,
                )
            ).all()
        )
        if len(order_payments) != 1 or order_payments[0].id != payment.id:
            return self._deny(
                contract,
                "AMBIGUOUS_ORDER_PAYMENTS",
                f"Order '{order.id}' has {len(order_payments)} candidate payments; "
                "unique payment binding required",
                checked,
            )

        # QA T13: Verify optimistic concurrency version on SimOrder
        if order.version != params.expected_order_version:
            return self._deny(
                contract,
                "STALE_ORDER_VERSION",
                f"Order '{order.id}' version changed before write: expected version "
                f"{params.expected_order_version}, but current DB version is {order.version}. "
                "Re-read and replan required.",
                checked,
                snapshot={
                    "order_id": order.id,
                    "expected_order_version": params.expected_order_version,
                    "current_order_version": order.version,
                },
            )
        checked.append("order_version_matches_expected_order_version")

        # Verify order is currently PENDING and UNPAID
        if (
            order.status != OrderStatus.PENDING.value
            or order.payment_status != OrderPaymentStatus.UNPAID.value
        ):
            return self._deny(
                contract,
                "INVALID_ORDER_STATE_FOR_SYNC",
                f"Order '{order.id}' is in state (status={order.status}, "
                f"payment_status={order.payment_status}); expected (PENDING, UNPAID)",
                checked,
            )
        checked.append("order_status_is_pending_and_unpaid")

        return PolicyEvaluationResult(
            allowed=True,
            policy_version=POLICY_VERSION,
            action_type=ActionType.ORDER_SYNC.value,
            contract=contract,
            checked_preconditions=checked,
            context_snapshot={
                "order_id": order.id,
                "payment_id": payment.id,
                "merchant_id": payment.merchant_id,
                "amount": order.amount,
                "currency": order.currency,
                "order_version": order.version,
            },
        )

    def _evaluate_refund(
        self,
        *,
        contract: BusinessActionContract,
        params: RefundActionParams,
        checked: list[str],
        lock_rows: bool,
    ) -> PolicyEvaluationResult:
        stmt_pay = select(SimPayment).where(
            SimPayment.id == params.payment_id,
            SimPayment.session_id == self._session_id,
        )
        if lock_rows:
            stmt_pay = stmt_pay.with_for_update()
        payment = self._db.scalar(stmt_pay)

        if payment is None:
            return self._deny(
                contract,
                "PAYMENT_NOT_FOUND",
                f"Payment '{params.payment_id}' not found in session",
                checked,
            )

        if params.order_id and payment.order_id != params.order_id:
            return self._deny(
                contract,
                "CROSS_ORDER_PAYMENT_MISMATCH",
                f"Payment '{payment.id}' does not belong to order '{params.order_id}'",
                checked,
            )

        if payment.status != PaymentStatus.SUCCESS.value or payment.verified_at is None:
            return self._deny(
                contract,
                "PAYMENT_NOT_VERIFIED_SUCCESS",
                f"Cannot refund payment '{payment.id}' with status '{payment.status}'",
                checked,
            )
        checked.append("payment_status_is_verified_success")

        stmt_refunds = select(SimRefund).where(
            SimRefund.payment_id == payment.id,
            SimRefund.session_id == self._session_id,
            SimRefund.status.in_(
                [
                    RefundStatus.COMPLETED.value,
                    RefundStatus.PENDING.value,
                    RefundStatus.UNKNOWN.value,
                ]
            ),
        )
        if lock_rows:
            stmt_refunds = stmt_refunds.with_for_update()
        existing_refunds = list(self._db.scalars(stmt_refunds).all())

        used_balance = sum(
            r.completed_amount if r.status == RefundStatus.COMPLETED.value else r.requested_amount
            for r in existing_refunds
        )
        remaining_balance = payment.amount - used_balance

        if remaining_balance <= 0 or params.amount > remaining_balance:
            return self._deny(
                contract,
                "REFUND_EXCEEDS_ELIGIBLE_BALANCE",
                f"Refund amount {params.amount} exceeds remaining eligible refundable "
                f"balance {remaining_balance}",
                checked,
                snapshot={
                    "payment_id": payment.id,
                    "payment_amount": payment.amount,
                    "used_balance": used_balance,
                    "remaining_balance": remaining_balance,
                    "requested_amount": params.amount,
                },
            )
        checked.append(
            "refund_amount_within_remaining_eligible_balance_across_completed_pending_unknown"
        )

        return PolicyEvaluationResult(
            allowed=True,
            policy_version=POLICY_VERSION,
            action_type=ActionType.REFUND_EXECUTE.value,
            contract=contract,
            checked_preconditions=checked,
            context_snapshot={
                "payment_id": payment.id,
                "order_id": payment.order_id,
                "amount": params.amount,
                "remaining_balance_before": remaining_balance,
            },
        )

    def _evaluate_hold_order(
        self,
        *,
        contract: BusinessActionContract,
        params: HoldOrderActionParams,
        checked: list[str],
        lock_rows: bool,
    ) -> PolicyEvaluationResult:
        stmt_order = select(SimOrder).where(
            SimOrder.id == params.order_id,
            SimOrder.session_id == self._session_id,
        )
        if lock_rows:
            stmt_order = stmt_order.with_for_update()
        order = self._db.scalar(stmt_order)
        if order is None:
            return self._deny(
                contract,
                "ORDER_NOT_FOUND",
                f"Order '{params.order_id}' not found in session",
                checked,
            )
        checked.append("order_exists_in_session")
        return PolicyEvaluationResult(
            allowed=True,
            policy_version=POLICY_VERSION,
            action_type=ActionType.HOLD_ORDER.value,
            contract=contract,
            checked_preconditions=checked,
            context_snapshot={"order_id": order.id, "order_status": order.status},
        )

    @staticmethod
    def _deny(
        contract: BusinessActionContract,
        code: str,
        message: str,
        checked: list[str],
        snapshot: dict[str, Any] | None = None,
    ) -> PolicyEvaluationResult:
        return PolicyEvaluationResult(
            allowed=False,
            policy_version=POLICY_VERSION,
            action_type=contract.action_type,
            contract=contract,
            violation_code=code,
            violation_message=message,
            checked_preconditions=checked,
            context_snapshot=snapshot or {},
        )
