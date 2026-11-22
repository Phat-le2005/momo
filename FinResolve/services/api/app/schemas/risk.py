"""Pydantic schemas for Risk Center APIs and deterministic refund safety evaluations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RiskSignalDetailResponse(BaseModel):
    id: str
    case_id: str
    session_id: str
    category: str
    level: str
    response: str
    lifecycle_status: str = "ACTIVE"
    rule_code: str | None = None
    explanation_vi: str | None = None
    evidence_refs: list[str]
    source_refs: list[str] = []
    details_json: dict[str, Any]
    created_at: datetime


class RiskRuleDocResponse(BaseModel):
    rule_code: str
    category: str
    severity: str
    default_response: str
    description_vi: str
    known_blind_spots: str


class RiskCenterListResponse(BaseModel):
    session_id: str
    source_mode: str = "MOCK"
    policy_version: str = "FinResolve-Policy-v1.2"
    total_count: int
    hold_action_count: int
    active_hold_action_count: int = 0
    cleared_count: int = 0
    review_count: int
    observe_count: int
    items: list[RiskSignalDetailResponse]
    rules_catalog: list[RiskRuleDocResponse]


class ResolveRiskSignalRequest(BaseModel):
    new_response: str = Field(
        default="REVIEW",
        pattern=r"^(REVIEW|OBSERVE)$",
        description="Target non-blocking response after manager review (REVIEW or OBSERVE)",
    )
    resolution_note: str = Field(
        ...,
        min_length=5,
        max_length=1000,
        description="Mandatory audit justification for clearing HOLD_ACTION",
    )


class SeedRiskScenariosRequest(BaseModel):
    force_new: bool = Field(
        default=False,
        description="If false (default), replays existing seeded scenario pair for the session",
    )


class EvaluateRefundRiskRequest(BaseModel):
    payment_id: str | None = Field(default=None, max_length=64)
    order_id: str | None = Field(default=None, max_length=64)
    requested_amount: int = Field(..., gt=0, le=100_000_000_000)
    merchant_id: str = Field(default="MERCHANT_MOCK_01", max_length=64)
    currency: str = Field(default="VND", pattern=r"^(VND|USD)$")
    request_id: str | None = Field(default=None, max_length=128)
    case_id: str | None = Field(default=None, max_length=64)
    has_ambiguity: bool = Field(default=False)


class RefundBalanceLedgerResponse(BaseModel):
    payment_id: str | None
    order_id: str | None
    currency: str
    payment_status: str
    payment_verified: bool
    eligible_amount: int
    settled_refunds: int
    pending_or_held_refunds: int
    refundable_remainder: int
    refund_records_count: int
    source_refs: list[str]


class RefundRiskEvaluationResponse(BaseModel):
    allowed: bool
    recommended_response: str
    requested_amount: int
    explanation_vi: str
    ledger: RefundBalanceLedgerResponse
    signals: list[dict[str, Any]]


class SeededRiskScenariosResponse(BaseModel):
    session_id: str
    source_mode: str = "MOCK"
    idempotent_replay: bool = False
    valid_partial_refund_case_id: str
    valid_partial_refund_proposal_id: str
    valid_partial_refund_order_id: str
    valid_partial_refund_payment_id: str
    blocked_duplicate_refund_case_id: str
    blocked_duplicate_refund_order_id: str
    blocked_risk_signal_ids: list[str]
