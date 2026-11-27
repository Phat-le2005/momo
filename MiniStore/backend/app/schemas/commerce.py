"""Pydantic v2 request and response schemas for FinShop LIVE Commerce API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class VariantResponse(BaseModel):
    id: str
    product_id: str
    sku: str
    title: str
    price_vnd: int
    compare_at_price_vnd: int | None = None
    effective_price_vnd: int
    is_flash_sale: bool = False
    image_url: str
    on_hand_qty: int
    reserved_qty: int
    available_qty: int
    low_stock_threshold: int
    is_active: bool


class ProductResponse(BaseModel):
    id: str
    merchant_id: str
    slug: str
    title: str
    category: str
    description: str
    image_urls: list[str]
    is_featured: bool
    is_active: bool
    is_test_data: bool
    min_price_vnd: int
    max_price_vnd: int
    total_available_qty: int
    variants: list[VariantResponse]


class VoucherResponse(BaseModel):
    id: str
    code: str
    title: str
    discount_type: str
    discount_value: int
    max_discount_vnd: int | None = None
    min_order_vnd: int
    usage_limit: int
    used_count: int
    per_customer_limit: int
    starts_at: datetime
    ends_at: datetime
    is_active: bool


class CustomerIdentityResponse(BaseModel):
    id: str
    merchant_id: str
    commerce_session_id: str
    display_name: str
    phone: str
    email: str
    default_address: str
    session_token: str
    is_test_identity: bool


class CreateCustomerSessionRequest(BaseModel):
    display_name: str = Field(..., min_length=2, max_length=120)
    phone: str = Field(default="0900000111", max_length=32)
    default_address: str = Field(default="TP. Hồ Chí Minh", max_length=300)


class CartItemAddRequest(BaseModel):
    variant_id: str = Field(..., min_length=2, max_length=64)
    quantity: int = Field(default=1, ge=1, le=50)
    live_session_id: str | None = Field(default=None, max_length=64)


class CartItemUpdateRequest(BaseModel):
    quantity: int = Field(..., ge=1, le=50)


class ApplyVoucherRequest(BaseModel):
    voucher_code: str = Field(..., min_length=2, max_length=64)


class CartLineResponse(BaseModel):
    variant_id: str
    product_id: str
    product_title: str
    variant_title: str
    sku: str
    image_url: str
    quantity: int
    unit_price_vnd: int
    original_price_vnd: int
    is_flash_sale_price: bool
    line_total_vnd: int
    available_qty: int


class CartResponse(BaseModel):
    cart_id: str
    items: list[CartLineResponse]
    subtotal_vnd: int
    discount_vnd: int
    shipping_fee_vnd: int
    total_vnd: int
    applied_voucher_code: str | None = None
    voucher_title: str | None = None
    voucher_error: str | None = None


class DirectCheckoutItem(BaseModel):
    variant_id: str = Field(..., min_length=2, max_length=64)
    quantity: int = Field(default=1, ge=1, le=50)
    live_session_id: str | None = Field(default=None, max_length=64)


class CheckoutRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, max_length=128)
    recipient_name: str = Field(..., min_length=2, max_length=120)
    recipient_phone: str = Field(..., min_length=6, max_length=32)
    shipping_address: str = Field(..., min_length=5, max_length=300)
    customer_note: str = Field(default="", max_length=500)
    voucher_code: str | None = Field(default=None, max_length=64)
    payment_method: str = Field(default="LOCAL_MOCK", max_length=32)
    direct_items: list[DirectCheckoutItem] | None = None
    expected_total_vnd: int | None = Field(default=None, ge=0)


class OrderItemResponse(BaseModel):
    id: str
    product_id: str
    variant_id: str
    sku_snapshot: str
    product_title_snapshot: str
    variant_title_snapshot: str
    image_url_snapshot: str
    unit_price_vnd: int
    original_price_vnd: int
    is_flash_sale_price: bool
    quantity: int
    line_total_vnd: int


class PaymentResponse(BaseModel):
    id: str
    order_id: str
    merchant_id: str
    provider: str
    provider_environment: str = "LOCAL_MOCK"
    provider_ref: str
    provider_txn_no: str | None = None
    provider_txn_date: str | None = None
    provider_response_code: str | None = None
    provider_transaction_status: str | None = None
    observation_source: str = "INTERNAL_LEDGER"
    signature_verified: bool = False
    last_observed_at: datetime | None = None
    redirect_url: str | None = None
    correlation_id: str
    amount_vnd: int
    refunded_amount_vnd: int
    refundable_remaining_vnd: int
    status: str
    is_synced_to_order: bool
    reconciliation_required: bool = False
    reconciliation_note: str | None = None
    confirmed_at: datetime | None = None
    created_at: datetime


class PaymentQueryAttemptResponse(BaseModel):
    id: str
    payment_id: str
    order_id: str
    provider: str
    request_id: str
    command: str
    http_status: int | None = None
    provider_response_code: str | None = None
    provider_transaction_status: str | None = None
    signature_verified: bool
    mapped_payment_status: str | None = None
    observation_provenance: str
    error_message: str | None = None
    created_at: datetime


class ReconciliationRecordResponse(BaseModel):
    id: str
    order_id: str
    payment_id: str | None = None
    operator_id: str
    action_type: str
    note: str
    evidence_json: dict[str, Any]
    verified_postconditions: bool
    created_at: datetime


class OrderResponse(BaseModel):
    id: str
    order_code: str
    merchant_id: str
    commerce_session_id: str
    customer_id: str
    status: str
    payment_status: str
    reconciliation_status: str = "NONE"
    reconciliation_reason: str | None = None
    subtotal_vnd: int
    discount_vnd: int
    shipping_fee_vnd: int
    total_vnd: int
    voucher_code: str | None = None
    recipient_name: str
    recipient_phone: str
    shipping_address: str
    customer_note: str
    payment_method: str
    correlation_id: str
    version: int
    items: list[OrderItemResponse]
    payment: PaymentResponse | None = None
    created_at: datetime
    updated_at: datetime


class CheckoutResponse(BaseModel):
    order: OrderResponse
    payment: PaymentResponse
    idempotent_replay: bool


class CustomerMockPayRequest(BaseModel):
    outcome: str = Field(default="SUCCEEDED", max_length=32)
    auto_sync_order: bool = Field(default=True)


class MockPaymentNotifyRequest(BaseModel):
    provider_ref: str = Field(..., min_length=3, max_length=128)
    notification_id: str = Field(..., min_length=3, max_length=128)
    merchant_id: str = Field(..., min_length=2, max_length=64)
    order_id: str = Field(..., min_length=2, max_length=64)
    amount_vnd: int = Field(..., ge=0)
    outcome: str = Field(default="SUCCEEDED", max_length=32)
    auto_sync_order: bool = Field(default=True)


class CreateComplaintRequest(BaseModel):
    category: str = Field(default="PAYMENT_OR_ORDER", max_length=64)
    description: str = Field(..., min_length=5, max_length=2000)
    claimed_receipt_ref: str | None = Field(default=None, max_length=128)


class ComplaintResponse(BaseModel):
    id: str
    order_id: str
    customer_id: str
    category: str
    description: str
    claimed_receipt_ref: str | None = None
    evidence_kind: str
    status: str
    merchant_response: str
    created_at: datetime
    updated_at: datetime


class CreateRefundRequest(BaseModel):
    requested_amount_vnd: int = Field(..., gt=0)
    reason: str = Field(..., min_length=3, max_length=500)


class RefundRequestResponse(BaseModel):
    id: str
    order_id: str
    payment_id: str
    customer_id: str
    requested_amount_vnd: int
    reason: str
    status: str
    merchant_note: str
    created_at: datetime
    updated_at: datetime


class LiveProductItemResponse(BaseModel):
    id: str
    product_id: str
    variant_id: str
    product_title: str
    variant_title: str
    sku: str
    image_url: str
    original_price_vnd: int
    effective_price_vnd: int
    flash_sale_price_vnd: int | None = None
    flash_sale_stock_limit: int | None = None
    flash_sale_reserved_qty: int = 0
    flash_sale_sold_qty: int
    flash_sale_remaining: int | None = None
    flash_sale_starts_at: datetime | None = None
    flash_sale_ends_at: datetime | None = None
    available_qty: int
    is_pinned: bool
    sort_order: int


class LiveCommentCreateRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=300)


class LiveCommentResponse(BaseModel):
    id: str
    author_name: str
    message: str
    created_at: datetime


class LiveSessionResponse(BaseModel):
    id: str
    merchant_id: str
    merchant_name: str
    title: str
    host_name: str
    status: str
    is_simulated_stream: bool
    media_label_vi: str
    starts_at: datetime
    ends_at: datetime
    active_participants_count: int
    session_orders_count: int
    pinned_product: LiveProductItemResponse | None = None
    products: list[LiveProductItemResponse]
    recent_comments: list[LiveCommentResponse]


# Merchant Center request schemas
class MerchantLoginRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=64)
    password: str = Field(..., min_length=4, max_length=128)


class MerchantLoginResponse(BaseModel):
    session_id: str
    merchant_id: str
    commerce_session_id: str
    role: str
    operator_id: str
    merchant_token: str
    expires_at: datetime
    source_mode: str


class ReconciliationResolveRequest(BaseModel):
    resolution_note: str = Field(..., min_length=3, max_length=500)


class MerchantCreateProductRequest(BaseModel):
    title: str = Field(..., min_length=3, max_length=200)
    category: str = Field(..., min_length=2, max_length=64)
    description: str = Field(default="", max_length=2000)
    image_url: str = Field(default="/products/fash-tshirt.jpg", max_length=255)
    is_featured: bool = False
    sku: str = Field(..., min_length=3, max_length=64)
    variant_title: str = Field(default="Tiêu chuẩn", max_length=120)
    price_vnd: int = Field(..., ge=1000)
    compare_at_price_vnd: int | None = Field(default=None, ge=0)
    initial_stock: int = Field(default=10, ge=0, le=100000)


class MerchantUpdateProductRequest(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=200)
    category: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    is_featured: bool | None = None
    is_active: bool | None = None


class MerchantCreateVariantRequest(BaseModel):
    sku: str = Field(..., min_length=3, max_length=64)
    title: str = Field(..., min_length=1, max_length=120)
    price_vnd: int = Field(..., ge=1000)
    compare_at_price_vnd: int | None = Field(default=None, ge=0)
    initial_stock: int = Field(default=10, ge=0, le=100000)


class MerchantAdjustInventoryRequest(BaseModel):
    on_hand_qty: int = Field(..., ge=0, le=100000)
    low_stock_threshold: int | None = Field(default=None, ge=0, le=1000)


class MerchantUpdateOrderStatusRequest(BaseModel):
    status: str = Field(..., min_length=3, max_length=32)


class MerchantRefundDecisionRequest(BaseModel):
    decision: str = Field(..., pattern="^(APPROVE|REJECT)$")
    merchant_note: str = Field(default="", max_length=500)


class MerchantCreateVoucherRequest(BaseModel):
    code: str = Field(..., min_length=3, max_length=64)
    title: str = Field(..., min_length=3, max_length=160)
    discount_type: str = Field(default="FIXED_VND", pattern="^(FIXED_VND|PERCENT)$")
    discount_value: int = Field(..., gt=0)
    max_discount_vnd: int | None = Field(default=None, gt=0)
    min_order_vnd: int = Field(default=0, ge=0)
    usage_limit: int = Field(default=50, ge=1)
    per_customer_limit: int = Field(default=1, ge=1)
    duration_days: int = Field(default=14, ge=1, le=365)


class MerchantUpdateLiveProductRequest(BaseModel):
    is_pinned: bool | None = None
    flash_sale_price_vnd: int | None = Field(default=None, ge=1000)
    flash_sale_stock_limit: int | None = Field(default=None, ge=0)
    flash_sale_duration_minutes: int | None = Field(default=None, ge=1, le=1440)


class MerchantUpdateComplaintRequest(BaseModel):
    status: str = Field(..., pattern="^(OPEN|UNDER_REVIEW|RESOLVED|CLOSED)$")
    merchant_response: str = Field(default="", max_length=2000)


class CommerceEventItemResponse(BaseModel):
    id: str
    event_id: str
    event_type: str
    source: str
    merchant_id: str
    commerce_session_id: str
    world_epoch: int
    order_id: str | None = None
    payment_id: str | None = None
    correlation_id: str | None = None
    occurred_at: datetime
    payload_json: dict[str, Any]
    outbox_status: str | None = None
    failure_category: str | None = None
    finresolve_mapped_type: str | None = None
