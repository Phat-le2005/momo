"""Action proposal, approval, execution, and verification models."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ActionExecutionState, ActionProposalStatus

if TYPE_CHECKING:
    from app.models.cases import Case


class ActionProposal(Base):
    __tablename__ = "action_proposals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("cases.id", ondelete="CASCADE"), nullable=True, index=True
    )
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False, default="LOW")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ActionProposalStatus.PROPOSED.value
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    case: Mapped[Case] = relationship("Case", back_populates="action_proposals")
    approval: Mapped[Approval | None] = relationship(
        "Approval", back_populates="proposal", uselist=False, cascade="all, delete-orphan"
    )
    executions: Mapped[list[ActionExecution]] = relationship(
        "ActionExecution", back_populates="proposal", cascade="all, delete-orphan"
    )


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("action_proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    approver_role: Mapped[str] = mapped_column(String(64), nullable=False)
    approver_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="demo_manager", server_default="demo_manager"
    )
    policy_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="FinResolve-Policy-v1.2",
        server_default="FinResolve-Policy-v1.2",
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    digest: Mapped[str] = mapped_column(String(128), nullable=False)
    expiry: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    proposal: Mapped[ActionProposal] = relationship("ActionProposal", back_populates="approval")


class ActionExecution(Base):
    __tablename__ = "action_executions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("action_proposals.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128), unique=True, index=True, nullable=False
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ActionExecutionState.PENDING.value
    )
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    proposal: Mapped[ActionProposal] = relationship("ActionProposal", back_populates="executions")


class Verification(Base):
    """Authoritative read-back verification record for Case and Action effects.

    Hard Invariant:
    A Verification record is bound to a specific case_version. Stale verification records
    cannot authorize transitioning a Case to RESOLVED if the Case version has changed.
    """

    __tablename__ = "verifications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("cases.id", ondelete="CASCADE"), nullable=True, index=True
    )
    action_execution_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("action_executions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    case_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    postconditions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    case: Mapped[Case] = relationship("Case", back_populates="verifications")
