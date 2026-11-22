"""Risk signal model."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import RiskResponse

if TYPE_CHECKING:
    from app.models.cases import Case


class RiskSignal(Base):
    __tablename__ = "risk_signals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("cases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    response: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RiskResponse.OBSERVE.value
    )
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    case: Mapped[Case] = relationship("Case", back_populates="risk_signals")
