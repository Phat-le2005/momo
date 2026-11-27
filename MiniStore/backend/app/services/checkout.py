"""Authoritative pricing, voucher validation, atomic inventory reservation, and idempotent checkout.

Financial & Inventory Invariants (Prompt 10.1):
- All money is integer VND. Frontend-submitted prices/discounts/totals are NEVER trusted.
- Flash Sale prices and stock limits are enforced strictly by backend timestamps and locked rows,
  separating `flash_sale_reserved_qty` (unpaid active holds) from `flash_sale_sold_qty` (confirmed).
- `Inventory` rows are locked in deterministic `variant_id` order (`SELECT ... FOR UPDATE`) to
  prevent deadlocks and eliminate overselling under concurrent checkouts.
- `Order` creation uses unique `(merchant_id, commerce_session_id, customer_id, idempotency_key)`
  paired with canonical `request_fingerprint`:
  - Identical retry (even after cart is cleared) returns the original order cleanly.
  - Reusing the same `idempotency_key` with different items/address/voucher returns `409 CONFLICT`.
- Inventory release validates reservation integrity strictly without silent clamping.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.errors import CommerceError, ErrorCode
from app.models.entities import (
    Cart,
    CommerceSession,
    Customer,
    Inventory,
    InventoryReservation,
    LiveProduct,
    LiveSession,
    Order,
    OrderItem,
    Payment,
    Product,
    ProductVariant,
    Voucher,
    VoucherRedemption,
)
from app.models.enums import (
    LiveSessionStatus,
    OrderStatus,
    PaymentStatus,
    ReconciliationStatus,
    ReservationStatus,
    VoucherDiscountType,
    can_transition_order,
)
from app.services.events import record_commerce_event

STANDARD_SHIPPING_FEE_VND = 25000
FREE_SHIPPING_THRESHOLD_VND = 300000


@dataclass(frozen=True)
class PricedLineItem:
    product: Product
    variant: ProductVariant
    quantity: int
    unit_price_vnd: int
    original_price_vnd: int
    is_flash_sale_price: bool
    live_product_id: str | None
    line_total_vnd: int


@dataclass(frozen=True)
class CartPricingSummary:
    lines: list[PricedLineItem]
    subtotal_vnd: int
    discount_vnd: int
    shipping_fee_vnd: int
    total_vnd: int
    voucher: Voucher | None
    voucher_error: str | None = None


def compute_checkout_fingerprint(
    *,
    items: list[tuple[str, int, str | None]],
    voucher_code: str | None,
    recipient_name: str,
    recipient_phone: str,
    shipping_address: str,
    payment_method: str,
) -> str:
    """Compute canonical SHA-256 fingerprint for checkout idempotency conflict detection."""
    normalized_items = sorted(
        [(vid.strip(), int(qty), (lsid.strip() if lsid else None)) for vid, qty, lsid in items],
        key=lambda x: (x[0], x[2] or ""),
    )
    payload = {
        "items": normalized_items,
        "voucher_code": (
            voucher_code.strip().upper() if voucher_code and voucher_code.strip() else None
        ),
        "recipient_name": recipient_name.strip(),
        "recipient_phone": recipient_phone.strip(),
        "shipping_address": shipping_address.strip(),
        "payment_method": payment_method.strip().upper(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_effective_unit_price(
    db: Session,
    *,
    variant: ProductVariant,
    live_session_id: str | None,
    quantity: int,
    now: datetime,
    lock_live_product: bool = False,
) -> tuple[int, bool, LiveProduct | None]:
    """Resolve authoritative unit price in integer VND, enforcing backend Flash Sale windows."""
    if not live_session_id:
        return variant.price_vnd, False, None

    live = db.get(LiveSession, live_session_id)
    if (
        live is None
        or live.status != LiveSessionStatus.LIVE.value
        or live.merchant_id != variant.merchant_id
    ):
        return variant.price_vnd, False, None

    stmt = select(LiveProduct).where(
        LiveProduct.live_session_id == live_session_id,
        LiveProduct.variant_id == variant.id,
    )
    if lock_live_product:
        stmt = stmt.with_for_update()
    lp = db.scalar(stmt)
    if lp is None or lp.flash_sale_price_vnd is None:
        return variant.price_vnd, False, lp

    if lp.flash_sale_starts_at and now < lp.flash_sale_starts_at:
        return variant.price_vnd, False, lp
    if lp.flash_sale_ends_at and now > lp.flash_sale_ends_at:
        return variant.price_vnd, False, lp

    if lp.flash_sale_stock_limit is not None:
        used_fs = lp.flash_sale_reserved_qty + lp.flash_sale_sold_qty
        remaining_fs = lp.flash_sale_stock_limit - used_fs
        if remaining_fs < quantity:
            if lock_live_product:
                raise CommerceError(
                    ErrorCode.OUT_OF_STOCK,
                    f"Flash Sale cho '{variant.title}' chỉ còn {max(0, remaining_fs)} suất",
                    status_code=409,
                    details={
                        "variant_id": variant.id,
                        "flash_sale_remaining": max(0, remaining_fs),
                    },
                )
            return variant.price_vnd, False, lp

    return lp.flash_sale_price_vnd, True, lp


def evaluate_voucher(
    db: Session,
    *,
    merchant_id: str,
    customer_id: str,
    voucher_code: str,
    subtotal_vnd: int,
    now: datetime,
    lock_voucher: bool = False,
) -> tuple[Voucher, int]:
    """Validate voucher eligibility, usage limits, window, and compute integer VND discount."""
    norm_code = voucher_code.strip().upper()
    stmt = select(Voucher).where(
        Voucher.merchant_id == merchant_id,
        Voucher.code == norm_code,
    )
    if lock_voucher:
        stmt = stmt.with_for_update()
    v = db.scalar(stmt)
    if v is None or not v.is_active:
        raise CommerceError(
            ErrorCode.VOUCHER_INVALID,
            f"Mã giảm giá '{norm_code}' không tồn tại hoặc đã ngừng áp dụng",
            status_code=400,
        )
    if now < v.starts_at or now > v.ends_at:
        raise CommerceError(
            ErrorCode.VOUCHER_INVALID,
            f"Mã giảm giá '{norm_code}' chưa bắt đầu hoặc đã hết hạn",
            status_code=400,
        )
    if subtotal_vnd < v.min_order_vnd:
        raise CommerceError(
            ErrorCode.VOUCHER_INVALID,
            f"Đơn hàng cần tối thiểu {v.min_order_vnd:,}đ để dùng mã '{norm_code}'",
            status_code=400,
        )
    if v.used_count >= v.usage_limit:
        raise CommerceError(
            ErrorCode.VOUCHER_INVALID,
            f"Mã giảm giá '{norm_code}' đã hết lượt sử dụng",
            status_code=409,
        )
    cust_used = (
        db.scalar(
            select(func.count(VoucherRedemption.id)).where(
                VoucherRedemption.voucher_id == v.id,
                VoucherRedemption.customer_id == customer_id,
                VoucherRedemption.status == "APPLIED",
            )
        )
        or 0
    )
    if cust_used >= v.per_customer_limit:
        raise CommerceError(
            ErrorCode.VOUCHER_INVALID,
            f"Bạn đã dùng hết lượt cho phép của mã '{norm_code}'",
            status_code=409,
        )

    if v.discount_type == VoucherDiscountType.FIXED_VND.value:
        discount = min(subtotal_vnd, v.discount_value)
    else:
        discount = (subtotal_vnd * v.discount_value) // 100
        if v.max_discount_vnd is not None:
            discount = min(discount, v.max_discount_vnd)
        discount = min(subtotal_vnd, discount)
    return v, int(discount)


def compute_shipping_fee_vnd(subtotal_vnd: int) -> int:
    if subtotal_vnd <= 0:
        return 0
    return 0 if subtotal_vnd >= FREE_SHIPPING_THRESHOLD_VND else STANDARD_SHIPPING_FEE_VND


def price_cart(
    db: Session,
    *,
    cart: Cart,
    customer_id: str,
    strict_voucher: bool = False,
) -> CartPricingSummary:
    """Compute authoritative backend pricing summary for a customer's Cart."""
    now = datetime.now(UTC)
    lines: list[PricedLineItem] = []
    subtotal = 0

    for item in cart.items:
        variant = db.get(ProductVariant, item.variant_id)
        if variant is None or not variant.is_active:
            continue
        product = db.get(Product, variant.product_id)
        if product is None or not product.is_active:
            continue
        unit_price, is_fs, lp = resolve_effective_unit_price(
            db,
            variant=variant,
            live_session_id=item.live_session_id,
            quantity=item.quantity,
            now=now,
            lock_live_product=False,
        )
        line_total = unit_price * item.quantity
        subtotal += line_total
        lines.append(
            PricedLineItem(
                product=product,
                variant=variant,
                quantity=item.quantity,
                unit_price_vnd=unit_price,
                original_price_vnd=variant.price_vnd,
                is_flash_sale_price=is_fs,
                live_product_id=lp.id if lp else None,
                line_total_vnd=line_total,
            )
        )

    voucher: Voucher | None = None
    discount = 0
    voucher_err: str | None = None
    if cart.applied_voucher_code and subtotal > 0:
        try:
            voucher, discount = evaluate_voucher(
                db,
                merchant_id=cart.merchant_id,
                customer_id=customer_id,
                voucher_code=cart.applied_voucher_code,
                subtotal_vnd=subtotal,
                now=now,
                lock_voucher=False,
            )
        except CommerceError as exc:
            if strict_voucher:
                raise
            voucher_err = exc.message

    shipping = compute_shipping_fee_vnd(subtotal)
    total = max(0, subtotal - discount + shipping)
    return CartPricingSummary(
        lines=lines,
        subtotal_vnd=subtotal,
        discount_vnd=discount,
        shipping_fee_vnd=shipping,
        total_vnd=total,
        voucher=voucher,
        voucher_error=voucher_err,
    )


