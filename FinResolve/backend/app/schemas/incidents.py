"""Pydantic schemas for Prompt 05 Adaptive Incident Taskforce APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RoleCapabilityResponse(BaseModel):
    role: str
    title_vi: str
    responsibility_vi: str
    allowed_read_tools: list[str]
    can_propose_actions: bool = False
    can_execute_writes: bool = False


class IncidentCaseItemResponse(BaseModel):
    id: str
    incident_id: str
    case_id: str
    case_state: str
    case_version: int
    link_reason: str
    shared_evidence_refs: list[str]
    causal_factors_json: dict[str, Any]
    is_auto_resolved: bool = False
    created_at: datetime


class AgentTaskItemResponse(BaseModel):
    id: str
    incident_id: str | None
    case_id: str | None
    role: str
    objective: str
    state: str
    allowed_tools: list[str]
    evidence_refs: list[str]
    handoff_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class IncidentSummaryResponse(BaseModel):
    id: str
    session_id: str
    title: str
    hypothesis: str
    status: str
    correlation_key: str
    correlation_trace: str
    linked_cases_count: int
    assigned_tasks_count: int
    confidence: float
    shared_factors_json: dict[str, Any]
    evidence_refs: list[str]
    created_at: datetime
    updated_at: datetime


class IncidentDetailResponse(BaseModel):
    id: str
    session_id: str
    source_mode: str = "MOCK"
    model_mode: str = "DEMO_RULES"
    configured_model_mode: str = "DEMO_RULES"
    taskforce_execution_mode: str = "DETERMINISTIC_TASKFORCE_RULES"
    llm_invoked: bool = False
    title: str
    hypothesis: str
    status: str
    correlation_key: str
    correlation_trace: str
    confidence: float
    shared_factors_json: dict[str, Any]
    evidence_refs: list[str]
    linked_cases: list[IncidentCaseItemResponse]
    assigned_tasks: list[AgentTaskItemResponse]
    rejected_lookalikes: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class IncidentListResponse(BaseModel):
    session_id: str
    source_mode: str = "MOCK"
    model_mode: str = "DEMO_RULES"
    configured_model_mode: str = "DEMO_RULES"
    taskforce_execution_mode: str = "DETERMINISTIC_TASKFORCE_RULES"
    llm_invoked: bool = False
    total_count: int
    items: list[IncidentSummaryResponse]
    capabilities_catalog: list[RoleCapabilityResponse]


class SeedIncidentScenarioRequest(BaseModel):
    force_new: bool = Field(
        default=False,
        description="If false (default), replays the existing seeded incident scenario in session",
    )


class SeededIncidentScenarioResponse(BaseModel):
    session_id: str
    source_mode: str = "MOCK"
    model_mode: str = "DEMO_RULES"
    configured_model_mode: str = "DEMO_RULES"
    taskforce_execution_mode: str = "DETERMINISTIC_TASKFORCE_RULES"
    llm_invoked: bool = False
    idempotent_replay: bool = False
    incident_id: str
    correlation_trace: str
    correlated_case_ids: list[str]
    unrelated_lookalike_case_ids: list[str]
    assigned_task_ids: list[str]
