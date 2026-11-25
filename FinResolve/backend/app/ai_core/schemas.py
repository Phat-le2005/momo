"""Pydantic v2 schemas for the Bounded AI Planner, Vietnamese understanding, and runtime results.

Hard Domain Invariants:
1. Strict schema validation (extra="forbid"): rejects any attempt by an LLM to inject
   privileged fields such as `payment_verified`, `is_trusted`, `override_policy`, or `auto_execute`.
2. Strict tool allowlist: `READ_TOOL` can ONLY invoke the 7 authorized read-only tools.
3. No write execution from AI: `PROPOSE_ACTION` can only propose `propose_order_sync`,
   `propose_refund`, or `HOLD_ORDER`, with strictly validated argument types and references.
4. No SSRF / arbitrary URLs: rejects any tool arguments containing URLs (`http://`, `https://`)
   or cross-session overrides (`session_id`, `tenant_id`).
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, TypeGuard

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import ModelMode

ALLOWED_READ_TOOLS: frozenset[str] = frozenset(
    {
        "get_order",
        "find_payment_candidates",
        "get_payment_record",
        "get_refund_history",
        "get_inventory",
        "get_policy",
        "get_order_events",
        "get_operational_telemetry",
        "get_incident_experience",
    }
)

ALLOWED_PROPOSAL_ACTIONS: frozenset[str] = frozenset(
    {
        "propose_order_sync",
        "propose_refund",
        "ORDER_SYNC",
        "REFUND_EXECUTE",
        "HOLD_ORDER",
    }
)

FORBIDDEN_PRIVILEGE_KEYS: frozenset[str] = frozenset(
    {
        "payment_verified",
        "is_trusted",
        "adapter_verified",
        "override_policy",
        "auto_execute",
        "bypass_approval",
        "force_resolve",
        "is_authorized",
        "session_id",
        "tenant_id",
        "raw_sql",
    }
)

_URL_PATTERN = re.compile(
    r"(?:https?|ftp|file|ws|wss|gopher|javascript|data)://",
    re.IGNORECASE,
)

_STRING_ARG_KEYS: frozenset[str] = frozenset(
    {
        "order_id",
        "payment_id",
        "provider_ref",
        "customer_phone",
        "product_id",
        "sku",
        "category",
        "reason",
        "question",
        "risk_category",
        "status",
        "observed_payment_status",
    }
)

_IDENTIFIER_ARG_KEYS: frozenset[str] = frozenset(
    {
        "order_id",
        "payment_id",
        "provider_ref",
        "customer_phone",
        "product_id",
        "sku",
    }
)

_INT_ARG_KEYS: frozenset[str] = frozenset(
    {
        "amount",
        "requested_amount",
        "remaining_refundable_amount",
        "expected_order_version",
        "order_amount",
        "payment_amount",
    }
)


def is_strict_int(value: Any) -> TypeGuard[int]:
    """Return True if value is an integer and NOT a boolean (since bool subclasses int)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _scan_dict_for_violations(data: dict[str, Any], path: str = "args") -> None:
    """Recursively inspect dictionary arguments for forbidden privilege keys and arbitrary URLs."""
    for key, value in data.items():
        key_lower = str(key).strip().lower()
        if key_lower in FORBIDDEN_PRIVILEGE_KEYS:
            raise ValueError(
                f"Forbidden privileged or scoping field '{key}' is not permitted in planner {path}"
            )
        if isinstance(value, str):
            if _URL_PATTERN.search(value):
                raise ValueError(
                    f"Arbitrary URL detected in '{path}.{key}'; external URLs are forbidden"
                )
        elif isinstance(value, dict):
            _scan_dict_for_violations(value, path=f"{path}.{key}")
        elif isinstance(value, list):
            for idx, item in enumerate(value):
                if isinstance(item, str) and _URL_PATTERN.search(item):
                    raise ValueError(
                        f"Arbitrary URL detected in '{path}.{key}[{idx}]'; "
                        "external URLs are forbidden"
                    )
                elif isinstance(item, dict):
                    _scan_dict_for_violations(item, path=f"{path}.{key}[{idx}]")


