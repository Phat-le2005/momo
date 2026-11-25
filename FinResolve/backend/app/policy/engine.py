"""Deterministic PolicyEngine for FinResolve AI.

Hard Domain Invariants:
1. Evaluates tenant/session isolation, source_mode == MOCK, role permissions,
   authoritative OBSERVED_FACT evidence provenance, and live database state.
2. Never grants privilege from model text or untrusted CLAIM evidence.
3. Rechecks current business state and optimistic concurrency version (`order.version`)
   immediately before both approval and execution.
4. Enforces merchant, currency, result_code, source_mode, and remaining balance invariants
   on REFUND_EXECUTE proposals.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import SourceMode, get_settings
from app.models.actions import ActionProposal, Approval
from app.models.cases import Case, Evidence
from app.models.enums import (
    ActionType,
    ApprovalDecision,
    CaseState,
    EvidenceKind,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
    RiskResponse,
)
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
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


@dataclass
class CommercePolicyDecision:
    """Authoritative PolicyEngine decision with HMAC-SHA256 signed command authorization."""

    allowed: bool
    decision_code: str
    policy_version: str
    command: str
    risk_level: str
    requires_human_approval: bool
    authorization_id: str | None = None
    authorization_token: str | None = None
    authorization_digest: str | None = None
    approval_id: str | None = None
    violation_code: str | None = None
    violation_message: str | None = None
    checked_preconditions: list[str] = field(default_factory=list)
    context_snapshot: dict[str, Any] = field(default_factory=dict)


COMMERCE_ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        "REQUEST_ORDER_SYNC",
        "REQUEST_EVENT_REDELIVERY",
        "REQUEST_PAYMENT_RECONCILIATION",
        "REQUEST_MANUAL_REVIEW",
    }
)


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
        for_execution: bool = False,
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

            # Check for any active (non-cleared) HOLD_ACTION RiskSignal on this Case
            hold_signals = list(
                self._db.scalars(
                    select(RiskSignal).where(
                        RiskSignal.case_id == case_obj.id,
                        RiskSignal.session_id == self._session_id,
                        RiskSignal.response == RiskResponse.HOLD_ACTION.value,
                    )
                ).all()
            )
            active_holds = [
                s
                for s in hold_signals
                if str(s.details_json.get("lifecycle_status") or "ACTIVE").upper() == "ACTIVE"
            ]
            if active_holds:
                categories = [s.category for s in active_holds]
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
                for_execution=for_execution,
                case_id=proposal.case_id,
                evidence_refs=proposal.evidence_refs,
            )
        if isinstance(validated_params, HoldOrderActionParams):
            return self._evaluate_hold_order(
                contract=contract,
                params=validated_params,
                checked=checked,
                lock_rows=lock_rows,
                for_execution=for_execution,
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

        if payment.order_id != order.id:
            return self._deny(
                contract,
                "CROSS_ORDER_PAYMENT_MISMATCH",
                f"Payment '{payment.id}' belongs to order '{payment.order_id}', "
                f"not target order '{order.id}'",
                checked,
            )
        checked.append("payment_bound_to_exact_order_id")

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
        for_execution: bool = False,
        case_id: str | None = None,
        evidence_refs: list[str] | None = None,
    ) -> PolicyEvaluationResult:
        """Strictly validate all payment, order, merchant, currency, result_code, source_mode,
        in-flight pending/unknown refund locks, and deterministic RiskEngine ledger invariants
        under database lock immediately before REFUND_EXECUTE."""
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

        if payment.source_mode != "MOCK":
            return self._deny(
                contract,
                "NON_MOCK_ENTITY_FORBIDDEN",
                f"Payment '{payment.id}' has source_mode='{payment.source_mode}', expected 'MOCK'",
                checked,
            )

        if not payment.merchant_id or payment.merchant_id != params.merchant_id:
            return self._deny(
                contract,
                "PAYMENT_MERCHANT_MISMATCH",
                f"Payment merchant_id '{payment.merchant_id}' does not match "
                f"proposal merchant_id '{params.merchant_id}'",
                checked,
            )
        checked.append("payment_merchant_verified")

        if payment.currency != params.currency:
            return self._deny(
                contract,
                "PAYMENT_CURRENCY_MISMATCH",
                f"Payment currency '{payment.currency}' does not match "
                f"proposal currency '{params.currency}'",
                checked,
            )
        checked.append("payment_currency_verified")

        if payment.result_code != 0:
            return self._deny(
                contract,
                "PAYMENT_RESULT_CODE_INVALID",
                f"Payment '{payment.id}' has non-zero result_code={payment.result_code}",
                checked,
            )
        checked.append("payment_result_code_zero")

        if params.order_id and payment.order_id != params.order_id:
            return self._deny(
                contract,
                "CROSS_ORDER_PAYMENT_MISMATCH",
                f"Payment '{payment.id}' does not belong to order '{params.order_id}'",
                checked,
            )

        target_order_id = params.order_id or payment.order_id
        if target_order_id:
            stmt_ord = select(SimOrder).where(
                SimOrder.id == target_order_id,
                SimOrder.session_id == self._session_id,
            )
            if lock_rows:
                stmt_ord = stmt_ord.with_for_update()
            order = self._db.scalar(stmt_ord)
            if order is None:
                return self._deny(
                    contract,
                    "ORDER_NOT_FOUND",
                    f"Order '{target_order_id}' linked to refund not found in session",
                    checked,
                )
            if order.source_mode != "MOCK":
                return self._deny(
                    contract,
                    "NON_MOCK_ENTITY_FORBIDDEN",
                    f"Order '{order.id}' has source_mode='{order.source_mode}', expected 'MOCK'",
                    checked,
                )
            if order.currency != payment.currency:
                return self._deny(
                    contract,
                    "ORDER_PAYMENT_CURRENCY_MISMATCH",
                    f"Order currency '{order.currency}' != payment currency '{payment.currency}'",
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

        settled_balance = sum(
            r.completed_amount for r in existing_refunds if r.status == RefundStatus.COMPLETED.value
        )
        in_flight_refunds = [
            r
            for r in existing_refunds
            if r.status in (RefundStatus.PENDING.value, RefundStatus.UNKNOWN.value)
        ]
        pending_or_unknown_balance = sum(r.requested_amount for r in in_flight_refunds)
        used_balance = settled_balance + pending_or_unknown_balance
        remaining_balance = payment.amount - used_balance

        # Hardening 2 (Prompt 04): Enforce RiskEngine safety inside PolicyEngine/ActionExecutor
        # under database lock. Reject any new refund when the payment has PENDING or UNKNOWN
        # refunds, even if available numeric balance remains! Never rely on frontend having
        # called /risks/evaluate-refund first.
        from app.risk.engine import RiskEngine

        risk_engine = RiskEngine(self._db, self._session_id)
        risk_eval = risk_engine.evaluate_refund_request(
            requested_amount=params.amount,
            payment_id=payment.id,
            order_id=target_order_id,
            expected_merchant_id=params.merchant_id,
            expected_currency=params.currency,
            case_id=case_id,
            evidence_refs=evidence_refs,
            persist_signals=False,
            lock_rows=lock_rows,
        )

        if in_flight_refunds:
            in_flight_statuses = sorted({r.status for r in in_flight_refunds})
            return self._deny(
                contract,
                "IN_FLIGHT_PENDING_REFUND",
                f"Payment '{payment.id}' has in-flight refund(s) with status "
                f"{in_flight_statuses} ({pending_or_unknown_balance} {payment.currency}). "
                "New refunds are rejected under lock until in-flight refunds are reconciled, "
                "even if numeric balance remains.",
                checked,
                snapshot={
                    "payment_id": payment.id,
                    "payment_amount": payment.amount,
                    "settled_refunds": settled_balance,
                    "pending_or_held_refunds": pending_or_unknown_balance,
                    "remaining_balance": remaining_balance,
                    "in_flight_refund_ids": [r.id for r in in_flight_refunds],
                    "in_flight_statuses": in_flight_statuses,
                    "requested_amount": params.amount,
                },
            )
        checked.append("no_in_flight_pending_or_unknown_refunds")

        if settled_balance >= payment.amount:
            return self._deny(
                contract,
                "ALREADY_REFUNDED_PAYMENT",
                f"Payment '{payment.id}' is already fully refunded "
                f"({settled_balance}/{payment.amount} {payment.currency})",
                checked,
                snapshot={
                    "payment_id": payment.id,
                    "payment_amount": payment.amount,
                    "settled_refunds": settled_balance,
                    "remaining_balance": 0,
                    "requested_amount": params.amount,
                },
            )

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

        if not risk_eval.allowed:
            primary_rule = (
                risk_eval.signals[0].rule_code if risk_eval.signals else "RISK_ENGINE_HOLD_ACTION"
            )
            return self._deny(
                contract,
                primary_rule,
                f"RiskEngine blocked refund ({primary_rule}): {risk_eval.explanation_vi}",
                checked,
                snapshot={
                    "payment_id": payment.id,
                    "recommended_response": risk_eval.recommended_response,
                    "remaining_balance": risk_eval.ledger.refundable_remainder,
                    "requested_amount": params.amount,
                },
            )

        checked.append(
            "refund_amount_within_remaining_eligible_balance_across_completed_pending_unknown"
        )
        checked.append("risk_engine_pre_execution_check_passed")

        return PolicyEvaluationResult(
            allowed=True,
            policy_version=POLICY_VERSION,
            action_type=ActionType.REFUND_EXECUTE.value,
            contract=contract,
            checked_preconditions=checked,
            context_snapshot={
                "payment_id": payment.id,
                "order_id": payment.order_id,
                "merchant_id": payment.merchant_id,
                "currency": payment.currency,
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
        for_execution: bool = False,
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

        # Hardening 2: Disable HOLD_ORDER execution so a no-op action never executes or resolves
        if for_execution:
            return self._deny(
                contract,
                "HOLD_ORDER_EXECUTION_DISABLED",
                "HOLD_ORDER execution is disabled until durable order-hold persistence is "
                "implemented; never executes or reports PASSED for a no-op action",
                checked,
            )

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

    def evaluate_commerce_recovery(
        self,
        *,
        binding: CommerceSourceBinding,
        obs: AnomalyObservation,
        command: str,
        request_hash: str,
        idempotency_key: str = "",
        expected_version: int | None = None,
        order_snapshot: dict[str, Any] | None = None,
        payment_snapshot: dict[str, Any] | None = None,
        target_event_id: str | None = None,
        approval_id: str | None = None,
        require_human_approval: bool = False,
    ) -> CommercePolicyDecision:
        """Deterministic PolicyEngine authorization for MiniStore recovery commands.

        Never trusts agent-supplied risk labels or Policy RAG text. Evaluates actual
        authoritative business state, scope, risk signals, and human approval records.
        Defaults to DENY whenever required evidence or authorization is missing.
        """
        norm_cmd = command.strip().upper()
        checked: list[str] = []

        def _deny_cmd(
            code: str,
            msg: str,
            risk: str = "HIGH",
            needs_approval: bool = True,
            snap: dict[str, Any] | None = None,
        ) -> CommercePolicyDecision:
            return CommercePolicyDecision(
                allowed=False,
                decision_code=f"DENY:{code}",
                policy_version=POLICY_VERSION,
                command=norm_cmd,
                risk_level=risk,
                requires_human_approval=needs_approval,
                violation_code=code,
                violation_message=msg,
                checked_preconditions=checked,
                context_snapshot=snap or {},
            )

        # 1. Scope & world-epoch isolation
        if binding.session_id != self._session_id or obs.session_id != self._session_id:
            return _deny_cmd(
                "SESSION_SCOPE_MISMATCH",
                "Commerce binding or anomaly does not belong to active session",
            )
        if not binding.is_enabled or binding.world_reset_required:
            return _deny_cmd(
                "INACTIVE_SOURCE_BINDING",
                f"CommerceSourceBinding '{binding.id}' is disabled or requires world reset",
            )
        if binding.commerce_world_epoch != obs.world_epoch:
            return _deny_cmd(
                "STALE_WORLD_EPOCH",
                f"Binding world_epoch ({binding.commerce_world_epoch}) != anomaly "
                f"world_epoch ({obs.world_epoch})",
            )
        checked.append("scope_and_world_epoch_verified")

        # 2. Source mode enforcement
        if self._settings.source_mode != SourceMode.MOCK:
            return _deny_cmd(
                "UNSUPPORTED_SOURCE_MODE",
                "Autonomous commerce recovery is restricted to SOURCE_MODE=MOCK",
            )
        checked.append("source_mode_is_mock")

        # 3. Command capability allowlist
        if norm_cmd not in COMMERCE_ALLOWED_COMMANDS:
            return _deny_cmd(
                "UNSUPPORTED_RECOVERY_COMMAND",
                f"Command '{norm_cmd}' is not an authorized recovery capability",
            )
        checked.append("command_in_capability_allowlist")

        # 4. Active HOLD_ACTION risk signal check on linked Case
        if obs.linked_case_id:
            case_obj = self._db.scalar(
                select(Case).where(
                    Case.id == obs.linked_case_id,
                    Case.session_id == self._session_id,
                )
            )
            if case_obj is not None and case_obj.state in (
                CaseState.RESOLVED.value,
                CaseState.FAILED.value,
            ):
                return _deny_cmd(
                    "CASE_ALREADY_TERMINAL",
                    f"Linked Case '{case_obj.id}' is in terminal state '{case_obj.state}'",
                )
            hold_signals = list(
                self._db.scalars(
                    select(RiskSignal).where(
                        RiskSignal.case_id == obs.linked_case_id,
                        RiskSignal.session_id == self._session_id,
                        RiskSignal.response == RiskResponse.HOLD_ACTION.value,
                    )
                ).all()
            )
            active_holds = [
                s
                for s in hold_signals
                if str(s.details_json.get("lifecycle_status") or "ACTIVE").upper() == "ACTIVE"
            ]
            if active_holds:
                return _deny_cmd(
                    "RISK_HOLD_ACTION_ACTIVE",
                    f"Active HOLD_ACTION risk signals block mutation: "
                    f"{[s.category for s in active_holds]}",
                )
            checked.append("no_active_hold_action_risk_signal")

        # 5. Command-specific authoritative evidence & deterministic risk evaluation
        derived_risk = "HIGH"
        needs_approval = require_human_approval
        decision_code = "ALLOW_POLICY_ENGINE_VERIFIED"
        snapshot: dict[str, Any] = {
            "binding_id": binding.id,
            "merchant_id": binding.merchant_id,
            "commerce_session_id": binding.commerce_session_id,
            "world_epoch": binding.commerce_world_epoch,
            "request_hash": request_hash,
        }

        if norm_cmd == "REQUEST_ORDER_SYNC":
            if not obs.affected_order_id or order_snapshot is None or payment_snapshot is None:
                return _deny_cmd(
                    "MISSING_AUTHORITATIVE_EVIDENCE",
                    "REQUEST_ORDER_SYNC requires authoritative order and payment snapshots",
                )
            o_id = str(order_snapshot.get("id") or "")
            o_status = str(order_snapshot.get("status") or "").upper()
            o_recon = str(order_snapshot.get("reconciliation_status") or "CLEAR").upper()
            o_ver = order_snapshot.get("version")
            p_id = str(payment_snapshot.get("id") or payment_snapshot.get("payment_id") or "")
            p_order_id = str(payment_snapshot.get("order_id") or "")
            p_status = str(payment_snapshot.get("status") or "").upper()
            p_provider = str(payment_snapshot.get("provider") or "").upper()
            p_synced = bool(payment_snapshot.get("is_synced_to_order", False))
            p_recon = bool(payment_snapshot.get("reconciliation_required", False))

            if o_id != obs.affected_order_id or p_order_id != o_id:
                return _deny_cmd(
                    "TARGET_ENTITY_MISMATCH",
                    f"Payment '{p_id}' (order={p_order_id}) does not match target order "
                    f"'{obs.affected_order_id}'",
                )
            if p_provider != "LOCAL_MOCK":
                return _deny_cmd(
                    "EXTERNAL_PROVIDER_WRITE_FORBIDDEN",
                    f"Autonomous sync is forbidden for external provider '{p_provider}'",
                )
            if o_status == "CANCELLED":
                return _deny_cmd(
                    "CANCELLED_ORDER_RESURRECTION_FORBIDDEN",
                    "Cancelled orders MUST NOT be resurrected by automated sync",
                )
            if o_status != "PENDING_PAYMENT":
                return _deny_cmd(
                    "INVALID_ORDER_STATE_FOR_SYNC",
                    f"Order '{o_id}' has status '{o_status}', expected 'PENDING_PAYMENT'",
                )
            if p_status != "SUCCEEDED" or p_synced:
                return _deny_cmd(
                    "PAYMENT_NOT_ELIGIBLE_FOR_SYNC",
                    f"Payment '{p_id}' is not an unsynced SUCCEEDED payment "
                    f"(status={p_status}, is_synced={p_synced})",
                )
            if p_recon or o_recon == "RECONCILIATION_REQUIRED":
                return _deny_cmd(
                    "RECONCILIATION_CONFLICT_BLOCKS_SYNC",
                    "Reconciliation conflict present; human finance review required",
                )
            if expected_version is None or not isinstance(o_ver, int) or o_ver != expected_version:
                return _deny_cmd(
                    "STALE_OR_MISSING_ORDER_VERSION",
                    f"Order version mismatch or missing: expected={expected_version}, "
                    f"authoritative={o_ver}",
                )
            checked.append("authoritative_local_mock_sync_preconditions_verified")
            derived_risk = "LOW"
            decision_code = "ALLOW_POLICY_ENGINE_LOCAL_MOCK_LOW_RISK"
            snapshot.update({"order_id": o_id, "payment_id": p_id, "order_version": o_ver})

        elif norm_cmd == "REQUEST_EVENT_REDELIVERY":
            if not target_event_id and not obs.affected_order_id:
                return _deny_cmd(
                    "SCOPED_TARGET_REQUIRED",
                    "REQUEST_EVENT_REDELIVERY requires explicit event_id or order_id",
                )
            checked.append("scoped_event_redelivery_target_verified")
            derived_risk = "LOW"
            decision_code = "ALLOW_POLICY_ENGINE_SCOPED_REDELIVERY"
            snapshot.update({"event_id": target_event_id, "order_id": obs.affected_order_id})

        elif norm_cmd == "REQUEST_PAYMENT_RECONCILIATION":
            if not obs.affected_order_id or order_snapshot is None:
                return _deny_cmd(
                    "MISSING_AUTHORITATIVE_EVIDENCE",
                    "REQUEST_PAYMENT_RECONCILIATION requires authoritative order snapshot",
                )
            o_ver = order_snapshot.get("version")
            if (
                expected_version is not None
                and isinstance(o_ver, int)
                and o_ver != expected_version
            ):
                return _deny_cmd(
                    "STALE_ORDER_VERSION",
                    f"Order version mismatch: expected={expected_version}, authoritative={o_ver}",
                )
            checked.append("reconciliation_flag_preconditions_verified")
            derived_risk = "LOW"
            decision_code = "ALLOW_POLICY_ENGINE_RECONCILIATION_FLAG"
            snapshot.update({"order_id": obs.affected_order_id, "order_version": o_ver})

        elif norm_cmd == "REQUEST_MANUAL_REVIEW":
            checked.append("manual_review_escalation_non_mutating")
            derived_risk = "LOW"
            decision_code = "ALLOW_POLICY_ENGINE_MANUAL_REVIEW"

        # 6. Validate human Approval record when required (or when approval_id is supplied)
        if needs_approval or approval_id is not None:
            if not approval_id:
                return _deny_cmd(
                    "HUMAN_APPROVAL_REQUIRED",
                    f"Command '{norm_cmd}' requires a valid, unexpired human Approval record",
                    risk=derived_risk,
                    needs_approval=True,
                )
            approval = self._db.scalar(select(Approval).where(Approval.id == approval_id))
            if approval is None:
                return _deny_cmd(
                    "APPROVAL_NOT_FOUND",
                    f"Approval '{approval_id}' does not exist",
                    risk=derived_risk,
                    needs_approval=True,
                )
            if approval.decision != ApprovalDecision.APPROVED.value:
                return _deny_cmd(
                    "APPROVAL_NOT_APPROVED",
                    f"Approval '{approval_id}' has decision '{approval.decision}'",
                    risk=derived_risk,
                    needs_approval=True,
                )
            now = datetime.now(UTC)
            exp = approval.expiry
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if exp <= now:
                return _deny_cmd(
                    "APPROVAL_EXPIRED",
                    f"Approval '{approval_id}' expired at {exp.isoformat()}",
                    risk=derived_risk,
                    needs_approval=True,
                )
            proposal = self._db.scalar(
                select(ActionProposal).where(
                    ActionProposal.id == approval.proposal_id,
                    ActionProposal.session_id == self._session_id,
                )
            )
            if proposal is None:
                return _deny_cmd(
                    "APPROVAL_SCOPE_MISMATCH",
                    f"Approval '{approval_id}' does not belong to active session",
                    risk=derived_risk,
                    needs_approval=True,
                )
            if obs.linked_case_id and proposal.case_id and proposal.case_id != obs.linked_case_id:
                return _deny_cmd(
                    "APPROVAL_CASE_MISMATCH",
                    f"Approval '{approval_id}' is bound to Case '{proposal.case_id}', "
                    f"not '{obs.linked_case_id}'",
                    risk=derived_risk,
                    needs_approval=True,
                )
            prop_params = proposal.params_json or {}
            prop_order = prop_params.get("order_id")
            if prop_order and obs.affected_order_id and prop_order != obs.affected_order_id:
                return _deny_cmd(
                    "APPROVAL_TARGET_MISMATCH",
                    f"Approval target order '{prop_order}' != '{obs.affected_order_id}'",
                    risk=derived_risk,
                    needs_approval=True,
                )
            prop_hash = str(prop_params.get("request_hash") or "").strip()
            if not prop_hash:
                return _deny_cmd(
                    "APPROVAL_MISSING_REQUEST_HASH",
                    "Approval proposal must cryptographically bind request_hash",
                    risk=derived_risk,
                    needs_approval=True,
                )
            if not hmac.compare_digest(prop_hash, request_hash):
                return _deny_cmd(
                    "APPROVAL_REQUEST_HASH_MISMATCH",
                    "Approval request_hash does not match command payload hash",
                    risk=derived_risk,
                    needs_approval=True,
                )
            checked.append("human_approval_record_verified")

        signing_secret = self._settings.command_auth_signing_secret.get_secret_value().strip()
        if not signing_secret:
            return _deny_cmd(
                "SIGNING_SECRET_UNAVAILABLE",
                "Command authorization signing secret is not configured (fail closed)",
                risk=derived_risk,
                needs_approval=needs_approval,
            )

        now_ts = datetime.now(UTC)
        exp_ts = now_ts + timedelta(seconds=self._settings.command_auth_ttl_seconds)
        auth_id = f"AUTH-{uuid.uuid4().hex[:12].upper()}"
        auth_payload = {
            "auth_id": auth_id,
            "key_id": self._settings.command_auth_key_id,
            "policy_version": POLICY_VERSION,
            "policy_decision": decision_code,
            "command": norm_cmd,
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "merchant_id": binding.merchant_id,
            "commerce_session_id": binding.commerce_session_id,
            "world_epoch": binding.commerce_world_epoch,
            "source_binding_id": binding.id,
            "target_order_id": obs.affected_order_id,
            "target_payment_id": obs.affected_payment_id,
            "target_event_id": target_event_id,
            "expected_version": expected_version,
            "approval_id": approval_id,
            "issued_at": int(now_ts.timestamp()),
            "expires_at": int(exp_ts.timestamp()),
        }
        canonical_bytes = json.dumps(
            auth_payload, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
        sig_hex = hmac.new(
            signing_secret.encode("utf-8"), canonical_bytes, hashlib.sha256
        ).hexdigest()
        b64_payload = base64.urlsafe_b64encode(canonical_bytes).decode("ascii").rstrip("=")
        auth_token = f"{b64_payload}.{sig_hex}"

        return CommercePolicyDecision(
            allowed=True,
            decision_code=decision_code,
            policy_version=POLICY_VERSION,
            command=norm_cmd,
            risk_level=derived_risk,
            requires_human_approval=needs_approval,
            authorization_id=auth_id,
            authorization_token=auth_token,
            authorization_digest=sig_hex,
            approval_id=approval_id,
            checked_preconditions=checked,
            context_snapshot=snapshot,
        )
