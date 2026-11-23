"""SimScenarioRun ORM model for Prompt 06 Mini Mega Live Simulator."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SimScenarioRun(Base):
    """Tracks seeded scenario runs (payment_sync, refund_risk, mega_live), async progress,
    performance telemetry, and internal test-only Oracle truth (never exposed to AI tools)."""

    __tablename__ = "sim_scenario_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scenario_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    seed: Mapped[int] = mapped_column(Integer, nullable=False, default=20261122)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING", index=True)
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_generated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    case_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    incident_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # Internal test-only Oracle truth; NEVER queried by ReadToolRegistry or AI Core!
    oracle_truth_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="MOCK")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
