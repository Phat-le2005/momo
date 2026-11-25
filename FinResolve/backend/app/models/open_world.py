"""Open-World Autonomous Business Guardian Models (Prompt 14).

Persists:
- `RecoveryCommand`: durable recovery command protocol (`PROPOSED -> AUTHORIZED -> EXECUTING ->
  VERIFYING -> VERIFIED / REJECTED / FAILED / UNKNOWN_OUTCOME / MANUAL_REVIEW`).
- `BusinessObservation`: canonical open-world observation store (business, operational,
  security/risk, unstructured, or unknown categories).
- `BehavioralBaseline`: persistent merchant/service/metric statistical baselines with warm-up
  and contamination freeze protection.
- `IncidentExperience`: merchant-scoped verified incident memory for safe knowledge reuse.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RecoveryCommand(Base):
    """Durable, idempotent, policy-gated recovery command lifecycle."""

    __tablename__ = "recovery_commands"
    __table_args__ = (
        UniqueConstraint("session_id", "idempotency_key", name="uq_recovery_cmd_session_idemp"),
        Index("ix_recovery_cmd_case_state", "case_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    command: Mapped[str] = mapped_column(String(64), nullable=False)
    case_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    anomaly_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_binding_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("commerce_source_bindings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    commerce_session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    world_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    target_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    expected_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_decision: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="PROPOSED", index=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
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


class BusinessObservation(Base):
    """Canonical open-world observation record across business, operational, security & text."""

    __tablename__ = "business_observations"
    __table_args__ = (Index("ix_bus_obs_scope_cat", "session_id", "world_epoch", "category"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    world_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    commerce_session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )  # BUSINESS, OPERATIONAL, SECURITY_RISK, UNSTRUCTURED, UNKNOWN
    observation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="OBSERVED_FACT"
    )  # OBSERVED_FACT, CLAIM, DERIVED_STAT, HYPOTHESIS
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    entity_refs_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    attributes_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    verification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="UNVERIFIED"
    )
    freshness_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )


class BehavioralBaseline(Base):
    """Persistent adaptive statistical baseline per merchant/service/metric."""

    __tablename__ = "behavioral_baselines"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "merchant_id",
            "service_name",
            "metric_name",
            "regime",
            name="uq_behavioral_baseline_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    service_name: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False)
    regime: Mapped[str] = mapped_column(String(32), nullable=False, default="NORMAL")
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_samples: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    ewma: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    median: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    mad: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    recent_values_json: Mapped[list[float]] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_frozen: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class IncidentExperience(Base):
    """Verified incident experience memory scoped strictly per merchant."""

    __tablename__ = "incident_experiences"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    case_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    symptom_summary: Mapped[str] = mapped_column(Text, nullable=False)
    symptom_tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    validated_root_cause: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    actions_attempted_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    verified_recovery_steps_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0")
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
