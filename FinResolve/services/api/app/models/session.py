"""Demo session model with TTL, case quota, and token-based isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import SessionStatus

if TYPE_CHECKING:
    from app.models.cases import Case


class DemoSession(Base):
    __tablename__ = "demo_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=SessionStatus.ACTIVE.value, nullable=False
    )
    max_cases: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    cases: Mapped[list[Case]] = relationship(
        "Case", back_populates="session", cascade="all, delete-orphan"
    )

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at

    @property
    def is_active(self) -> bool:
        return self.status == SessionStatus.ACTIVE.value and not self.is_expired
