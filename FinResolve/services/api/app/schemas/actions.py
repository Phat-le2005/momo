"""Pydantic schemas for Action Approval, Execution, Verification, and Contract inspection."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import ApprovalDecision


class ApproveActionRequest(BaseModel):
    decision: ApprovalDecision = Field(
        default=ApprovalDecision.APPROVED,
        description="Quyết định phê duyệt (APPROVED hoặc REJECTED)",
    )
    approver_role: str = Field(
        default="DEMO_MANAGER",
        min_length=2,
        max_length=64,
        description="Vai trò phê duyệt được cấp quyền (OPS_MANAGER, FINANCE_LEAD, DEMO_MANAGER)",
    )
    approver_id: str = Field(
        default="demo_manager_01",
        min_length=2,
        max_length=64,
        description="Định danh người phê duyệt trong phiên demo",
    )
    reason: str = Field(
        default="Đã đối chiếu bằng chứng xác thực và đồng ý phê duyệt đề xuất",
        min_length=3,
        max_length=512,
        description="Lý do phê duyệt hoặc từ chối",
    )
    expiry_minutes: int = Field(
        default=120,
        ge=1,
        le=1440,
        description="Thời hạn hiệu lực của chữ ký phê duyệt (phút)",
    )


class ApprovalResponse(BaseModel):
    approval_id: str
    proposal_id: str
    case_id: str | None
    action_type: str
    decision: str
    approver_role: str
    approver_id: str
    reason: str
    digest: str
    policy_version: str
    expiry: datetime
    proposal_status: str
    case_state: str | None = None
    case_version: int | None = None
    created_at: datetime


class ExecuteActionRequest(BaseModel):
    idempotency_key: str | None = Field(
        default=None,
        max_length=128,
        description="Khóa chống thực thi lặp (cũng có thể truyền qua header X-Idempotency-Key)",
    )
    simulate_timeout: bool = Field(
        default=False,
        description="Mô phỏng timeout khi thực thi side-effect (QA T04 -> UNKNOWN_OUTCOME)",
    )
    simulate_verification_failure: bool = Field(
        default=False,
        description="Mô phỏng tool trả về 200 OK nhưng hậu điều kiện DB không đạt (QA T20)",
    )


class VerificationResponse(BaseModel):
    id: str
    case_id: str | None
    action_execution_id: str | None
    case_version: int
    verdict: str
    postconditions_json: dict[str, Any]
    checked_at: datetime


class ExecuteActionResponse(BaseModel):
    execution_id: str
    proposal_id: str
    case_id: str | None
    action_type: str
    idempotency_key: str
    execution_state: str
    proposal_status: str
    case_state: str | None = None
    case_version: int | None = None
    idempotent_replay: bool = False
    result_json: dict[str, Any]
    verification: VerificationResponse | None = None
    executed_at: datetime


class ActionContractSummaryResponse(BaseModel):
    action_type: str
    policy_version: str
    required_evidence_sources: list[str]
    preconditions: list[str]
    required_approver_roles: list[str]
    default_risk_level: str
    allowed_source_modes: list[str]
    side_effects: list[str]
    expected_postconditions: list[str]


class ActionProposalDetailResponse(BaseModel):
    id: str
    case_id: str | None
    session_id: str
    action_type: str
    params_json: dict[str, Any]
    evidence_refs: list[str]
    expected_version: int
    risk_level: str
    status: str
    created_at: datetime
    contract: ActionContractSummaryResponse
    approval_id: str | None = None
    approval_decision: str | None = None
    approval_role: str | None = None
    approval_digest: str | None = None
    approval_expiry: datetime | None = None
    executions_count: int = 0
    latest_execution_state: str | None = None
