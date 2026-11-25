"""Customer Storefront & restricted Test-Payment API routes for FinShop LIVE."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.auth import (
    CUSTOMER_COOKIE_NAME,
    CustomerPrincipal,
    generate_customer_token,
    get_current_customer,
    verify_mock_provider_secret,
)
from app.core.config import get_settings
from app.core.errors import CommerceError, ErrorCode
from app.core.rate_limit import (
    enforce_auth_rate_limit,
    enforce_checkout_rate_limit,
    enforce_payment_rate_limit,
)
from app.db.session import get_db
from app.models.entities import (
    Cart,
    CartItem,
    CommerceEvent,
    CommerceSession,
    Complaint,
    Customer,
    Inventory,
    LiveComment,
    LiveSession,
    Merchant,
    Order,
    Payment,
    Product,
    ProductVariant,
    RefundRequest,
    Voucher,
)
from app.models.enums import (
    ComplaintStatus,
    LiveSessionStatus,
    PaymentStatus,
    RefundRequestStatus,
)
from app.payments.adapter import MockPaymentAdapter
from app.schemas.commerce import (
    ApplyVoucherRequest,
    CartItemAddRequest,
    CartItemUpdateRequest,
    CartLineResponse,
    CartResponse,
    CheckoutRequest,
    CheckoutResponse,
    ComplaintResponse,
    CreateComplaintRequest,
    CreateCustomerSessionRequest,
    CreateRefundRequest,
    CustomerIdentityResponse,
    CustomerMockPayRequest,
    LiveCommentCreateRequest,
    LiveCommentResponse,
    LiveProductItemResponse,
    LiveSessionResponse,
    MockPaymentNotifyRequest,
    OrderItemResponse,
    OrderResponse,
    PaymentResponse,
    ProductResponse,
    RefundRequestResponse,
    VariantResponse,
    VoucherResponse,
)
from app.services.checkout import (
    cancel_order,
    create_order_from_items,
    evaluate_voucher,
    price_cart,
    resolve_effective_unit_price,
)
from app.services.events import record_commerce_event
from app.services.seed import (
    DEFAULT_LIVE_SESSION_ID,
    DEFAULT_MERCHANT_ID,
    DEFAULT_SESSION_ID,
    seed_commerce_data,
)
from app.services.sync import sync_confirmed_payments_to_orders

router = APIRouter(tags=["storefront"])


def _to_payment_response(p: Payment) -> PaymentResponse:
    return PaymentResponse(
        id=p.id,
        order_id=p.order_id,
        merchant_id=p.merchant_id,
        provider=p.provider,
        provider_ref=p.provider_ref,
        correlation_id=p.correlation_id,
        amount_vnd=p.amount_vnd,
        refunded_amount_vnd=p.refunded_amount_vnd,
        refundable_remaining_vnd=max(0, p.amount_vnd - p.refunded_amount_vnd),
        status=p.status,
        is_synced_to_order=p.is_synced_to_order,
        reconciliation_required=p.reconciliation_required,
        reconciliation_note=p.reconciliation_note,
        confirmed_at=p.confirmed_at,
        created_at=p.created_at,
    )


def _to_order_response(order: Order) -> OrderResponse:
    pay = order.payments[0] if order.payments else None
    return OrderResponse(
        id=order.id,
        order_code=order.order_code,
        merchant_id=order.merchant_id,
        commerce_session_id=order.commerce_session_id,
        customer_id=order.customer_id,
        status=order.status,
        payment_status=order.payment_status,
        reconciliation_status=order.reconciliation_status,
        reconciliation_reason=order.reconciliation_reason,
        subtotal_vnd=order.subtotal_vnd,
        discount_vnd=order.discount_vnd,
        shipping_fee_vnd=order.shipping_fee_vnd,
        total_vnd=order.total_vnd,
        voucher_code=order.voucher_code,
        recipient_name=order.recipient_name,
        recipient_phone=order.recipient_phone,
        shipping_address=order.shipping_address,
        customer_note=order.customer_note,
        payment_method=order.payment_method,
        correlation_id=order.correlation_id,
        version=order.version,
        items=[
            OrderItemResponse(
                id=oi.id,
                product_id=oi.product_id,
                variant_id=oi.variant_id,
                sku_snapshot=oi.sku_snapshot,
                product_title_snapshot=oi.product_title_snapshot,
                variant_title_snapshot=oi.variant_title_snapshot,
                image_url_snapshot=oi.image_url_snapshot,
                unit_price_vnd=oi.unit_price_vnd,
                original_price_vnd=oi.original_price_vnd,
                is_flash_sale_price=oi.is_flash_sale_price,
                quantity=oi.quantity,
                line_total_vnd=oi.line_total_vnd,
            )
            for oi in (order.items or [])
        ],
        payment=_to_payment_response(pay) if pay is not None else None,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )


def _serialize_product(
    db: Session,
    product: Product,
    *,
    active_live_id: str | None = None,
) -> ProductResponse:
    now = datetime.now(UTC)
    variants_resp: list[VariantResponse] = []
    for v in product.variants:
        if not v.is_active:
            continue
        inv = v.inventory
        on_hand = inv.on_hand_qty if inv else 0
        reserved = inv.reserved_qty if inv else 0
        avail = inv.available_qty if inv else 0
        low_th = inv.low_stock_threshold if inv else 5
        eff_price, is_fs, _ = resolve_effective_unit_price(
            db,
            variant=v,
            live_session_id=active_live_id,
            quantity=1,
            now=now,
            lock_live_product=False,
        )
        variants_resp.append(
            VariantResponse(
                id=v.id,
                product_id=v.product_id,
                sku=v.sku,
                title=v.title,
                price_vnd=v.price_vnd,
                compare_at_price_vnd=v.compare_at_price_vnd,
                effective_price_vnd=eff_price,
                is_flash_sale=is_fs,
                image_url=v.image_url,
                on_hand_qty=on_hand,
                reserved_qty=reserved,
                available_qty=avail,
                low_stock_threshold=low_th,
                is_active=v.is_active,
            )
        )
    prices = [vr.effective_price_vnd for vr in variants_resp] or [0]
    return ProductResponse(
        id=product.id,
        merchant_id=product.merchant_id,
        slug=product.slug,
        title=product.title,
        category=product.category,
        description=product.description,
        image_urls=list(product.image_urls_json or []),
        is_featured=product.is_featured,
        is_active=product.is_active,
        is_test_data=product.is_test_data,
        min_price_vnd=min(prices),
        max_price_vnd=max(prices),
        total_available_qty=sum(vr.available_qty for vr in variants_resp),
        variants=variants_resp,
    )


def _get_or_create_cart(db: Session, principal: CustomerPrincipal) -> Cart:
    cart = db.scalar(
        select(Cart)
        .options(selectinload(Cart.items))
        .where(
            Cart.customer_id == principal.customer.id,
            Cart.commerce_session_id == principal.session.id,
        )
    )
    if cart is None:
        cart = Cart(
            id=f"CART-{uuid.uuid4().hex[:10].upper()}",
            merchant_id=principal.merchant_id,
            commerce_session_id=principal.session.id,
            customer_id=principal.customer.id,
        )
        db.add(cart)
        db.commit()
        db.refresh(cart)
    return cart


def _build_cart_response(db: Session, cart: Cart, customer_id: str) -> CartResponse:
    summary = price_cart(db, cart=cart, customer_id=customer_id, strict_voucher=False)
    lines: list[CartLineResponse] = []
    for ln in summary.lines:
        inv = db.scalar(select(Inventory).where(Inventory.variant_id == ln.variant.id))
        lines.append(
            CartLineResponse(
                variant_id=ln.variant.id,
                product_id=ln.product.id,
                product_title=ln.product.title,
                variant_title=ln.variant.title,
                sku=ln.variant.sku,
                image_url=ln.variant.image_url,
                quantity=ln.quantity,
                unit_price_vnd=ln.unit_price_vnd,
                original_price_vnd=ln.original_price_vnd,
                is_flash_sale_price=ln.is_flash_sale_price,
                line_total_vnd=ln.line_total_vnd,
                available_qty=inv.available_qty if inv else 0,
            )
        )
    return CartResponse(
        cart_id=cart.id,
        items=lines,
        subtotal_vnd=summary.subtotal_vnd,
        discount_vnd=summary.discount_vnd,
        shipping_fee_vnd=summary.shipping_fee_vnd,
        total_vnd=summary.total_vnd,
        applied_voucher_code=cart.applied_voucher_code,
        voucher_title=summary.voucher.title if summary.voucher else None,
        voucher_error=summary.voucher_error,
    )


@router.get(
    "/store/bootstrap", summary="Public storefront bootstrap metadata (zero session tokens)"
)
def store_bootstrap(db: Session = Depends(get_db)) -> dict[str, Any]:
    merchant = db.get(Merchant, DEFAULT_MERCHANT_ID)
    if merchant is None:
        seed_commerce_data(db)
        merchant = db.get(Merchant, DEFAULT_MERCHANT_ID)
    csess = db.get(CommerceSession, DEFAULT_SESSION_ID)
    categories = sorted(
        {
            c
            for c in db.scalars(
                select(Product.category).where(
                    Product.merchant_id == DEFAULT_MERCHANT_ID,
                    Product.is_active.is_(True),
                )
            ).all()
        }
    )
    return {
        "brand": "FinShop LIVE",
        "source_mode": "MOCK",
        "merchant": {
            "id": merchant.id if merchant else DEFAULT_MERCHANT_ID,
            "name": merchant.name if merchant else "FinShop Official Store (Test)",
            "description": merchant.description if merchant else "",
        },
        "commerce_session": {
            "id": csess.id if csess else DEFAULT_SESSION_ID,
            "world_epoch": csess.world_epoch if csess else 1,
        },
        "categories": categories,
        "payment_methods": [
            {
                "id": "LOCAL_MOCK",
                "label_vi": "Thanh toán thử nghiệm nội bộ (LOCAL_MOCK)",
                "available": True,
            },
            {
                "id": "VNPAY_SANDBOX",
                "label_vi": "VNPAY Sandbox (Sắp ra mắt ở Prompt 11 — chưa khả dụng)",
                "available": False,
            },
        ],
    }


def _issue_guest_customer(
    payload: CreateCustomerSessionRequest,
    response: Response,
    db: Session,
) -> CustomerIdentityResponse:
    if db.get(CommerceSession, DEFAULT_SESSION_ID) is None:
        seed_commerce_data(db)
    settings = get_settings()
    now = datetime.now(UTC)
    cid = f"CUST-{uuid.uuid4().hex[:8].upper()}"
    tok = generate_customer_token()
    cust = Customer(
        id=cid,
        merchant_id=DEFAULT_MERCHANT_ID,
        commerce_session_id=DEFAULT_SESSION_ID,
        display_name=payload.display_name.strip(),
        phone=payload.phone.strip(),
        email=f"{cid.lower()}@finshop.test",
        default_address=payload.default_address.strip(),
        session_token=tok,
        is_test_identity=True,
        token_expires_at=now + timedelta(seconds=settings.customer_session_ttl_seconds),
        revoked_at=None,
    )
    db.add(cust)
    db.commit()
    db.refresh(cust)
    response.set_cookie(
        key=CUSTOMER_COOKIE_NAME,
        value=tok,
        httponly=True,
        samesite="lax",
        secure=not settings.is_local_env,
        max_age=settings.customer_session_ttl_seconds,
    )
    return CustomerIdentityResponse(
        id=cust.id,
        merchant_id=cust.merchant_id,
        commerce_session_id=cust.commerce_session_id,
        display_name=cust.display_name,
        phone=cust.phone,
        email=cust.email,
        default_address=cust.default_address,
        session_token=cust.session_token,
        is_test_identity=cust.is_test_identity,
    )


@router.post(
    "/store/customers/session",
    response_model=CustomerIdentityResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an isolated guest customer session (returns token only to caller)",
)
def create_customer_session(
    payload: CreateCustomerSessionRequest,
    response: Response,
    _rl: None = Depends(enforce_auth_rate_limit),
    db: Session = Depends(get_db),
) -> CustomerIdentityResponse:
    return _issue_guest_customer(payload, response, db)


@router.post(
    "/store/customers/guest",
    response_model=CustomerIdentityResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an isolated guest customer session (alias)",
)
def create_guest_customer_session(
    payload: CreateCustomerSessionRequest,
    response: Response,
    _rl: None = Depends(enforce_auth_rate_limit),
    db: Session = Depends(get_db),
) -> CustomerIdentityResponse:
    return _issue_guest_customer(payload, response, db)


@router.get(
    "/store/customers/me",
    response_model=CustomerIdentityResponse,
    summary="Return the currently authenticated customer identity",
)
def get_my_customer_profile(
    principal: CustomerPrincipal = Depends(get_current_customer),
) -> CustomerIdentityResponse:
    c = principal.customer
    return CustomerIdentityResponse(
        id=c.id,
        merchant_id=c.merchant_id,
        commerce_session_id=c.commerce_session_id,
        display_name=c.display_name,
        phone=c.phone,
        email=c.email,
        default_address=c.default_address,
        session_token=c.session_token,
        is_test_identity=c.is_test_identity,
    )


@router.post("/store/customers/logout", summary="Revoke the active customer session token")
def logout_customer_session(
    response: Response,
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    principal.customer.revoked_at = datetime.now(UTC)
    db.commit()
    response.delete_cookie(CUSTOMER_COOKIE_NAME)
    return {"revoked": True, "customer_id": principal.customer.id}


@router.get("/store/products", response_model=list[ProductResponse])
def list_products(
    q: str | None = Query(default=None, max_length=120),
    category: str | None = Query(default=None, max_length=64),
    featured: bool | None = Query(default=None),
    recommended: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> list[ProductResponse]:
    """List active products with search, category filter, or deterministic local recommendation."""
    stmt = (
        select(Product)
        .options(selectinload(Product.variants).selectinload(ProductVariant.inventory))
        .where(Product.merchant_id == DEFAULT_MERCHANT_ID, Product.is_active.is_(True))
        .order_by(Product.is_featured.desc(), Product.created_at.asc())
    )
    if category and category.strip() and category.strip() != "Tất cả":
        stmt = stmt.where(Product.category == category.strip())
    if featured is not None:
        stmt = stmt.where(Product.is_featured.is_(featured))

    products = list(db.scalars(stmt).all())
    if q and q.strip():
        needle = q.strip().lower()
        products = [
            p
            for p in products
            if needle in p.title.lower()
            or needle in p.category.lower()
            or needle in p.description.lower()
        ]

    active_live = db.scalar(
        select(LiveSession.id).where(
            LiveSession.merchant_id == DEFAULT_MERCHANT_ID,
            LiveSession.status == LiveSessionStatus.LIVE.value,
        )
    )
    serialized = [_serialize_product(db, p, active_live_id=active_live) for p in products]
    if recommended:
        # Documented local deterministic selection strategy:
        # prioritize in-stock featured items, then round-robin across categories for diversity.
        in_stock = [s for s in serialized if s.total_available_qty > 0]
        by_cat: dict[str, list[ProductResponse]] = {}
        for item in in_stock:
            by_cat.setdefault(item.category, []).append(item)
        ordered: list[ProductResponse] = []
        while any(by_cat.values()) and len(ordered) < 8:
            for cat_key in sorted(by_cat.keys()):
                bucket = by_cat[cat_key]
                if bucket:
                    ordered.append(bucket.pop(0))
                    if len(ordered) >= 8:
                        break
        return ordered
    return serialized


@router.get("/store/products/{product_id}", response_model=ProductResponse)
def get_product_detail(product_id: str, db: Session = Depends(get_db)) -> ProductResponse:
    product = db.scalar(
        select(Product)
        .options(selectinload(Product.variants).selectinload(ProductVariant.inventory))
        .where(
            Product.id == product_id,
            Product.merchant_id == DEFAULT_MERCHANT_ID,
            Product.is_active.is_(True),
        )
    )
    if product is None:
        raise CommerceError(
            ErrorCode.NOT_FOUND, f"Không tìm thấy sản phẩm '{product_id}'", status_code=404
        )
    active_live = db.scalar(
        select(LiveSession.id).where(
            LiveSession.merchant_id == DEFAULT_MERCHANT_ID,
            LiveSession.status == LiveSessionStatus.LIVE.value,
        )
    )
    return _serialize_product(db, product, active_live_id=active_live)


@router.get("/store/vouchers", response_model=list[VoucherResponse])
def list_store_vouchers(db: Session = Depends(get_db)) -> list[VoucherResponse]:
    now = datetime.now(UTC)
    rows = list(
        db.scalars(
            select(Voucher)
            .where(
                Voucher.merchant_id == DEFAULT_MERCHANT_ID,
                Voucher.is_active.is_(True),
                Voucher.starts_at <= now,
                Voucher.ends_at >= now,
            )
            .order_by(Voucher.min_order_vnd.asc())
        ).all()
    )
    return [
        VoucherResponse(
            id=v.id,
            code=v.code,
            title=v.title,
            discount_type=v.discount_type,
            discount_value=v.discount_value,
            max_discount_vnd=v.max_discount_vnd,
            min_order_vnd=v.min_order_vnd,
            usage_limit=v.usage_limit,
            used_count=v.used_count,
            per_customer_limit=v.per_customer_limit,
            starts_at=v.starts_at,
            ends_at=v.ends_at,
            is_active=v.is_active,
        )
        for v in rows
    ]


@router.get("/store/live", response_model=LiveSessionResponse)
def get_active_live_session(db: Session = Depends(get_db)) -> LiveSessionResponse:
    live = db.scalar(
        select(LiveSession)
        .options(selectinload(LiveSession.live_products))
        .where(LiveSession.id == DEFAULT_LIVE_SESSION_ID)
    )
    if live is None:
        seed_commerce_data(db)
        live = db.scalar(
            select(LiveSession)
            .options(selectinload(LiveSession.live_products))
            .where(LiveSession.id == DEFAULT_LIVE_SESSION_ID)
        )
    if live is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Không có phiên LIVE nào", status_code=404)

    merchant = db.get(Merchant, live.merchant_id)
    now = datetime.now(UTC)
    items: list[LiveProductItemResponse] = []
    pinned: LiveProductItemResponse | None = None

    for lp in sorted(live.live_products, key=lambda x: (not x.is_pinned, x.sort_order)):
        prod = db.get(Product, lp.product_id)
        var = db.get(ProductVariant, lp.variant_id)
        if prod is None or var is None or not prod.is_active or not var.is_active:
            continue
        inv = db.scalar(select(Inventory).where(Inventory.variant_id == var.id))
        avail = inv.available_qty if inv else 0
        eff_price, _, _ = resolve_effective_unit_price(
            db,
            variant=var,
            live_session_id=live.id,
            quantity=1,
            now=now,
            lock_live_product=False,
        )
        fs_rem = (
            max(
                0,
                lp.flash_sale_stock_limit - lp.flash_sale_reserved_qty - lp.flash_sale_sold_qty,
            )
            if lp.flash_sale_stock_limit is not None
            else None
        )
        item = LiveProductItemResponse(
            id=lp.id,
            product_id=prod.id,
            variant_id=var.id,
            product_title=prod.title,
            variant_title=var.title,
            sku=var.sku,
            image_url=var.image_url,
            original_price_vnd=var.price_vnd,
            effective_price_vnd=eff_price,
            flash_sale_price_vnd=lp.flash_sale_price_vnd,
            flash_sale_stock_limit=lp.flash_sale_stock_limit,
            flash_sale_reserved_qty=lp.flash_sale_reserved_qty,
            flash_sale_sold_qty=lp.flash_sale_sold_qty,
            flash_sale_remaining=fs_rem,
            flash_sale_starts_at=lp.flash_sale_starts_at,
            flash_sale_ends_at=lp.flash_sale_ends_at,
            available_qty=avail,
            is_pinned=lp.is_pinned,
            sort_order=lp.sort_order,
        )
        items.append(item)
        if lp.is_pinned and pinned is None:
            pinned = item

    # Real persisted counts — NEVER fabricated viewer counts!
    participants = (
        db.scalar(
            select(func.count(Customer.id)).where(
                Customer.commerce_session_id == live.commerce_session_id
            )
        )
        or 0
    )
    live_orders = (
        db.scalar(select(func.count(Order.id)).where(Order.live_session_id == live.id)) or 0
    )
    comments = list(
        db.scalars(
            select(LiveComment)
            .where(LiveComment.live_session_id == live.id)
            .order_by(LiveComment.created_at.desc())
            .limit(30)
        ).all()
    )
    return LiveSessionResponse(
        id=live.id,
        merchant_id=live.merchant_id,
        merchant_name=merchant.name if merchant else "FinShop Official",
        title=live.title,
        host_name=live.host_name,
        status=live.status,
        is_simulated_stream=live.is_simulated_stream,
        media_label_vi=live.media_label_vi,
        starts_at=live.starts_at,
        ends_at=live.ends_at,
        active_participants_count=int(participants),
        session_orders_count=int(live_orders),
        pinned_product=pinned or (items[0] if items else None),
        products=items,
        recent_comments=[
            LiveCommentResponse(
                id=c.id,
                author_name=c.author_name,
                message=c.message,
                created_at=c.created_at,
            )
            for c in reversed(comments)
        ],
    )


@router.post(
    "/store/live/{live_id}/comments",
    response_model=LiveCommentResponse,
    status_code=status.HTTP_201_CREATED,
)
def post_live_comment(
    live_id: str,
    payload: LiveCommentCreateRequest,
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> LiveCommentResponse:
    live = db.get(LiveSession, live_id)
    if live is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Phiên LIVE không tồn tại", status_code=404)
    cmt = LiveComment(
        id=f"LCMT-{uuid.uuid4().hex[:10].upper()}",
        live_session_id=live.id,
        customer_id=principal.customer.id,
        author_name=principal.customer.display_name,
        message=payload.message.strip(),
    )
    db.add(cmt)
    db.commit()
    db.refresh(cmt)
    return LiveCommentResponse(
        id=cmt.id,
        author_name=cmt.author_name,
        message=cmt.message,
        created_at=cmt.created_at,
    )


# --- Cart Routes ---
@router.get("/store/cart", response_model=CartResponse)
def get_cart(
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CartResponse:
    cart = _get_or_create_cart(db, principal)
    return _build_cart_response(db, cart, principal.customer.id)


@router.post("/store/cart/items", response_model=CartResponse)
def add_cart_item(
    payload: CartItemAddRequest,
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CartResponse:
    variant = db.scalar(
        select(ProductVariant).where(
            ProductVariant.id == payload.variant_id,
            ProductVariant.merchant_id == principal.merchant_id,
            ProductVariant.is_active.is_(True),
        )
    )
    if variant is None:
        raise CommerceError(
            ErrorCode.NOT_FOUND, "Phân loại sản phẩm không tồn tại", status_code=404
        )
    inv = db.scalar(select(Inventory).where(Inventory.variant_id == variant.id))
    avail = inv.available_qty if inv else 0

    cart = _get_or_create_cart(db, principal)
    existing = next((ci for ci in cart.items if ci.variant_id == variant.id), None)
    new_qty = (existing.quantity if existing else 0) + payload.quantity
    if new_qty > avail:
        raise CommerceError(
            ErrorCode.OUT_OF_STOCK,
            f"Chỉ còn {avail} sản phẩm khả dụng trong kho",
            status_code=409,
        )
    if existing is not None:
        existing.quantity = new_qty
        if payload.live_session_id:
            existing.live_session_id = payload.live_session_id
    else:
        db.add(
            CartItem(
                id=f"CI-{uuid.uuid4().hex[:10].upper()}",
                cart_id=cart.id,
                variant_id=variant.id,
                live_session_id=payload.live_session_id,
                quantity=payload.quantity,
            )
        )
    db.commit()
    db.refresh(cart)
    return _build_cart_response(db, cart, principal.customer.id)


@router.patch("/store/cart/items/{variant_id}", response_model=CartResponse)
def update_cart_item(
    variant_id: str,
    payload: CartItemUpdateRequest,
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CartResponse:
    cart = _get_or_create_cart(db, principal)
    item = next((ci for ci in cart.items if ci.variant_id == variant_id), None)
    if item is None:
        raise CommerceError(
            ErrorCode.NOT_FOUND, "Sản phẩm không có trong giỏ hàng", status_code=404
        )
    inv = db.scalar(select(Inventory).where(Inventory.variant_id == variant_id))
    avail = inv.available_qty if inv else 0
    if payload.quantity > avail:
        raise CommerceError(
            ErrorCode.OUT_OF_STOCK,
            f"Chỉ còn {avail} sản phẩm khả dụng trong kho",
            status_code=409,
        )
    item.quantity = payload.quantity
    db.commit()
    db.refresh(cart)
    return _build_cart_response(db, cart, principal.customer.id)


@router.delete("/store/cart/items/{variant_id}", response_model=CartResponse)
def remove_cart_item(
    variant_id: str,
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CartResponse:
    cart = _get_or_create_cart(db, principal)
    item = next((ci for ci in cart.items if ci.variant_id == variant_id), None)
    if item is not None:
        db.delete(item)
        db.commit()
        db.refresh(cart)
    return _build_cart_response(db, cart, principal.customer.id)


@router.post("/store/cart/voucher", response_model=CartResponse)
def apply_cart_voucher(
    payload: ApplyVoucherRequest,
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CartResponse:
    cart = _get_or_create_cart(db, principal)
    summary = price_cart(db, cart=cart, customer_id=principal.customer.id, strict_voucher=False)
    if summary.subtotal_vnd <= 0:
        raise CommerceError(ErrorCode.BAD_REQUEST, "Giỏ hàng trống", status_code=400)
    v, _ = evaluate_voucher(
        db,
        merchant_id=principal.merchant_id,
        customer_id=principal.customer.id,
        voucher_code=payload.voucher_code,
        subtotal_vnd=summary.subtotal_vnd,
        now=datetime.now(UTC),
        lock_voucher=False,
    )
    cart.applied_voucher_code = v.code
    db.commit()
    db.refresh(cart)
    return _build_cart_response(db, cart, principal.customer.id)


@router.delete("/store/cart/voucher", response_model=CartResponse)
def remove_cart_voucher(
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CartResponse:
    cart = _get_or_create_cart(db, principal)
    cart.applied_voucher_code = None
    db.commit()
    db.refresh(cart)
    return _build_cart_response(db, cart, principal.customer.id)


# --- Checkout & Orders ---
@router.post(
    "/store/checkout",
    response_model=CheckoutResponse,
    status_code=status.HTTP_201_CREATED,
)
def checkout_order(
    payload: CheckoutRequest,
    response: Response,
    idempotency_header: str | None = Header(default=None, alias="X-Idempotency-Key"),
    _rl: None = Depends(enforce_checkout_rate_limit),
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CheckoutResponse:
    idemp_key = (idempotency_header or payload.idempotency_key or "").strip()
    if not idemp_key:
        raise CommerceError(
            ErrorCode.BAD_REQUEST,
            "Header X-Idempotency-Key hoặc trường idempotency_key là bắt buộc khi đặt hàng",
            status_code=400,
        )

    cart: Cart | None = None
    direct_items: list[tuple[str, int, str | None]] | None = None
    eff_voucher = payload.voucher_code

    if payload.direct_items:
        direct_items = [(d.variant_id, d.quantity, d.live_session_id) for d in payload.direct_items]
    else:
        cart = _get_or_create_cart(db, principal)
        if not eff_voucher:
            eff_voucher = cart.applied_voucher_code

    order, payment, replay = create_order_from_items(
        db,
        customer=principal.customer,
        session_obj=principal.session,
        idempotency_key=idemp_key,
        recipient_name=payload.recipient_name,
        recipient_phone=payload.recipient_phone,
        shipping_address=payload.shipping_address,
        customer_note=payload.customer_note,
        voucher_code=eff_voucher,
        payment_method=payload.payment_method,
        direct_items=direct_items,
        clear_cart=cart if direct_items is None else None,
        expected_total_vnd=payload.expected_total_vnd,
    )
    if replay:
        response.status_code = status.HTTP_200_OK

    loaded = db.scalar(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.payments))
        .where(Order.id == order.id)
    )
    assert loaded is not None  # noqa: S101
    return CheckoutResponse(
        order=_to_order_response(loaded),
        payment=_to_payment_response(payment),
        idempotent_replay=replay,
    )


@router.get("/store/orders", response_model=list[OrderResponse])
def list_customer_orders(
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> list[OrderResponse]:
    rows = list(
        db.scalars(
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.payments))
            .where(
                Order.customer_id == principal.customer.id,
                Order.merchant_id == principal.merchant_id,
            )
            .order_by(Order.created_at.desc())
        ).all()
    )
    return [_to_order_response(o) for o in rows]


@router.get("/store/orders/{order_id}", response_model=dict[str, Any])
def get_customer_order_detail(
    order_id: str,
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    order = db.scalar(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.payments))
        .where(
            Order.id == order_id,
            Order.customer_id == principal.customer.id,
            Order.merchant_id == principal.merchant_id,
        )
    )
    if order is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Không tìm thấy đơn hàng", status_code=404)

    events = list(
        db.scalars(
            select(CommerceEvent)
            .where(CommerceEvent.order_id == order.id)
            .order_by(CommerceEvent.occurred_at.asc())
        ).all()
    )
    complaints = list(
        db.scalars(
            select(Complaint)
            .where(Complaint.order_id == order.id, Complaint.customer_id == principal.customer.id)
            .order_by(Complaint.created_at.desc())
        ).all()
    )
    refunds = list(
        db.scalars(
            select(RefundRequest)
            .where(
                RefundRequest.order_id == order.id,
                RefundRequest.customer_id == principal.customer.id,
            )
            .order_by(RefundRequest.created_at.desc())
        ).all()
    )
    return {
        "order": _to_order_response(order).model_dump(),
        "timeline": [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "occurred_at": e.occurred_at.isoformat(),
                "payload": e.payload_json,
            }
            for e in events
        ],
        "complaints": [
            ComplaintResponse(
                id=c.id,
                order_id=c.order_id,
                customer_id=c.customer_id,
                category=c.category,
                description=c.description,
                claimed_receipt_ref=c.claimed_receipt_ref,
                evidence_kind=c.evidence_kind,
                status=c.status,
                merchant_response=c.merchant_response,
                created_at=c.created_at,
                updated_at=c.updated_at,
            ).model_dump()
            for c in complaints
        ],
        "refund_requests": [
            RefundRequestResponse(
                id=r.id,
                order_id=r.order_id,
                payment_id=r.payment_id,
                customer_id=r.customer_id,
                requested_amount_vnd=r.requested_amount_vnd,
                reason=r.reason,
                status=r.status,
                merchant_note=r.merchant_note,
                created_at=r.created_at,
                updated_at=r.updated_at,
            ).model_dump()
            for r in refunds
        ],
    }


@router.post("/store/orders/{order_id}/cancel", response_model=OrderResponse)
def cancel_customer_order(
    order_id: str,
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> OrderResponse:
    order = db.scalar(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.payments))
        .where(
            Order.id == order_id,
            Order.customer_id == principal.customer.id,
            Order.merchant_id == principal.merchant_id,
        )
        .with_for_update()
    )
    if order is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Không tìm thấy đơn hàng", status_code=404)
    updated = cancel_order(db, order=order, actor=f"customer:{principal.customer.id}")
    return _to_order_response(updated)


@router.post(
    "/store/orders/{order_id}/complaints",
    response_model=ComplaintResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_order_complaint(
    order_id: str,
    payload: CreateComplaintRequest,
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> ComplaintResponse:
    """Customer complaint or receipt reference is stored strictly as untrusted CLAIM evidence."""
    order = db.scalar(
        select(Order).where(
            Order.id == order_id,
            Order.customer_id == principal.customer.id,
            Order.merchant_id == principal.merchant_id,
        )
    )
    if order is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Không tìm thấy đơn hàng", status_code=404)

    comp = Complaint(
        id=f"CMP-{uuid.uuid4().hex[:10].upper()}",
        order_id=order.id,
        merchant_id=order.merchant_id,
        commerce_session_id=order.commerce_session_id,
        customer_id=principal.customer.id,
        category=payload.category.strip().upper(),
        description=payload.description.strip(),
        claimed_receipt_ref=(
            payload.claimed_receipt_ref.strip() if payload.claimed_receipt_ref else None
        ),
        evidence_kind="CLAIM",
        status=ComplaintStatus.OPEN.value,
    )
    db.add(comp)
    record_commerce_event(
        db,
        merchant_id=order.merchant_id,
        commerce_session_id=order.commerce_session_id,
        world_epoch=order.world_epoch,
        event_type="COMPLAINT_SUBMITTED",
        order_id=order.id,
        correlation_id=order.correlation_id,
        payload={
            "complaint_id": comp.id,
            "category": comp.category,
            "evidence_kind": "CLAIM",
            "claimed_receipt_ref": comp.claimed_receipt_ref,
        },
    )
    db.commit()
    db.refresh(comp)
    return ComplaintResponse(
        id=comp.id,
        order_id=comp.order_id,
        customer_id=comp.customer_id,
        category=comp.category,
        description=comp.description,
        claimed_receipt_ref=comp.claimed_receipt_ref,
        evidence_kind=comp.evidence_kind,
        status=comp.status,
        merchant_response=comp.merchant_response,
        created_at=comp.created_at,
        updated_at=comp.updated_at,
    )


@router.post(
    "/store/orders/{order_id}/refund-requests",
    response_model=RefundRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
def submit_refund_request(
    order_id: str,
    payload: CreateRefundRequest,
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> RefundRequestResponse:
    order = db.scalar(
        select(Order)
        .options(selectinload(Order.payments))
        .where(
            Order.id == order_id,
            Order.customer_id == principal.customer.id,
            Order.merchant_id == principal.merchant_id,
        )
    )
    if order is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Không tìm thấy đơn hàng", status_code=404)
    if not order.payments:
        raise CommerceError(
            ErrorCode.CONFLICT, "Đơn hàng chưa có giao dịch thanh toán", status_code=409
        )
    pay = order.payments[0]
    if pay.status not in (PaymentStatus.SUCCEEDED.value, PaymentStatus.PARTIALLY_REFUNDED.value):
        raise CommerceError(
            ErrorCode.CONFLICT,
            (
                "Chỉ có thể yêu cầu hoàn tiền cho giao dịch đã thanh toán thành công "
                f"(hiện tại: {pay.status})"
            ),
            status_code=409,
        )
    rem = pay.amount_vnd - pay.refunded_amount_vnd
    if payload.requested_amount_vnd > rem:
        raise CommerceError(
            ErrorCode.CONFLICT,
            (
                f"Số tiền hoàn yêu cầu ({payload.requested_amount_vnd:,}đ) "
                f"vượt số dư có thể hoàn ({rem:,}đ)"
            ),
            status_code=409,
        )

    req = RefundRequest(
        id=f"RREQ-{uuid.uuid4().hex[:10].upper()}",
        order_id=order.id,
        payment_id=pay.id,
        merchant_id=order.merchant_id,
        commerce_session_id=order.commerce_session_id,
        customer_id=principal.customer.id,
        requested_amount_vnd=payload.requested_amount_vnd,
        reason=payload.reason.strip(),
        status=RefundRequestStatus.REQUESTED.value,
    )
    db.add(req)
    record_commerce_event(
        db,
        merchant_id=order.merchant_id,
        commerce_session_id=order.commerce_session_id,
        world_epoch=order.world_epoch,
        event_type="REFUND_REQUESTED",
        order_id=order.id,
        payment_id=pay.id,
        correlation_id=order.correlation_id,
        payload={
            "refund_request_id": req.id,
            "requested_amount_vnd": req.requested_amount_vnd,
            "reason": req.reason,
        },
    )
    db.commit()
    db.refresh(req)
    return RefundRequestResponse(
        id=req.id,
        order_id=req.order_id,
        payment_id=req.payment_id,
        customer_id=req.customer_id,
        requested_amount_vnd=req.requested_amount_vnd,
        reason=req.reason,
        status=req.status,
        merchant_note=req.merchant_note,
        created_at=req.created_at,
        updated_at=req.updated_at,
    )


# --- Customer-Initiated LOCAL_MOCK Payment Action (no provider secret in browser) ---
@router.post(
    "/store/orders/{order_id}/mock-pay",
    summary="Customer-authenticated LOCAL_MOCK payment trigger (server-side provider callback)",
)
def customer_mock_pay_order(
    order_id: str,
    payload: CustomerMockPayRequest,
    _rl: None = Depends(enforce_payment_rate_limit),
    principal: CustomerPrincipal = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Allow an authenticated customer to complete a LOCAL_MOCK test payment for THEIR OWN order.

    The backend validates ownership and invokes `MockPaymentAdapter.process_provider_notification`
    server-side. The browser NEVER receives provider secrets and NEVER modifies other users' orders.
    """
    settings = get_settings()
    if settings.source_mode != "MOCK" or not settings.enable_mock_payment_test_api:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "LOCAL_MOCK test payment action is disabled",
            status_code=403,
        )
    order = db.scalar(
        select(Order)
        .options(selectinload(Order.payments))
        .where(
            Order.id == order_id,
            Order.customer_id == principal.customer.id,
            Order.merchant_id == principal.merchant_id,
        )
    )
    if order is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Không tìm thấy đơn hàng", status_code=404)
    if not order.payments:
        raise CommerceError(
            ErrorCode.CONFLICT, "Đơn hàng chưa có giao dịch thanh toán", status_code=409
        )
    pay = order.payments[0]
    adapter = MockPaymentAdapter()
    notif_id = f"MOCK-CUST-{uuid.uuid4().hex[:12].upper()}"
    updated_pay, is_dup = adapter.process_provider_notification(
        db,
        provider_ref=pay.provider_ref,
        notification_id=notif_id,
        merchant_id=order.merchant_id,
        order_id=order.id,
        amount_vnd=pay.amount_vnd,
        outcome=payload.outcome,
        raw_payload={"initiated_by": "customer_mock_pay", "customer_id": principal.customer.id},
    )
    synced_count = 0
    if payload.auto_sync_order and updated_pay.status == PaymentStatus.SUCCEEDED.value:
        synced_count = sync_confirmed_payments_to_orders(db, limit=10)
        db.refresh(updated_pay)

    return {
        "payment": _to_payment_response(updated_pay).model_dump(),
        "is_duplicate_notification": is_dup,
        "orders_synced": synced_count,
    }


