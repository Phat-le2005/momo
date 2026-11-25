"""Continuous Commerce Monitoring models (Prompt 12).

Persists:
- `CommerceSourceBinding`: durable configuration, event cursor checkpoint, connectivity
  state, heartbeat, and PostgreSQL lease for multi-worker continuous monitoring.
- `AnomalyObservation`: durable deduplicated anomaly records (`D01`..`D08`), linked
  Cases/Jobs, lifecycle transitions, and independent recovery verification outcomes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CommerceSourceBinding(Base):
    """Session-scoped binding to an external MiniStore Commerce service read API."""

    __tablename__ = "commerce_source_bindings"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "merchant_id",
            "commerce_session_id",
            name="uq_commerce_source_binding_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    world_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    commerce_session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    commerce_world_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    connector_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default_ministore"
    )
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    world_reset_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    connectivity_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="UNCONFIGURED"
    )
    last_event_cursor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_event_occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    events_scan_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_orders_cursor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_payments_cursor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_scan_coverage_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_successful_scan_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    events_observed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class AnomalyObservation(Base):
    """Durable, deduplicated anomaly detected by the Continuous Commerce Monitor."""

    __tablename__ = "anomaly_observations"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "world_epoch",
            "fingerprint",
            name="uq_anomaly_observation_fingerprint",
        ),
        Index("ix_anomaly_obs_session_state", "session_id", "lifecycle_state"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    world_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_binding_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("commerce_source_bindings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    detector_code: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    anomaly_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="HIGH")
    lifecycle_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="OBSERVED", index=True
    )
    affected_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    affected_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    affected_entities_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    evidence_refs_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    linked_case_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    linked_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recovery_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verification_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verified_recovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verification_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
