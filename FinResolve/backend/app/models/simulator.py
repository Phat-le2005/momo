"""Simulator models for Orders, Payments, Refunds, Products, and Inventory.

Hard Domain Invariants:
- All monetary amounts are BIGINT minor units for VND (amount >= 0).
- Distinct payment, order, and refund state lifecycles.
- Explicit currency (default VND).
- Concurrency & version fields for optimistic locking and SELECT FOR UPDATE.
- Source_mode MOCK always visible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    pass


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_products_price_nonnegative"),
        CheckConstraint("currency IN ('VND', 'USD')", name="ck_products_currency"),
        Index("ix_products_session_sku", "session_id", "sku", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="VND")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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


class Warehouse(Base):
    __tablename__ = "warehouses"
    __table_args__ = (Index("ix_warehouses_session_code", "session_id", "code", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
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


class InventoryLevel(Base):
    __tablename__ = "inventory_levels"
    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="ck_inventory_levels_on_hand_nonnegative"),
        CheckConstraint("reserved >= 0", name="ck_inventory_levels_reserved_nonnegative"),
        CheckConstraint("on_hand >= reserved", name="ck_inventory_levels_available"),
        Index(
            "ix_inventory_levels_product_warehouse",
            "session_id",
            "product_id",
            "warehouse_id",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    warehouse_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("warehouses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="MOCK")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


class SimOrder(Base):
    __tablename__ = "sim_orders"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_sim_orders_amount_nonnegative"),
        CheckConstraint("subtotal >= 0", name="ck_sim_orders_subtotal_nonnegative"),
        CheckConstraint("discount >= 0", name="ck_sim_orders_discount_nonnegative"),
        CheckConstraint("tax >= 0", name="ck_sim_orders_tax_nonnegative"),
        CheckConstraint("shipping >= 0", name="ck_sim_orders_shipping_nonnegative"),
        CheckConstraint("grand_total >= 0", name="ck_sim_orders_grand_total_nonnegative"),
        CheckConstraint("currency IN ('VND', 'USD')", name="ck_sim_orders_currency"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_name: Mapped[str] = mapped_column(String(128), nullable=False)
    customer_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="VND")
    subtotal: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    discount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tax: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    shipping: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    grand_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    payment_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNPAID")
    fulfillment_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="UNFULFILLED"
    )
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

    items: Mapped[list[OrderItem]] = relationship(
        "OrderItem", back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price >= 0", name="ck_order_items_unit_price_nonnegative"),
        CheckConstraint("discount >= 0", name="ck_order_items_discount_nonnegative"),
        CheckConstraint("tax >= 0", name="ck_order_items_tax_nonnegative"),
        CheckConstraint("line_total >= 0", name="ck_order_items_line_total_nonnegative"),
        CheckConstraint("currency IN ('VND', 'USD')", name="ck_order_items_currency"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sim_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    sku_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    product_name_snapshot: Mapped[str] = mapped_column(String(256), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tax: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    line_total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="VND")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    order: Mapped[SimOrder] = relationship("SimOrder", back_populates="items")


class InventoryReservation(Base):
    __tablename__ = "inventory_reservations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_inventory_reservations_quantity_positive"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("sim_orders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    order_item_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("order_items.id", ondelete="CASCADE"), nullable=True, index=True
    )
    product_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    warehouse_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("warehouses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="MOCK")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"
    __table_args__ = (CheckConstraint("delta != 0", name="ck_inventory_movements_delta_nonzero"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    warehouse_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("warehouses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    source_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="MOCK")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class SimPayment(Base):
    __tablename__ = "sim_payments"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_sim_payments_amount_nonnegative"),
        CheckConstraint("currency IN ('VND', 'USD')", name="ck_sim_payments_currency"),
        Index(
            "ix_sim_payments_session_provider_ref",
            "session_id",
            "provider",
            "provider_ref",
            unique=True,
            postgresql_where="provider_ref IS NOT NULL",
        ),
        Index(
            "ix_sim_payments_session_provider_request",
            "session_id",
            "provider",
            "request_id",
            unique=True,
            postgresql_where="request_id IS NOT NULL",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    merchant_id: Mapped[str] = mapped_column(String(64), nullable=False, default="MERCHANT_MOCK_01")
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_ref: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    provider_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    provider_transaction_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    result_code: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="VND")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    source_event_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verification_source: Mapped[str] = mapped_column(
        String(64), nullable=False, default="PROVIDER_QUERY"
    )
    source_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="MOCK")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    refunds: Mapped[list[SimRefund]] = relationship(
        "SimRefund", back_populates="payment", cascade="all, delete-orphan"
    )


class SimRefund(Base):
    __tablename__ = "sim_refunds"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_sim_refunds_amount_nonnegative"),
        CheckConstraint(
            "requested_amount >= 0", name="ck_sim_refunds_requested_amount_nonnegative"
        ),
        CheckConstraint("approved_amount >= 0", name="ck_sim_refunds_approved_amount_nonnegative"),
        CheckConstraint(
            "completed_amount >= 0", name="ck_sim_refunds_completed_amount_nonnegative"
        ),
        CheckConstraint("currency IN ('VND', 'USD')", name="ck_sim_refunds_currency"),
        Index(
            "ix_sim_refunds_session_request",
            "session_id",
            "request_id",
            unique=True,
            postgresql_where="request_id IS NOT NULL",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payment_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sim_payments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    requested_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approved_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    completed_amount: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="VND")
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(256), nullable=False)
    approval_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_refund_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
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

    payment: Mapped[SimPayment] = relationship("SimPayment", back_populates="refunds")


class SimPaymentEvent(Base):
    __tablename__ = "sim_payment_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("demo_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    payment_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("sim_payments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="MOCK")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
