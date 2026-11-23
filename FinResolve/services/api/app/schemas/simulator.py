"""Pydantic schemas for Prompt 06 Mini Mega Live Simulator & Scenario APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ScenarioCatalogItemResponse(BaseModel):
    scenario_id: str
    control_label: str
    title_vi: str
    description_vi: str
    default_order_count: int
    min_order_count: int
    max_order_count: int
    source_mode: str = "MOCK"


class RunScenarioRequest(BaseModel):
    seed: int = Field(default=20261122, ge=1, le=2_147_483_647)
    order_count: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Number of synthetic orders for mega_live (50-100; default 60)",
    )
    run_async: bool = Field(
        default=False,
        description="Run batch scenario asynchronously in background and poll /status",
    )
    reset_before_run: bool = Field(
        default=False,
        description="Reset current DemoSession synthetic world before running scenario",
    )


class ScenarioRunResponse(BaseModel):
    run_id: str
    session_id: str
    scenario_id: str
    seed: int
    status: str
    progress_pct: int
    orders_generated: int
    case_ids: list[str]
    incident_ids: list[str]
    source_mode: str = "MOCK"
    metrics: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ScenarioOracleReportResponse(BaseModel):
    run_id: str
    session_id: str
    scenario_id: str
    seed: int
    source_mode: str = "MOCK"
    status: str
    verdict: str
    unsafe_effects_count: int
    verified_resolved_count: int
    pending_unverified_count: int
    mismatched_count: int
    blocked_risk_count: int
    correlated_incident_count: int
    false_merges_count: int
    stock_oversell_detected: bool
    duplicate_ipn_deduplicated: bool
    oracle_leak_detected: bool
    performance_metrics: dict[str, Any] = Field(default_factory=dict)
    unsafe_effect_details: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)


class SessionResetResponse(BaseModel):
    session_id: str
    source_mode: str = "MOCK"
    reset_completed: bool = True
    deleted_counts: dict[str, int]
