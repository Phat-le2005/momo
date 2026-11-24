"""Authoritative pricing, voucher validation, atomic inventory reservation, and idempotent checkout.

Financial & Inventory Invariants:
- All money is integer VND. Frontend-submitted prices/discounts/totals are NEVER trusted.
- Flash Sale prices and stock limits are enforced strictly by backend timestamps and locked rows.
- `Inventory` rows are locked in deterministic `variant_id` order (`SELECT ... FOR UPDATE`) to
  prevent deadlocks and eliminate overselling under concurrent checkouts.
- `Order` creation uses unique `(merchant_id, commerce_session_id, customer_id, idempotency_key)`
  so repeated clicks or network retries return the existing order cleanly.
- Every order creation, reservation, and cancellation persists `CommerceEvent` + `EventOutbox`
  atomically in the same DB transaction.
"""

from __future__ import annotations

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
    if live is None or live.status != LiveSessionStatus.LIVE.value:
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
        remaining_fs = lp.flash_sale_stock_limit - lp.flash_sale_sold_qty
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
        # Integer arithmetic only (no float!)
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
) -> tuple[Order, Payment, bool]:
    """Atomically reserve inventory, redeem voucher, create Order + Payment + Outbox events.

    Returns `(order, payment, idempotent_replay)`.
    If `expected_total_vnd` is supplied by caller and differs from backend-calculated total,
    rejects with 400 (`PRICE_MANIPULATION_REJECTED`) so manipulated frontend totals never pass.
    """
    clean_key = idempotency_key.strip()
    if not clean_key or len(clean_key) > 128:
        raise CommerceError(
            ErrorCode.BAD_REQUEST,
            "A valid idempotency_key is required for checkout",
            status_code=400,
        )
    norm_pm = payment_method.strip().upper()
    if norm_pm != "LOCAL_MOCK":
        raise CommerceError(
            ErrorCode.BAD_REQUEST,
            (
                "Chỉ hỗ trợ phương thức thanh toán LOCAL_MOCK ở phiên bản này "
                "(VNPAY Sandbox sẽ ra mắt ở Prompt 11)."
            ),
            status_code=400,
        )

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
        existing_pay = existing_order.payments[0]
        return existing_order, existing_pay, True

    raw_items: list[tuple[str, int, str | None]] = []
    if direct_items is not None:
        raw_items = list(direct_items)
    elif clear_cart is not None:
        raw_items = [(ci.variant_id, ci.quantity, ci.live_session_id) for ci in clear_cart.items]

    if not raw_items:
        raise CommerceError(
            ErrorCode.BAD_REQUEST, "Giỏ hàng trống, không thể tạo đơn", status_code=400
        )

    # Deduplicate & sort by variant_id for deterministic lock ordering (zero deadlocks)
    merged: dict[str, tuple[int, str | None]] = {}
    for vid, qty, lsid in raw_items:
        if qty <= 0 or qty > 50:
            raise CommerceError(
                ErrorCode.BAD_REQUEST, "Số lượng sản phẩm không hợp lệ (1–50)", status_code=400
            )
        prev_qty, prev_ls = merged.get(vid, (0, lsid))
        merged[vid] = (prev_qty + qty, prev_ls or lsid)

    sorted_vids = sorted(merged.keys())
    now = datetime.now(UTC)
    ttl_s = get_settings().reservation_ttl_seconds
    expires_at = now + timedelta(seconds=ttl_s)
    corr_id = f"CORR-{uuid.uuid4().hex[:12].upper()}"
    order_id = f"ORD-{uuid.uuid4().hex[:10].upper()}"
    order_code = f"FS{now.strftime('%y%m%d')}{uuid.uuid4().hex[:6].upper()}"

    priced_lines: list[PricedLineItem] = []
    locked_inventories: list[tuple[Inventory, int]] = []
    locked_live_products: list[tuple[LiveProduct, int]] = []
    subtotal = 0
    primary_live_id: str | None = None

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
        priced_lines.append(
            PricedLineItem(
                product=product,
                variant=variant,
                quantity=qty,
                unit_price_vnd=unit_price,
                original_price_vnd=variant.price_vnd,
                is_flash_sale_price=is_fs,
                live_product_id=lp.id if lp else None,
                line_total_vnd=line_total,
            )
        )
        locked_inventories.append((inv, qty))
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
        correlation_id=corr_id,
        status=OrderStatus.PENDING_PAYMENT.value,
        payment_status=PaymentStatus.INITIATED.value,
        subtotal_vnd=subtotal,
        discount_vnd=discount,
        shipping_fee_vnd=shipping_fee,
        total_vnd=total_vnd,
        voucher_code=voucher.code if voucher else None,
        recipient_name=recipient_name.strip(),
        recipient_phone=recipient_phone.strip(),
        shipping_address=shipping_address.strip(),
        customer_note=customer_note.strip()[:500],
        payment_method="LOCAL_MOCK",
        version=1,
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(order)
            db.flush()
    except IntegrityError:
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
            return dup, dup.payments[0], True
        raise

    # Persist immutable order-item snapshots and reserve inventory atomically
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

    for inv, qty in locked_inventories:
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

    for lp, qty in locked_live_products:
        lp.flash_sale_sold_qty += qty

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

    # Create initial LOCAL_MOCK Payment record via PaymentAdapter
    from app.payments.adapter import MockPaymentAdapter

    adapter = MockPaymentAdapter()
    payment = adapter.create_payment(
        db,
        order=order,
        correlation_id=corr_id,
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
    """Release ACTIVE inventory reservations and restore voucher quota on cancellation/expiry."""
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
        if inv is not None:
            inv.reserved_qty = max(0, inv.reserved_qty - res.quantity)
            inv.version += 1
            inv.updated_at = now
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

    # Restore voucher redemption if still APPLIED
    redemption = db.scalar(
        select(VoucherRedemption)
        .where(VoucherRedemption.order_id == order.id, VoucherRedemption.status == "APPLIED")
        .with_for_update()
    )
    if redemption is not None:
        v = db.scalar(select(Voucher).where(Voucher.id == redemption.voucher_id).with_for_update())
        if v is not None:
            v.used_count = max(0, v.used_count - 1)
        redemption.status = "RELEASED"

    return released


def cancel_order(
    db: Session,
    *,
    order: Order,
    actor: str,
    reason: str = "Cancelled by customer",
) -> Order:
    """Validate state transition, cancel order, release reservations, and record event."""
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