# --- Restricted Server-Side Provider Callback Boundary (LOCAL_MOCK only) ---
@router.post(
    "/test-payments/mock/notify",
    summary="Server-authenticated LOCAL_MOCK payment provider notification boundary",
)
def mock_payment_provider_notify(
    payload: MockPaymentNotifyRequest,
    request: Request,
    _rl: None = Depends(enforce_payment_rate_limit),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Process a simulated LOCAL_MOCK payment gateway callback.

    Strictly protected by `X-Mock-Provider-Secret` matching `COMMERCE_MOCK_PROVIDER_SECRET`.
    Anonymous browser calls are rejected with 403.
    """
    verify_mock_provider_secret(request)
    adapter = MockPaymentAdapter()
    payment, is_duplicate = adapter.process_provider_notification(
        db,
        provider_ref=payload.provider_ref,
        notification_id=payload.notification_id,
        merchant_id=payload.merchant_id,
        order_id=payload.order_id,
        amount_vnd=payload.amount_vnd,
        outcome=payload.outcome,
    )
    synced_count = 0
    if payload.auto_sync_order and payment.status == PaymentStatus.SUCCEEDED.value:
        synced_count = sync_confirmed_payments_to_orders(db, limit=10)
        db.refresh(payment)

    return {
        "payment": _to_payment_response(payment).model_dump(),
        "is_duplicate_notification": is_duplicate,
        "orders_synced": synced_count,
    }