def _merge_raw_items(
    raw_items: list[tuple[str, int, str | None]],
) -> dict[str, tuple[int, str | None]]:
    merged: dict[str, tuple[int, str | None]] = {}
    for vid, qty, lsid in raw_items:
        if qty <= 0 or qty > 50:
            raise CommerceError(
                ErrorCode.BAD_REQUEST, "Số lượng sản phẩm không hợp lệ (1–50)", status_code=400
            )
        prev_qty, prev_ls = merged.get(vid, (0, lsid))
        merged[vid] = (prev_qty + qty, prev_ls or lsid)
    return merged


def _validate_idempotent_replay(
    *,
    existing_order: Order,
    raw_items: list[tuple[str, int, str | None]],
    voucher_code: str | None,
    recipient_name: str,
    recipient_phone: str,
    shipping_address: str,
    payment_method: str,
) -> tuple[Order, Payment, bool]:
    """Verify replay fingerprint when caller supplies items, or allow empty-cart retry."""
    if raw_items:
        merged = _merge_raw_items(raw_items)
        req_fp = compute_checkout_fingerprint(
            items=[(vid, qty, lsid) for vid, (qty, lsid) in merged.items()],
            voucher_code=voucher_code,
            recipient_name=recipient_name,
            recipient_phone=recipient_phone,
            shipping_address=shipping_address,
            payment_method=payment_method,
        )
        if existing_order.request_fingerprint and existing_order.request_fingerprint != req_fp:
            raise CommerceError(
                ErrorCode.CONFLICT,
                (
                    "CHECKOUT_IDEMPOTENCY_CONFLICT: Khóa idempotency_key đã được dùng cho "
                    "một đơn hàng khác với nội dung khác."
                ),
                status_code=409,
            )
    else:
        # Cart was already cleared by the first attempt; still validate non-item fields match
        if (
            existing_order.recipient_name != recipient_name.strip()
            or existing_order.recipient_phone != recipient_phone.strip()
            or existing_order.shipping_address != shipping_address.strip()
            or (existing_order.voucher_code or None)
            != (voucher_code.strip().upper() if voucher_code and voucher_code.strip() else None)
        ):
            raise CommerceError(
                ErrorCode.CONFLICT,
                (
                    "CHECKOUT_IDEMPOTENCY_CONFLICT: Khóa idempotency_key đã được dùng cho "
                    "một đơn hàng khác với thông tin nhận hàng/voucher khác."
                ),
                status_code=409,
            )

    existing_pay = existing_order.payments[0]
    return existing_order, existing_pay, True


