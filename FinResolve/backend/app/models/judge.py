"""Isolated Judge-Only Blind Challenge Run persistence model (Prompt 13 Part B/D).

Ground truth (`ground_truth_json`, `expected_constraints_json`) is strictly isolated
to Judge & Oracle components and NEVER exposed to FinResolve Agent tools, normal
Mission Control APIs, or Commerce read endpoints.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BlindChallengeRun(Base):
    """Durable lifecycle record for an isolated Blind Challenge Lab run."""

    __tablename__ = "blind_challenge_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    world_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    commerce_session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    challenge_family: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    seed: Mapped[int] = mapped_column(Integer, nullable=False, default=42)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="CREATED", index=True
    )  # CREATED, ARMED, RUNNING, COMPLETED, EVALUATED, ABORTED, RESET
    injection_config_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    affected_entities_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    ground_truth_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    expected_constraints_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    evaluation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
