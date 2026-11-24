"""Pydantic schemas for Case, Evidence, CaseEvent, Status, and AI Investigation."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class CreateCaseRequest(BaseModel):
    text: str = Field(
        ...,
        min_length=3,
        max_length=2000,
        description="Nội dung khiếu nại tiếng Việt từ khách hàng hoặc sự kiện kinh doanh (CLAIM)",
    )
    goal: str | None = Field(
        default=None,
        max_length=512,
        description="Mục tiêu điều tra nếu được chỉ định trước",
    )
    type: str = Field(
        default="PAYMENT_DISPUTE",
        max_length=64,
        description="Loại sự cố (PAYMENT_DISPUTE, REFUND_REQUEST, ORDER_SYNC)",
    )
    idempotency_key: str | None = Field(
        default=None,
        max_length=128,
        description="Khóa chống trùng lặp từ client khi gửi lại request",
    )
    auto_investigate: bool = Field(
        default=False,
        description=(
            "Nếu bật cùng Autopilot, AI Supervisor sẽ tự động xử lý ngay tác vụ điều tra vừa tạo"
        ),
    )


class EvidenceResponse(BaseModel):
    id: str
    case_id: str
    kind: str
    source: str
    source_ref: str
    payload_json: dict[str, Any]
    confidence: float | None = None
    observed_at: datetime


class CaseEventResponse(BaseModel):
    id: str
    case_id: str
    type: str
    payload_json: dict[str, Any]
    created_at: datetime


class ActionProposalSummaryResponse(BaseModel):
    id: str
    case_id: str | None
    action_type: str
    params_json: dict[str, Any]
    evidence_refs: list[str]
    expected_version: int
    risk_level: str
    status: str
    created_at: datetime


class RiskSignalSummaryResponse(BaseModel):
    id: str
    case_id: str
    category: str
    level: str
    evidence_refs: list[str]
    response: str
    details_json: dict[str, Any]
    created_at: datetime


class VerificationSummaryResponse(BaseModel):
    id: str
    case_id: str | None
    action_execution_id: str | None
    case_version: int
    verdict: str
    postconditions_json: dict[str, Any]
    checked_at: datetime


class CaseResponse(BaseModel):
    id: str
    session_id: str
    type: str
    goal: str
    state: str
    version: int
    owner: str
    idempotency_key: str | None = None
    created_at: datetime
    updated_at: datetime
    evidence: list[EvidenceResponse] = []
    action_proposals: list[ActionProposalSummaryResponse] = []
    risk_signals: list[RiskSignalSummaryResponse] = []
    verifications: list[VerificationSummaryResponse] = []
    unknowns: list[str] = []
    latest_decision_summary: str | None = None
    model_mode: str | None = None


class CaseStatusResponse(BaseModel):
    case_id: str
    state: str
    version: int
    updated_at: datetime
    model_mode: str | None = None
    latest_decision_summary: str | None = None
    pending_proposals_count: int = 0
    active_run_id: str | None = None
    investigation_status: Literal["IDLE", "RUNNING", "COMPLETED", "FAILED", "INTERRUPTED"] = "IDLE"


class InvestigateCaseRequest(BaseModel):
    max_steps: int = Field(default=6, ge=1, le=10)
    simulate_timeout: bool = Field(
        default=False,
        description="Cờ mô phỏng timeout từ cổng thanh toán để kiểm thử QA T04 (UNKNOWN_OUTCOME)",
    )
    run_async: bool = Field(
        default=False,
        description="Chạy điều tra bất đồng bộ trong background worker và trả về run_id để polling",
    )


class InvestigationJobStatusResponse(BaseModel):
    run_id: str
    case_id: str
    session_id: str
    job_status: Literal["RUNNING", "COMPLETED", "FAILED", "INTERRUPTED"]
    case_state: str
    case_version: int
    model_mode: str
    steps_executed: int = 0
    stop_reason: str | None = None
    decision_summary: str | None = None
    proposed_action_ids: list[str] = []
    risk_signal_ids: list[str] = []
    error_message: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
