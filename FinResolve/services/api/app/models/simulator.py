"""Simulator models for Orders, Payments, and Refunds (separate lifecycles)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SimOrder(Base):
    __tablename__ = "sim_orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_name: Mapped[str] = mapped_column(String(128), nullable=False)
    customer_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
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


class SimPayment(Base):
    __tablename__ = "sim_payments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_ref: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="MOCK")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class SimRefund(Base):
    __tablename__ = "sim_refunds"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payment_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(256), nullable=False)
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
