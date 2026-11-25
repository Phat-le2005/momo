"""All 21 persistent Commerce domain entities (+ LiveComment) for FinShop LIVE.

Invariants:
- All monetary values are stored as integer VND (`*_vnd`). Never float.
- `inventories` enforces `on_hand_qty >= 0`, `reserved_qty >= 0`, `reserved_qty <= on_hand_qty`.
- `order_items` persists immutable price & product snapshots at checkout time.
- `orders` enforces unique `(merchant_id, commerce_session_id, idempotency_key)` for checkout.
- `commerce_events` & `event_outbox` are written atomically in the same transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.models.enums import (
    ComplaintStatus,
    LiveSessionStatus,
    OrderStatus,
    OutboxStatus,
    PaymentStatus,
    ReconciliationStatus,
    RefundRequestStatus,
    RefundTransactionStatus,
    ReservationStatus,
    VoucherDiscountType,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    avatar_url: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_test_data: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class CommerceSession(Base):
    __tablename__ = "commerce_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    world_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    source_mode: Mapped[str] = mapped_column(String(32), default="MOCK", nullable=False)
    finresolve_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (Index("ix_customers_session_merchant", "commerce_session_id", "merchant_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    commerce_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("commerce_sessions.id", ondelete="CASCADE"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    email: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    default_address: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    session_token: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    is_test_identity: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class MerchantAccount(Base):
    __tablename__ = "merchant_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="MERCHANT_OWNER", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class MerchantSession(Base):
    __tablename__ = "merchant_sessions"
    __table_args__ = (Index("ix_merchant_sessions_merchant_active", "merchant_id", "revoked_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_account_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("merchant_accounts.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    commerce_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("commerce_sessions.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="MERCHANT_OWNER", nullable=False)
    operator_id: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class ServiceCredential(Base):
    __tablename__ = "service_credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    service_name: Mapped[str] = mapped_column(String(64), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    commerce_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("commerce_sessions.id", ondelete="CASCADE"), nullable=False
    )
    allowed_scopes_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (Index("ix_products_merchant_category", "merchant_id", "category"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    image_urls_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_test_data: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    variants: Mapped[list[ProductVariant]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductVariant.id"
    )


class ProductVariant(Base):
    __tablename__ = "product_variants"
    __table_args__ = (
        CheckConstraint("price_vnd >= 0", name="ck_product_variants_price_nonneg"),
        CheckConstraint(
            "compare_at_price_vnd IS NULL OR compare_at_price_vnd >= 0",
            name="ck_product_variants_compare_nonneg",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("products.id", ondelete="CASCADE"), index=True, nullable=False
    )
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    price_vnd: Mapped[int] = mapped_column(Integer, nullable=False)
    compare_at_price_vnd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_url: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    product: Mapped[Product] = relationship(back_populates="variants")
    inventory: Mapped[Inventory | None] = relationship(
        back_populates="variant", uselist=False, cascade="all, delete-orphan"
    )


class Inventory(Base):
    __tablename__ = "inventories"
    __table_args__ = (
        CheckConstraint("on_hand_qty >= 0", name="ck_inventories_on_hand_nonneg"),
        CheckConstraint("reserved_qty >= 0", name="ck_inventories_reserved_nonneg"),
        CheckConstraint("reserved_qty <= on_hand_qty", name="ck_inventories_reserved_le_on_hand"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    variant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("product_variants.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    on_hand_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    variant: Mapped[ProductVariant] = relationship(back_populates="inventory")

    @property
    def available_qty(self) -> int:
        return max(0, self.on_hand_qty - self.reserved_qty)


class InventoryReservation(Base):
    __tablename__ = "inventory_reservations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_inventory_reservations_qty_pos"),
        Index("ix_inv_res_status_expires", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    commerce_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("commerce_sessions.id", ondelete="CASCADE"), nullable=False
    )
    order_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    variant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("product_variants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    live_product_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("live_products.id", ondelete="SET NULL"),
        nullable=True,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=ReservationStatus.ACTIVE.value, index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class Cart(Base):
    __tablename__ = "carts"
    __table_args__ = (
        UniqueConstraint("customer_id", "commerce_session_id", name="uq_carts_customer_session"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    commerce_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("commerce_sessions.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    applied_voucher_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    items: Mapped[list[CartItem]] = relationship(
        back_populates="cart", cascade="all, delete-orphan", order_by="CartItem.created_at"
    )


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (
        UniqueConstraint("cart_id", "variant_id", name="uq_cart_items_cart_variant"),
        CheckConstraint("quantity > 0", name="ck_cart_items_qty_pos"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cart_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("carts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    variant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False
    )
    live_session_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("live_sessions.id", ondelete="SET NULL"), nullable=True
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    cart: Mapped[Cart] = relationship(back_populates="items")


class Voucher(Base):
    __tablename__ = "vouchers"
    __table_args__ = (
        UniqueConstraint("merchant_id", "code", name="uq_vouchers_merchant_code"),
        CheckConstraint("discount_value > 0", name="ck_vouchers_discount_pos"),
        CheckConstraint("min_order_vnd >= 0", name="ck_vouchers_min_order_nonneg"),
        CheckConstraint("usage_limit > 0", name="ck_vouchers_usage_limit_pos"),
        CheckConstraint("used_count >= 0", name="ck_vouchers_used_count_nonneg"),
        CheckConstraint("used_count <= usage_limit", name="ck_vouchers_used_le_limit"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    discount_type: Mapped[str] = mapped_column(
        String(24), default=VoucherDiscountType.FIXED_VND.value, nullable=False
    )
    discount_value: Mapped[int] = mapped_column(Integer, nullable=False)
    max_discount_vnd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    min_order_vnd: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usage_limit: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    per_customer_limit: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "commerce_session_id",
            "customer_id",
            "idempotency_key",
            name="uq_orders_idempotency",
        ),
        CheckConstraint("subtotal_vnd >= 0", name="ck_orders_subtotal_nonneg"),
        CheckConstraint("discount_vnd >= 0", name="ck_orders_discount_nonneg"),
        CheckConstraint("shipping_fee_vnd >= 0", name="ck_orders_shipping_nonneg"),
        CheckConstraint("total_vnd >= 0", name="ck_orders_total_nonneg"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    commerce_session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("commerce_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    world_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    live_session_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("live_sessions.id", ondelete="SET NULL"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)

    status: Mapped[str] = mapped_column(
        String(32), default=OrderStatus.PENDING_PAYMENT.value, index=True, nullable=False
    )
    payment_status: Mapped[str] = mapped_column(
        String(32), default=PaymentStatus.INITIATED.value, index=True, nullable=False
    )
    reconciliation_status: Mapped[str] = mapped_column(
        String(32), default=ReconciliationStatus.NONE.value, index=True, nullable=False
    )
    reconciliation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    subtotal_vnd: Mapped[int] = mapped_column(Integer, nullable=False)
    discount_vnd: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    shipping_fee_vnd: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_vnd: Mapped[int] = mapped_column(Integer, nullable=False)
    voucher_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    recipient_name: Mapped[str] = mapped_column(String(120), nullable=False)
    recipient_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    shipping_address: Mapped[str] = mapped_column(String(300), nullable=False)
    customer_note: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    payment_method: Mapped[str] = mapped_column(String(32), default="LOCAL_MOCK", nullable=False)

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderItem.id"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="Payment.created_at"
    )


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_qty_pos"),
        CheckConstraint("unit_price_vnd >= 0", name="ck_order_items_price_nonneg"),
        CheckConstraint("line_total_vnd >= 0", name="ck_order_items_line_total_nonneg"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    variant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sku_snapshot: Mapped[str] = mapped_column(String(64), nullable=False)
    product_title_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    variant_title_snapshot: Mapped[str] = mapped_column(String(120), nullable=False)
    image_url_snapshot: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    unit_price_vnd: Mapped[int] = mapped_column(Integer, nullable=False)
    original_price_vnd: Mapped[int] = mapped_column(Integer, nullable=False)
    is_flash_sale_price: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total_vnd: Mapped[int] = mapped_column(Integer, nullable=False)

    order: Mapped[Order] = relationship(back_populates="items")


class VoucherRedemption(Base):
    __tablename__ = "voucher_redemptions"
    __table_args__ = (
        UniqueConstraint("voucher_id", "order_id", name="uq_voucher_redemptions_voucher_order"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    voucher_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("vouchers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    order_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    discount_vnd: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="APPLIED", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_vnd >= 0", name="ck_payments_amount_nonneg"),
        CheckConstraint("refunded_amount_vnd >= 0", name="ck_payments_refunded_nonneg"),
        CheckConstraint("refunded_amount_vnd <= amount_vnd", name="ck_payments_refunded_le_amount"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    commerce_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("commerce_sessions.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), default="LOCAL_MOCK", nullable=False)
    provider_ref: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    amount_vnd: Mapped[int] = mapped_column(Integer, nullable=False)
    refunded_amount_vnd: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=PaymentStatus.INITIATED.value, index=True, nullable=False
    )
    is_synced_to_order: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True, nullable=False
    )
    reconciliation_required: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True, nullable=False
    )
    reconciliation_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_notification_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    order: Mapped[Order] = relationship(back_populates="payments")


class ProviderNotification(Base):
    __tablename__ = "provider_notifications"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "notification_id",
            name="uq_provider_notifications_provider_nid",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    notification_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payment_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("payments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    order_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    merchant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class RefundRequest(Base):
    __tablename__ = "refund_requests"
    __table_args__ = (CheckConstraint("requested_amount_vnd > 0", name="ck_refund_req_amt_pos"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    payment_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("payments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    commerce_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("commerce_sessions.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    requested_amount_vnd: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=RefundRequestStatus.REQUESTED.value, index=True, nullable=False
    )
    merchant_note: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class RefundTransaction(Base):
    __tablename__ = "refund_transactions"
    __table_args__ = (
        CheckConstraint("amount_vnd > 0", name="ck_refund_txn_amt_pos"),
        UniqueConstraint("payment_id", "idempotency_key", name="uq_refund_txn_idemp"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    refund_request_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("refund_requests.id", ondelete="CASCADE"), index=True, nullable=False
    )
    payment_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("payments.id", ondelete="CASCADE"), index=True, nullable=False
    )
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), default="LOCAL_MOCK", nullable=False)
    provider_refund_ref: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    amount_vnd: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=RefundTransactionStatus.PENDING.value, index=True, nullable=False
    )
    provider_payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class LiveSession(Base):
    __tablename__ = "live_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    commerce_session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("commerce_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    host_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=LiveSessionStatus.LIVE.value, index=True, nullable=False
    )
    is_simulated_stream: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    media_label_vi: Mapped[str] = mapped_column(
        String(255),
        default=(
            "Phiên LIVE mô phỏng (Simulated Live Commerce) — Không phải livestream trực tiếp thật"
        ),
        nullable=False,
    )
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    live_products: Mapped[list[LiveProduct]] = relationship(
        back_populates="live_session",
        cascade="all, delete-orphan",
        order_by="LiveProduct.sort_order",
    )


class LiveProduct(Base):
    __tablename__ = "live_products"
    __table_args__ = (
        UniqueConstraint("live_session_id", "variant_id", name="uq_live_products_session_variant"),
        CheckConstraint(
            "flash_sale_price_vnd IS NULL OR flash_sale_price_vnd >= 0",
            name="ck_live_products_flash_price_nonneg",
        ),
        CheckConstraint(
            "flash_sale_stock_limit IS NULL OR flash_sale_stock_limit >= 0",
            name="ck_live_products_flash_limit_nonneg",
        ),
        CheckConstraint(
            "flash_sale_reserved_qty >= 0",
            name="ck_live_products_flash_reserved_nonneg",
        ),
        CheckConstraint("flash_sale_sold_qty >= 0", name="ck_live_products_flash_sold_nonneg"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    live_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("live_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    product_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    variant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("product_variants.id", ondelete="CASCADE"), nullable=False
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    flash_sale_price_vnd: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flash_sale_stock_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flash_sale_reserved_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    flash_sale_sold_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    flash_sale_starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    flash_sale_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    live_session: Mapped[LiveSession] = relationship(back_populates="live_products")


class LiveComment(Base):
    __tablename__ = "live_comments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    live_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("live_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    author_name: Mapped[str] = mapped_column(String(120), nullable=False)
    message: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Complaint(Base):
    __tablename__ = "complaints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False
    )
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    commerce_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("commerce_sessions.id", ondelete="CASCADE"), nullable=False
    )
    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customers.id", ondelete="CASCADE"), index=True, nullable=False
    )
    category: Mapped[str] = mapped_column(String(64), default="PAYMENT_OR_ORDER", nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    claimed_receipt_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_kind: Mapped[str] = mapped_column(String(24), default="CLAIM", nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=ComplaintStatus.OPEN.value, index=True, nullable=False
    )
    merchant_response: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class CommerceEvent(Base):
    __tablename__ = "commerce_events"
    __table_args__ = (
        Index("ix_commerce_events_merchant_created", "merchant_id", "occurred_at"),
        Index("ix_commerce_events_order", "order_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="MINI_STORE_COMMERCE", nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), default="1.0", nullable=False)
    source_mode: Mapped[str] = mapped_column(String(32), default="MOCK", nullable=False)
    merchant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("merchants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    commerce_session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("commerce_sessions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    finresolve_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    world_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class EventOutbox(Base):
    __tablename__ = "event_outbox"
    __table_args__ = (Index("ix_event_outbox_status_next", "status", "next_attempt_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    commerce_event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("commerce_events.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    finresolve_mapped_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), default=OutboxStatus.PENDING.value, index=True, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_finresolve_world_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
