"""Pydantic schemas for Autonomous Agent Supervisor and Challenge Lab (Prompt 09)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SupervisorStatusResponse(BaseModel):
    session_id: str
    status: str
    status_label_vi: str
    autopilot_enabled: bool
    world_epoch: int
    active_job_id: str | None = None
    active_case_id: str | None = None
    configured_model_mode: str
    configured_provider: str | None = None
    configured_model_name: str | None = None
    llm_execution_state: str
    llm_execution_label_vi: str
    last_llm_succeeded_at: datetime | None = None
    last_llm_failed_at: datetime | None = None
    last_llm_error: str | None = None
    llm_calls_count: int
    llm_tokens_used: int
    llm_budget_calls: int
    llm_budget_tokens: int
    queue_depth: int
    active_cases_count: int
    total_cases_count: int
    waiting_approvals_count: int
    jobs_processed_count: int
    monitoring_note_vi: str
    updated_at: datetime


class SupervisorActivityItemResponse(BaseModel):
    id: str
    seq: int
    session_id: str
    world_epoch: int
    challenge_id: str | None = None
    case_id: str | None = None
    job_id: str | None = None
    run_id: str | None = None
    kind: str
    title_vi: str
    detail_vi: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ChallengePresetItem(BaseModel):
    preset_id: str
    label_vi: str
    description_vi: str
    defaults: dict[str, Any] = Field(default_factory=dict)


class ChallengeCatalogItemResponse(BaseModel):
    challenge_type: str
    title_vi: str
    subtitle_vi: str
    is_advanced: bool = False
    presets: list[ChallengePresetItem] = Field(default_factory=list)


class LaunchChallengeRequest(BaseModel):
    challenge_type: str = Field(
        ...,
        description=(
            "SUSPICIOUS_RECEIPT | TRAFFIC_BURST | DUPLICATE_REFUND | "
            "BULK_ORDER_SYNC | PROMPT_INJECTION"
        ),
    )
    preset_id: str | None = Field(default=None, max_length=64)
    parameters: dict[str, Any] = Field(default_factory=dict)
    auto_process: bool = Field(
        default=True,
        description="Nếu Autopilot đang bật, tự động xử lý ngay tác vụ trong hàng đợi",
    )


class ExtractReceiptRequest(BaseModel):
    raw_content: str = Field(..., min_length=5, max_length=64000)
    filename: str = Field(default="mock_receipt.txt", max_length=120)


class ChallengeRunResponse(BaseModel):
    id: str
    session_id: str
    world_epoch: int
    challenge_type: str
    preset_id: str
    title_vi: str
    status: str
    status_label_vi: str
    input_json: dict[str, Any]
    generated_refs_json: dict[str, Any]
    linked_case_ids: list[str]
    linked_incident_ids: list[str]
    agent_run_ids: list[str]
    outcome_json: dict[str, Any]
    safety_verdict: str
    source_mode: str
    created_at: datetime
    updated_at: datetime


class ApprovalInboxItemResponse(BaseModel):
    proposal_id: str
    case_id: str
    case_goal: str
    case_state: str
    action_type: str
    action_label_vi: str
    summary_vi: str
    params_json: dict[str, Any]
    evidence_refs: list[str]
    risk_level: str
    expected_version: int
    status: str
    created_at: datetime


class MissionControlOverviewResponse(BaseModel):
    supervisor: SupervisorStatusResponse
    latest_challenge: ChallengeRunResponse | None = None
    recent_challenges: list[ChallengeRunResponse] = Field(default_factory=list)
    activities: list[SupervisorActivityItemResponse] = Field(default_factory=list)
    approval_inbox: list[ApprovalInboxItemResponse] = Field(default_factory=list)
    latest_seq: int = 0
