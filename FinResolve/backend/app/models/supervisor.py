"""Persistent models for Supervisor, Worker Heartbeats, and Challenge Lab (Prompt 09.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import (
    ChallengeStatus,
    LlmExecutionState,
    SupervisorJobStatus,
    SupervisorStatus,
)


class SupervisorWorker(Base):
    """Registered background Supervisor worker process with live heartbeat health tracking."""

    __tablename__ = "supervisor_workers"

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    worker_mode: Mapped[str] = mapped_column(String(32), default="STANDALONE", nullable=False)
    hostname: Mapped[str] = mapped_column(String(128), default="localhost", nullable=False)
    pid: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="IDLE", nullable=False)
    active_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True, nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class SupervisorState(Base):
    """Session-scoped Autonomous Agent Supervisor state and truthful LLM execution telemetry."""

    __tablename__ = "supervisor_states"

    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("demo_sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        default=SupervisorStatus.MONITORING.value,
        nullable=False,
    )
    autopilot_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    world_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    activity_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_case_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_execution_state: Mapped[str] = mapped_column(
        String(32),
        default=LlmExecutionState.DEMO_RULES_FALLBACK.value,
        nullable=False,
    )
    last_llm_succeeded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_llm_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_llm_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    llm_calls_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_attempts_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_succeeded_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_timeout_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_fallback_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_tokens_unknown_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_tokens_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_budget_calls: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    llm_budget_tokens: Mapped[int] = mapped_column(Integer, default=40000, nullable=False)
    jobs_processed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class SupervisorJob(Base):
    """Durable PostgreSQL work queue item with fencing token, lease renewal, backoff, and dedupe."""

    __tablename__ = "supervisor_jobs"
    __table_args__ = (
        Index("ix_supervisor_jobs_session_status", "session_id", "status"),
        Index("ix_supervisor_jobs_dedupe", "session_id", "dedupe_key"),
        Index(
            "uq_supervisor_jobs_active_dedupe",
            "session_id",
            "world_epoch",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING', 'CANCELLING')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("demo_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    world_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    job_type: Mapped[str] = mapped_column(String(48), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    target_case_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    challenge_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        default=SupervisorJobStatus.QUEUED.value,
        index=True,
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class ChallengeRun(Base):
    """Persisted Adversarial Challenge Lab execution record and evidence-grounded outcome."""

    __tablename__ = "challenges"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("demo_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    world_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    challenge_type: Mapped[str] = mapped_column(String(48), index=True, nullable=False)
    preset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title_vi: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        default=ChallengeStatus.QUEUED.value,
        index=True,
        nullable=False,
    )
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    generated_refs_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    linked_case_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    linked_incident_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    agent_run_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    outcome_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    safety_verdict: Mapped[str] = mapped_column(String(64), default="PENDING", nullable=False)
    source_mode: Mapped[str] = mapped_column(String(32), default="MOCK", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class SupervisorActivity(Base):
    """Persisted, ordered, human-readable Agent activity event for Mission Control feed."""

    __tablename__ = "supervisor_activities"
    __table_args__ = (
        Index("ix_supervisor_activities_session_seq", "session_id", "seq", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("demo_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    world_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    challenge_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    case_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    title_vi: Mapped[str] = mapped_column(String(255), nullable=False)
    detail_vi: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
