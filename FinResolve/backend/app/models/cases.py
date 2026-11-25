"""Case, Evidence, and CaseEvent models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import CaseState, EvidenceKind

if TYPE_CHECKING:
    from app.models.actions import ActionProposal, Verification
    from app.models.risk import RiskSignal
    from app.models.session import DemoSession


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        Index(
            "ix_cases_session_idempotency",
            "session_id",
            "idempotency_key",
            unique=True,
            postgresql_where="idempotency_key IS NOT NULL",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False, default="PAYMENT_DISPUTE")
    goal: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CaseState.NEW.value, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    owner: Mapped[str] = mapped_column(String(64), nullable=False, default="guest")
    source_system: Mapped[str] = mapped_column(
        String(32), nullable=False, default="SIMULATOR", index=True
    )
    source_binding_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    idempotency_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    session: Mapped[DemoSession] = relationship("DemoSession", back_populates="cases")
    evidence: Mapped[list[Evidence]] = relationship(
        "Evidence",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="Evidence.observed_at",
    )
    events: Mapped[list[CaseEvent]] = relationship(
        "CaseEvent",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="CaseEvent.created_at",
    )
    risk_signals: Mapped[list[RiskSignal]] = relationship(
        "RiskSignal",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="RiskSignal.created_at",
    )
    action_proposals: Mapped[list[ActionProposal]] = relationship(
        "ActionProposal",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="ActionProposal.created_at",
    )
    verifications: Mapped[list[Verification]] = relationship(
        "Verification",
        back_populates="case",
        cascade="all, delete-orphan",
        order_by="Verification.checked_at",
    )


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default=EvidenceKind.CLAIM.value)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    case: Mapped[Case] = relationship("Case", back_populates="evidence")


class CaseEvent(Base):
    """Append-only audit trail for Case state and lifecycle changes."""

    __tablename__ = "case_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    case: Mapped[Case] = relationship("Case", back_populates="events")
