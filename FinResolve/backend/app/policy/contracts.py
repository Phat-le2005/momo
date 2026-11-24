"""Versioned BusinessActionContract definitions for FinResolve AI.

Hard Domain Invariants:
1. Every write action proposal must map to a registered BusinessActionContract.
2. Defines strict Pydantic v2 input schemas, required authoritative evidence sources,
   preconditions, required approver roles, risk level, side effects, and expected postconditions.
3. Restricts all financial/order mutations to `source_mode == "MOCK"` in the MVP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ai_core.schemas import is_strict_int
from app.core.errors import AppError, ErrorCode
from app.models.enums import ActionType, RiskLevel

POLICY_VERSION = "FinResolve-Policy-v1.2"


class OrderSyncActionParams(BaseModel):
    """Strict parameter contract for ORDER_SYNC / SYNC_PAID_ORDER."""

    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(..., min_length=3, max_length=64)
    payment_id: str = Field(..., min_length=3, max_length=64)
    amount: int = Field(..., gt=0, le=100_000_000_000)
    currency: str = Field(default="VND", pattern=r"^(VND|USD)$")
    expected_order_version: int = Field(default=1, ge=1, le=1_000_000)
    merchant_id: str = Field(default="MERCHANT_MOCK_01", min_length=3, max_length=64)

    @field_validator("amount", "expected_order_version", mode="before")
    @classmethod
    def _reject_boolean_ints(cls, value: Any) -> Any:
        if not is_strict_int(value):
            raise ValueError("Value must be a strict integer (booleans are not allowed)")
        return value


class RefundActionParams(BaseModel):
    """Strict parameter contract for REFUND_EXECUTE / REFUND_PROPOSAL."""

    model_config = ConfigDict(extra="forbid")

    payment_id: str = Field(..., min_length=3, max_length=64)
    order_id: str | None = Field(default=None, max_length=64)
    amount: int = Field(..., gt=0, le=100_000_000_000)
    currency: str = Field(default="VND", pattern=r"^(VND|USD)$")
    reason: str = Field(..., min_length=3, max_length=512)
    merchant_id: str = Field(default="MERCHANT_MOCK_01", min_length=3, max_length=64)

    @field_validator("amount", mode="before")
    @classmethod
    def _reject_boolean_amount(cls, value: Any) -> Any:
        if not is_strict_int(value):
            raise ValueError("Amount must be a strict integer (booleans are not allowed)")
        return value


class HoldOrderActionParams(BaseModel):
    """Strict parameter contract for HOLD_ORDER."""

    model_config = ConfigDict(extra="forbid")

    order_id: str = Field(..., min_length=3, max_length=64)
    reason: str = Field(default="Tạm giữ đơn hàng để kiểm tra rủi ro", min_length=3, max_length=512)


@dataclass(frozen=True)
class BusinessActionContract:
    """Formal contract governing a business action from proposal through verification."""

    action_type: str
    aliases: frozenset[str]
    policy_version: str
    input_schema: type[BaseModel]
    required_evidence_sources: frozenset[str]
    preconditions: tuple[str, ...]
    required_approver_roles: frozenset[str]
    default_risk_level: RiskLevel
    allowed_source_modes: frozenset[str]
    side_effects: tuple[str, ...]
    expected_postconditions: tuple[str, ...]

    def validate_params(self, params_json: dict[str, Any]) -> BaseModel:
        return self.input_schema.model_validate(params_json)


ORDER_SYNC_CONTRACT = BusinessActionContract(
    action_type=ActionType.ORDER_SYNC.value,
    aliases=frozenset(
        {
            ActionType.ORDER_SYNC.value,
            "SYNC_PAID_ORDER",
            "MARK_ORDER_PAYMENT_CONFIRMED",
            "propose_order_sync",
        }
    ),
    policy_version=POLICY_VERSION,
    input_schema=OrderSyncActionParams,
    required_evidence_sources=frozenset({"SIM_ORDERS_DB", "PROVIDER_LEDGER_MOCK"}),
    preconditions=(
        "session_ownership_verified",
        "source_mode_is_mock",
        "authoritative_observed_fact_evidence_present",
        "payment_status_is_verified_success",
        "payment_bound_to_exact_order_id",
        "payment_merchant_and_amount_match_order",
        "order_status_is_pending_and_unpaid",
        "order_version_matches_expected_order_version",
        "no_active_hold_action_risk_signal",
    ),
    required_approver_roles=frozenset({"OPS_MANAGER", "FINANCE_LEAD", "DEMO_MANAGER"}),
    default_risk_level=RiskLevel.LOW,
    allowed_source_modes=frozenset({"MOCK"}),
    side_effects=(
        "update_sim_order_status_to_paid",
        "update_sim_order_payment_status_to_paid",
        "increment_sim_order_version",
        "record_action_execution_and_case_event",
    ),
    expected_postconditions=(
        "action_execution_state_is_executed",
        "sim_order_status_is_paid",
        "sim_order_payment_status_is_paid",
        "sim_order_version_incremented_by_one",
        "sim_payment_ remains_verified_success_and_matches_order",
    ),
)

REFUND_CONTRACT = BusinessActionContract(
    action_type=ActionType.REFUND_EXECUTE.value,
    aliases=frozenset(
        {
            ActionType.REFUND_EXECUTE.value,
            "REFUND_PROPOSAL",
            "propose_refund",
        }
    ),
    policy_version=POLICY_VERSION,
    input_schema=RefundActionParams,
    required_evidence_sources=frozenset({"PROVIDER_LEDGER_MOCK"}),
    preconditions=(
        "session_ownership_verified",
        "source_mode_is_mock",
        "payment_status_is_verified_success",
        "refund_amount_within_remaining_eligible_balance_across_completed_pending_unknown",
        "no_active_hold_action_risk_signal",
    ),
    required_approver_roles=frozenset({"FINANCE_LEAD", "DEMO_MANAGER"}),
    default_risk_level=RiskLevel.HIGH,
    allowed_source_modes=frozenset({"MOCK"}),
    side_effects=(
        "insert_sim_refund_completed_record",
        "update_sim_order_payment_status_to_refunded_or_partially_refunded",
        "record_action_execution_and_case_event",
    ),
    expected_postconditions=(
        "action_execution_state_is_executed",
        "sim_refund_status_is_completed_and_amount_matches",
        "total_completed_refunds_do_not_exceed_payment_amount",
    ),
)

HOLD_ORDER_CONTRACT = BusinessActionContract(
    action_type=ActionType.HOLD_ORDER.value,
    aliases=frozenset({ActionType.HOLD_ORDER.value}),
    policy_version=POLICY_VERSION,
    input_schema=HoldOrderActionParams,
    required_evidence_sources=frozenset({"SIM_ORDERS_DB"}),
    preconditions=(
        "session_ownership_verified",
        "source_mode_is_mock",
        "order_exists_in_session",
    ),
    required_approver_roles=frozenset({"OPS_MANAGER", "FINANCE_LEAD", "DEMO_MANAGER"}),
    default_risk_level=RiskLevel.MEDIUM,
    allowed_source_modes=frozenset({"MOCK"}),
    side_effects=("record_order_hold_execution",),
    expected_postconditions=("action_execution_state_is_executed",),
)

_CONTRACTS_BY_ALIAS: dict[str, BusinessActionContract] = {}
for _contract in (ORDER_SYNC_CONTRACT, REFUND_CONTRACT, HOLD_ORDER_CONTRACT):
    for _alias in _contract.aliases:
        _CONTRACTS_BY_ALIAS[_alias] = _contract


def get_action_contract(action_type: str) -> BusinessActionContract:
    """Resolve a BusinessActionContract by canonical action_type or alias."""
    contract = _CONTRACTS_BY_ALIAS.get(action_type.strip())
    if contract is None:
        raise AppError(
            ErrorCode.BAD_REQUEST,
            f"Unsupported or unregistered business action type '{action_type}'",
            status_code=400,
        )
    return contract
