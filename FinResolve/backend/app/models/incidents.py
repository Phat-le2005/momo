"""Incident, IncidentCase, and AgentTask models for Prompt 05 Adaptive Incident Taskforce."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import AgentTaskState, IncidentStatus

if TYPE_CHECKING:
    from app.models.cases import Case


class Incident(Base):
    """Candidate group of Cases correlated by shared causal OBSERVED_FACT evidence."""

    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    hypothesis: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=IncidentStatus.OPEN.value, index=True
    )
    correlation_key: Mapped[str] = mapped_column(String(192), nullable=False, index=True)
    correlation_trace: Mapped[str] = mapped_column(String(512), nullable=False)
    shared_factors_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.85)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    incident_cases: Mapped[list[IncidentCase]] = relationship(
        "IncidentCase",
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="IncidentCase.created_at",
    )
    tasks: Mapped[list[AgentTask]] = relationship(
        "AgentTask",
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="AgentTask.created_at",
    )


class IncidentCase(Base):
    """Per-case association to an Incident. Never overrides or auto-resolves child Case state."""

    __tablename__ = "incident_cases"
    __table_args__ = (
        Index("ix_incident_cases_incident_case", "incident_id", "case_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    link_reason: Mapped[str] = mapped_column(String(512), nullable=False)
    shared_evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    causal_factors_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    is_auto_resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    incident: Mapped[Incident] = relationship("Incident", back_populates="incident_cases")
    case: Mapped[Case] = relationship("Case")


class AgentTask(Base):
    """Bounded logical role task (Finance, Operations, Risk, Support) with typed JSON handoff."""

    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    incident_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    case_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("cases.id", ondelete="CASCADE"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    objective: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AgentTaskState.ASSIGNED.value, index=True
    )
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    handoff_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    incident: Mapped[Incident | None] = relationship("Incident", back_populates="tasks")
    case: Mapped[Case | None] = relationship("Case")