def _validate_arg_types(args: dict[str, Any]) -> None:
    """Validate scalar argument types in next_step.args to prevent malformed model payloads."""
    for key, val in args.items():
        if val is None:
            continue
        if key in _STRING_ARG_KEYS:
            if not isinstance(val, str):
                raise ValueError(f"Argument '{key}' must be a string, got {type(val).__name__}")
            stripped = val.strip()
            if key in _IDENTIFIER_ARG_KEYS and (not stripped or len(stripped) > 128):
                raise ValueError(
                    f"Identifier argument '{key}' must be a non-empty string <= 128 chars"
                )
            if len(stripped) > 1000:
                raise ValueError(f"String argument '{key}' exceeds maximum length of 1000 chars")
        elif key in _INT_ARG_KEYS:
            if not is_strict_int(val):
                raise ValueError(
                    f"Argument '{key}' must be a strict integer (not {type(val).__name__})"
                )
            if key in ("amount", "requested_amount", "order_amount", "payment_amount") and (
                val <= 0 or val > 100_000_000_000
            ):
                raise ValueError(
                    f"Monetary argument '{key}' must be between 1 and 100,000,000,000 VND"
                )
            if key == "remaining_refundable_amount" and (val < 0 or val > 100_000_000_000):
                raise ValueError(f"Argument '{key}' must be a non-negative integer")
            if key == "expected_order_version" and (val < 1 or val > 1_000_000):
                raise ValueError("Argument 'expected_order_version' must be >= 1")
        elif key == "simulate_timeout":
            if not isinstance(val, bool):
                raise ValueError("Argument 'simulate_timeout' must be a boolean")
        elif key == "candidate_refs":
            if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
                raise ValueError("Argument 'candidate_refs' must be a list of strings")


class PlanStepKind(StrEnum):
    READ_TOOL = "READ_TOOL"
    ASK_USER = "ASK_USER"
    PROPOSE_ACTION = "PROPOSE_ACTION"
    ESCALATE = "ESCALATE"
    STOP = "STOP"


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=2, max_length=128)
    supporting_evidence_refs: list[str] = Field(default_factory=list)


class NextStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: PlanStepKind
    tool_name: str | None = Field(default=None, max_length=64)
    action_type: str | None = Field(default=None, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_step_safety(self) -> NextStep:
        # 1. Inspect args recursively for privilege escalation keys or arbitrary URLs
        _scan_dict_for_violations(self.args, path="next_step.args")
        _validate_arg_types(self.args)

        if self.tool_name and _URL_PATTERN.search(self.tool_name):
            raise ValueError("tool_name must not contain a URL")
        if self.action_type and _URL_PATTERN.search(self.action_type):
            raise ValueError("action_type must not contain a URL")

        # 2. Enforce strict READ_TOOL allowlist
        if self.kind == PlanStepKind.READ_TOOL:
            if not self.tool_name:
                raise ValueError("next_step.tool_name is required when kind is READ_TOOL")
            if self.tool_name not in ALLOWED_READ_TOOLS:
                raise ValueError(
                    f"Unauthorized or unknown read tool '{self.tool_name}'. "
                    f"Allowed read tools: {sorted(ALLOWED_READ_TOOLS)}"
                )

        # 3. Enforce strict PROPOSE_ACTION allowlist & required argument references
        elif self.kind == PlanStepKind.PROPOSE_ACTION:
            chosen_action = self.action_type or self.tool_name
            if not chosen_action:
                raise ValueError(
                    "next_step.action_type (or tool_name) is required when kind is PROPOSE_ACTION"
                )
            if chosen_action not in ALLOWED_PROPOSAL_ACTIONS:
                raise ValueError(
                    f"Unauthorized action proposal '{chosen_action}'. "
                    f"AI may only propose: {sorted(ALLOWED_PROPOSAL_ACTIONS)}"
                )
            self.action_type = chosen_action
            if self.tool_name is None:
                self.tool_name = chosen_action

            if chosen_action in ("propose_order_sync", "ORDER_SYNC"):
                order_id = self.args.get("order_id")
                payment_id = self.args.get("payment_id")
                if not isinstance(order_id, str) or not order_id.strip():
                    raise ValueError(
                        "PROPOSE_ACTION for ORDER_SYNC requires a non-empty string 'order_id'"
                    )
                if not isinstance(payment_id, str) or not payment_id.strip():
                    raise ValueError(
                        "PROPOSE_ACTION for ORDER_SYNC requires a non-empty string 'payment_id'"
                    )
            elif chosen_action in ("propose_refund", "REFUND_EXECUTE"):
                payment_id = self.args.get("payment_id")
                amount = self.args.get("amount")
                if not isinstance(payment_id, str) or not payment_id.strip():
                    raise ValueError(
                        "PROPOSE_ACTION for REFUND_EXECUTE requires a non-empty string 'payment_id'"
                    )
                if not is_strict_int(amount) or int(amount) <= 0:
                    raise ValueError(
                        "PROPOSE_ACTION for REFUND_EXECUTE requires a positive integer 'amount'"
                    )

        return self


class Plan(BaseModel):
    """Bounded Planner JSON contract as specified in docs/PRODUCT_CONTRACT.md."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(..., min_length=3, max_length=64)
    goal: str = Field(..., min_length=3, max_length=512)
    known_evidence_refs: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    next_step: NextStep
    decision_summary: str = Field(..., min_length=3, max_length=1000)

    @model_validator(mode="before")
    @classmethod
    def _reject_privileged_assertions(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for key in data:
                if str(key).strip().lower() in FORBIDDEN_PRIVILEGE_KEYS:
                    raise ValueError(
                        f"Model output attempted to set privileged field '{key}', "
                        "which violates domain safety invariants"
                    )
        return data


class IntentCategory(StrEnum):
    PAYMENT_SYNC = "PAYMENT_SYNC"
    REFUND_DISPUTE = "REFUND_DISPUTE"
    INVENTORY_OR_FULFILLMENT = "INVENTORY_OR_FULFILLMENT"
    GENERAL_EXCEPTION = "GENERAL_EXCEPTION"


class VietnameseCaseUnderstanding(BaseModel):
    """Structured extraction from untrusted Vietnamese customer complaint text.

    Invariant: Everything extracted here comes from a CLAIM and carries zero payment authority.
    """

    model_config = ConfigDict(extra="forbid")

    intent: IntentCategory
    extracted_order_ids: list[str] = Field(default_factory=list)
    extracted_payment_refs: list[str] = Field(default_factory=list)
    extracted_amounts_vnd: list[int] = Field(default_factory=list)
    extracted_phones: list[str] = Field(default_factory=list)
    extracted_skus: list[str] = Field(default_factory=list)
    claimed_paid: bool = False
    claimed_refund_requested: bool = False
    prompt_injection_detected: bool = False
    prompt_injection_patterns: list[str] = Field(default_factory=list)
    summary_vi: str


class ToolObservationRecord(BaseModel):
    """Summary of an executed read tool step recorded in context and audit logs."""

    step: int
    tool_name: str
    args: dict[str, Any]
    status: Literal["OK", "NOT_FOUND", "TIMEOUT", "ERROR"]
    evidence_id: str
    source: str
    source_ref: str
    source_mode: str = "MOCK"
    freshness_ms: int = 0
    observed_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class InvestigationStepTrace(BaseModel):
    """Single step trace in a bounded investigation run."""

    step_number: int
    model_mode: ModelMode
    adapter_name: str
    is_fallback: bool
    plan: Plan
    tool_observation: ToolObservationRecord | None = None


class InvestigationRunResult(BaseModel):
    """Complete result returned after executing a bounded investigation on a Case."""

    run_id: str
    case_id: str
    session_id: str
    final_state: str
    case_version: int
    model_mode: ModelMode
    adapter_name: str
    is_fallback: bool
    steps_executed: int
    replans_used: int
    total_tokens_used: int
    stop_reason: str
    decision_summary: str
    known_evidence_refs: list[str]
    unknowns: list[str]
    hypotheses: list[Hypothesis]
    understanding: VietnameseCaseUnderstanding
    step_traces: list[InvestigationStepTrace]
    proposed_action_ids: list[str] = Field(default_factory=list)
    risk_signal_ids: list[str] = Field(default_factory=list)