def create_order_from_items(
    db: Session,
    *,
    customer: Customer,
    session_obj: CommerceSession,
    idempotency_key: str,
    recipient_name: str,
    recipient_phone: str,
    shipping_address: str,
    customer_note: str = "",
    voucher_code: str | None = None,
    payment_method: str = "LOCAL_MOCK",
    direct_items: list[tuple[str, int, str | None]] | None = None,
    clear_cart: Cart | None = None,
    expected_total_vnd: int | None = None,
    client_ip: str = "127.0.0.1",
) -> tuple[Order, Payment, bool]:
    """Atomically reserve inventory, redeem voucher, create Order + Payment + Outbox events."""
    clean_key = idempotency_key.strip()
    if not clean_key or len(clean_key) > 128:
        raise CommerceError(
            ErrorCode.BAD_REQUEST,
            "A valid idempotency_key is required for checkout",
            status_code=400,
        )
    norm_pm = payment_method.strip().upper()
    if norm_pm not in {"LOCAL_MOCK", "VNPAY_SANDBOX"}:
        raise CommerceError(
            ErrorCode.BAD_REQUEST,
            (
                f"Phương thức thanh toán '{payment_method}' không được hỗ trợ. "
                "Hỗ trợ: LOCAL_MOCK, VNPAY_SANDBOX."
            ),
            status_code=400,
        )
    if norm_pm == "VNPAY_SANDBOX" and not get_settings().is_vnpay_configured:
        raise CommerceError(
            ErrorCode.BAD_REQUEST,
            (
                "VNPAY_NOT_CONFIGURED: Cổng VNPAY Sandbox chưa được cấu hình khóa merchant "
                "nên tạm thời chưa khả dụng."
            ),
            status_code=503,
        )

    raw_items: list[tuple[str, int, str | None]] = []
    if direct_items is not None:
        raw_items = list(direct_items)
    elif clear_cart is not None:
        raw_items = [(ci.variant_id, ci.quantity, ci.live_session_id) for ci in clear_cart.items]

    # 1. Check idempotency key BEFORE rejecting empty cart (supports retry after cart cleared)
    existing_order = db.scalar(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.payments))
        .where(
            Order.merchant_id == customer.merchant_id,
            Order.commerce_session_id == session_obj.id,
            Order.customer_id == customer.id,
            Order.idempotency_key == clean_key,
        )
    )
    if existing_order is not None:
        return _validate_idempotent_replay(
            existing_order=existing_order,
            raw_items=raw_items,
            voucher_code=voucher_code,
            recipient_name=recipient_name,
            recipient_phone=recipient_phone,
            shipping_address=shipping_address,
            payment_method=norm_pm,
        )

    if not raw_items:
        raise CommerceError(
            ErrorCode.BAD_REQUEST, "Giỏ hàng trống, không thể tạo đơn", status_code=400
        )

    merged = _merge_raw_items(raw_items)
    request_fingerprint = compute_checkout_fingerprint(
        items=[(vid, qty, lsid) for vid, (qty, lsid) in merged.items()],
        voucher_code=voucher_code,
        recipient_name=recipient_name,
        recipient_phone=recipient_phone,
        shipping_address=shipping_address,
        payment_method=norm_pm,
    )

    sorted_vids = sorted(merged.keys())
    now = datetime.now(UTC)
    ttl_s = get_settings().reservation_ttl_seconds
    expires_at = now + timedelta(seconds=ttl_s)
    corr_id = f"CORR-{uuid.uuid4().hex[:12].upper()}"
    order_id = f"ORD-{uuid.uuid4().hex[:10].upper()}"
    order_code = f"FS{now.strftime('%y%m%d')}{uuid.uuid4().hex[:6].upper()}"

    priced_lines: list[PricedLineItem] = []
    locked_inventories: list[tuple[Inventory, int, str | None]] = []
    locked_live_products: list[tuple[LiveProduct, int]] = []
    subtotal = 0
    primary_live_id: str | None = None

    # Canonical lock order: Inventory (sorted by variant_id) -> LiveProduct -> Voucher
    for vid in sorted_vids:
        qty, lsid = merged[vid]
        if lsid and primary_live_id is None:
            primary_live_id = lsid

        variant = db.scalar(
            select(ProductVariant).where(
                ProductVariant.id == vid,
                ProductVariant.merchant_id == customer.merchant_id,
                ProductVariant.is_active.is_(True),
            )
        )
        if variant is None:
            raise CommerceError(
                ErrorCode.NOT_FOUND, f"Không tìm thấy phân loại sản phẩm '{vid}'", status_code=404
            )
        product = db.get(Product, variant.product_id)
        if product is None or not product.is_active:
            raise CommerceError(
                ErrorCode.NOT_FOUND, "Sản phẩm đã ngừng kinh doanh", status_code=404
            )

        inv = db.scalar(
            select(Inventory)
            .where(Inventory.variant_id == vid, Inventory.merchant_id == customer.merchant_id)
            .with_for_update()
        )
        if inv is None or inv.available_qty < qty:
            avail = inv.available_qty if inv is not None else 0
            raise CommerceError(
                ErrorCode.OUT_OF_STOCK,
                f"Sản phẩm '{product.title} ({variant.title})' chỉ còn {avail} sản phẩm khả dụng",
                status_code=409,
                details={"variant_id": vid, "available_qty": avail, "requested_qty": qty},
            )

        unit_price, is_fs, lp = resolve_effective_unit_price(
            db,
            variant=variant,
            live_session_id=lsid,
            quantity=qty,
            now=now,
            lock_live_product=True,
        )
        line_total = unit_price * qty
        subtotal += line_total
        lp_id = lp.id if (is_fs and lp is not None) else None
        priced_lines.append(
            PricedLineItem(
                product=product,
                variant=variant,
                quantity=qty,
                unit_price_vnd=unit_price,
                original_price_vnd=variant.price_vnd,
                is_flash_sale_price=is_fs,
                live_product_id=lp_id,
                line_total_vnd=line_total,
            )
        )
        locked_inventories.append((inv, qty, lp_id))
        if is_fs and lp is not None:
            locked_live_products.append((lp, qty))

    voucher: Voucher | None = None
    discount = 0
    if voucher_code and voucher_code.strip():
        voucher, discount = evaluate_voucher(
            db,
            merchant_id=customer.merchant_id,
            customer_id=customer.id,
            voucher_code=voucher_code,
            subtotal_vnd=subtotal,
            now=now,
            lock_voucher=True,
        )

    shipping_fee = compute_shipping_fee_vnd(subtotal)
    total_vnd = max(0, subtotal - discount + shipping_fee)

    if expected_total_vnd is not None and expected_total_vnd != total_vnd:
        raise CommerceError(
            ErrorCode.BAD_REQUEST,
            (
                f"PRICE_MANIPULATION_REJECTED: Tổng tiền hiển thị ({expected_total_vnd:,}đ) "
                f"không khớp giá xác thực từ hệ thống ({total_vnd:,}đ)."
            ),
            status_code=400,
            details={
                "expected_total_vnd": expected_total_vnd,
                "authoritative_total_vnd": total_vnd,
            },
        )

    order = Order(
        id=order_id,
        order_code=order_code,
        merchant_id=customer.merchant_id,
        commerce_session_id=session_obj.id,
        world_epoch=session_obj.world_epoch,
        customer_id=customer.id,
        live_session_id=primary_live_id,
        idempotency_key=clean_key,
        request_fingerprint=request_fingerprint,
        correlation_id=corr_id,
        status=OrderStatus.PENDING_PAYMENT.value,
        payment_status=PaymentStatus.INITIATED.value,
        reconciliation_status=ReconciliationStatus.NONE.value,
        reconciliation_reason=None,
        subtotal_vnd=subtotal,
        discount_vnd=discount,
        shipping_fee_vnd=shipping_fee,
        total_vnd=total_vnd,
        voucher_code=voucher.code if voucher else None,
        recipient_name=recipient_name.strip(),
        recipient_phone=recipient_phone.strip(),
        shipping_address=shipping_address.strip(),
        customer_note=customer_note.strip()[:500],
        payment_method=norm_pm,
        version=1,
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(order)
            db.flush()
    except IntegrityError as err:
        err_text = str(err.orig) if err.orig is not None else str(err)
        if "uq_orders_idempotency" in err_text:
            dup = db.scalar(
                select(Order)
                .options(selectinload(Order.items), selectinload(Order.payments))
                .where(
                    Order.merchant_id == customer.merchant_id,
                    Order.commerce_session_id == session_obj.id,
                    Order.customer_id == customer.id,
                    Order.idempotency_key == clean_key,
                )
            )
            if dup is not None:
                return _validate_idempotent_replay(
                    existing_order=dup,
                    raw_items=raw_items,
                    voucher_code=voucher_code,
                    recipient_name=recipient_name,
                    recipient_phone=recipient_phone,
                    shipping_address=shipping_address,
                    payment_method=norm_pm,
                )
        raise

    for line in priced_lines:
        db.add(
            OrderItem(
                id=f"OI-{uuid.uuid4().hex[:10].upper()}",
                order_id=order.id,
                product_id=line.product.id,
                variant_id=line.variant.id,
                sku_snapshot=line.variant.sku,
                product_title_snapshot=line.product.title,
                variant_title_snapshot=line.variant.title,
                image_url_snapshot=line.variant.image_url,
                unit_price_vnd=line.unit_price_vnd,
                original_price_vnd=line.original_price_vnd,
                is_flash_sale_price=line.is_flash_sale_price,
                quantity=line.quantity,
                line_total_vnd=line.line_total_vnd,
            )
        )

    for inv, qty, lp_id in locked_inventories:
        inv.reserved_qty += qty
        inv.version += 1
        inv.updated_at = now
        db.add(
            InventoryReservation(
                id=f"RES-{uuid.uuid4().hex[:10].upper()}",
                merchant_id=customer.merchant_id,
                commerce_session_id=session_obj.id,
                order_id=order.id,
                variant_id=inv.variant_id,
                live_product_id=lp_id,
                quantity=qty,
                status=ReservationStatus.ACTIVE.value,
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
        )
        record_commerce_event(
            db,
            merchant_id=customer.merchant_id,
            commerce_session_id=session_obj.id,
            world_epoch=session_obj.world_epoch,
            event_type="INVENTORY_RESERVED",
            order_id=order.id,
            correlation_id=corr_id,
            payload={
                "variant_id": inv.variant_id,
                "quantity": qty,
                "available_after": inv.available_qty,
                "expires_at": expires_at.isoformat(),
            },
        )

    # Reserve promotional Flash Sale quota (moved to flash_sale_sold_qty on payment sync!)
    for lp, qty in locked_live_products:
        lp.flash_sale_reserved_qty += qty

    if voucher is not None:
        voucher.used_count += 1
        db.add(
            VoucherRedemption(
                id=f"VR-{uuid.uuid4().hex[:10].upper()}",
                voucher_id=voucher.id,
                merchant_id=customer.merchant_id,
                customer_id=customer.id,
                order_id=order.id,
                discount_vnd=discount,
                status="APPLIED",
                created_at=now,
            )
        )

    from app.payments.registry import get_payment_adapter

    adapter = get_payment_adapter(norm_pm)
    payment = adapter.create_payment(
        db,
        order=order,
        correlation_id=corr_id,
        client_ip=client_ip,
    )

    record_commerce_event(
        db,
        merchant_id=customer.merchant_id,
        commerce_session_id=session_obj.id,
        world_epoch=session_obj.world_epoch,
        event_type="ORDER_CREATED",
        order_id=order.id,
        payment_id=payment.id,
        correlation_id=corr_id,
        payload={
            "order_code": order.order_code,
            "total_vnd": order.total_vnd,
            "subtotal_vnd": order.subtotal_vnd,
            "discount_vnd": order.discount_vnd,
            "shipping_fee_vnd": order.shipping_fee_vnd,
            "voucher_code": order.voucher_code,
            "item_count": len(priced_lines),
        },
    )

    if clear_cart is not None:
        for ci in list(clear_cart.items):
            db.delete(ci)
        clear_cart.applied_voucher_code = None
        clear_cart.updated_at = now

    db.commit()
    db.refresh(order)
    db.refresh(payment)
    return order, payment, False


def release_order_reservations_and_voucher(
    db: Session,
    *,
    order: Order,
    reason_status: ReservationStatus = ReservationStatus.RELEASED,
) -> int:
    """Release ACTIVE inventory reservations, Flash Sale reservations, and voucher quota.

    Validates reservation integrity strictly without silent clamping.
    """
    now = datetime.now(UTC)
    reservations = list(
        db.scalars(
            select(InventoryReservation)
            .where(
                InventoryReservation.order_id == order.id,
                InventoryReservation.status == ReservationStatus.ACTIVE.value,
            )
            .order_by(InventoryReservation.variant_id.asc())
            .with_for_update()
        ).all()
    )
    released = 0
    for res in reservations:
        inv = db.scalar(
            select(Inventory).where(Inventory.variant_id == res.variant_id).with_for_update()
        )
        if inv is None or inv.reserved_qty < res.quantity:
            order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
            order.reconciliation_reason = (
                f"Inventory reservation integrity mismatch on release for variant "
                f"{res.variant_id}: reserved_qty={inv.reserved_qty if inv else 'MISSING'} "
                f"< reservation.quantity={res.quantity}"
            )
            record_commerce_event(
                db,
                merchant_id=order.merchant_id,
                commerce_session_id=order.commerce_session_id,
                world_epoch=order.world_epoch,
                event_type="INVENTORY_INCONSISTENCY_DETECTED",
                order_id=order.id,
                correlation_id=order.correlation_id,
                payload={
                    "variant_id": res.variant_id,
                    "reservation_id": res.id,
                    "expected_reserved_qty": res.quantity,
                    "actual_reserved_qty": inv.reserved_qty if inv else None,
                    "operation": "RELEASE",
                },
            )
        else:
            inv.reserved_qty -= res.quantity
            inv.version += 1
            inv.updated_at = now

        if res.live_product_id:
            lp = db.scalar(
                select(LiveProduct).where(LiveProduct.id == res.live_product_id).with_for_update()
            )
            if lp is not None and lp.flash_sale_reserved_qty >= res.quantity:
                lp.flash_sale_reserved_qty -= res.quantity

        res.status = reason_status.value
        res.updated_at = now
        released += 1
        record_commerce_event(
            db,
            merchant_id=order.merchant_id,
            commerce_session_id=order.commerce_session_id,
            world_epoch=order.world_epoch,
            event_type="INVENTORY_RELEASED",
            order_id=order.id,
            correlation_id=order.correlation_id,
            payload={
                "variant_id": res.variant_id,
                "quantity": res.quantity,
                "reason": reason_status.value,
            },
        )

    redemption = db.scalar(
        select(VoucherRedemption)
        .where(VoucherRedemption.order_id == order.id, VoucherRedemption.status == "APPLIED")
        .with_for_update()
    )
    if redemption is not None:
        v = db.scalar(select(Voucher).where(Voucher.id == redemption.voucher_id).with_for_update())
        if v is not None and v.used_count > 0:
            v.used_count -= 1
        redemption.status = "RELEASED"

    return released


def cancel_order(
    db: Session,
    *,
    order: Order,
    actor: str,
    reason: str = "Cancelled by customer",
) -> Order:
    """Validate state transition, cancel unpaid order, release reservations, and record event."""
    curr = OrderStatus(order.status)
    if not can_transition_order(curr, OrderStatus.CANCELLED):
        raise CommerceError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Không thể hủy đơn hàng ở trạng thái '{order.status}'",
            status_code=409,
        )
    if order.payment_status in (
        PaymentStatus.SUCCEEDED.value,
        PaymentStatus.PARTIALLY_REFUNDED.value,
        PaymentStatus.REFUNDED.value,
    ):
        raise CommerceError(
            ErrorCode.CONFLICT,
            "Đơn đã thanh toán thành công — vui lòng gửi yêu cầu hoàn tiền thay vì hủy trực tiếp",
            status_code=409,
        )
    if order.payment_status == PaymentStatus.UNKNOWN.value:
        raise CommerceError(
            ErrorCode.CONFLICT,
            (
                "Trạng thái thanh toán đang UNKNOWN (chưa xác định) — cần kiểm tra đối soát "
                "trước khi hủy đơn"
            ),
            status_code=409,
        )

    now = datetime.now(UTC)
    order.status = OrderStatus.CANCELLED.value
    order.version += 1
    order.updated_at = now
    release_order_reservations_and_voucher(
        db, order=order, reason_status=ReservationStatus.RELEASED
    )

    record_commerce_event(
        db,
        merchant_id=order.merchant_id,
        commerce_session_id=order.commerce_session_id,
        world_epoch=order.world_epoch,
        event_type="ORDER_UPDATED",
        order_id=order.id,
        correlation_id=order.correlation_id,
        payload={
            "previous_status": curr.value,
            "new_status": OrderStatus.CANCELLED.value,
            "actor": actor,
            "reason": reason,
        },
    )
    db.commit()
    db.refresh(order)
    return order
