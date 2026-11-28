"""Merchant Center API routes (desktop-first seller dashboard) for FinShop LIVE."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.auth import (
    MERCHANT_COOKIE_NAME,
    MERCHANT_TOKEN_HEADER,
    MerchantPrincipal,
    create_merchant_session,
    get_current_merchant,
    revoke_merchant_session,
    verify_password,
)
from app.core.config import get_settings
from app.core.errors import CommerceError, ErrorCode
from app.core.rate_limit import enforce_auth_rate_limit
from app.db.session import get_db
from app.models.entities import (
    CommerceEvent,
    CommerceSession,
    Complaint,
    EventOutbox,
    Inventory,
    InventoryReservation,
    LiveProduct,
    LiveSession,
    MerchantAccount,
    Order,
    Payment,
    PaymentQueryAttempt,
    Product,
    ProductVariant,
    ReconciliationRecord,
    RefundRequest,
    Voucher,
)
from app.models.enums import (
    OrderStatus,
    OutboxStatus,
    PaymentStatus,
    ReconciliationStatus,
    RefundRequestStatus,
    ReservationStatus,
    can_transition_order,
)
from app.payments.registry import get_payment_adapter
from app.schemas.commerce import (
    CommerceEventItemResponse,
    ComplaintResponse,
    MerchantAdjustInventoryRequest,
    MerchantCreateProductRequest,
    MerchantCreateVariantRequest,
    MerchantCreateVoucherRequest,
    MerchantLoginRequest,
    MerchantLoginResponse,
    MerchantRefundDecisionRequest,
    MerchantUpdateComplaintRequest,
    MerchantUpdateLiveProductRequest,
    MerchantUpdateOrderStatusRequest,
    MerchantUpdateProductRequest,
    OrderResponse,
    PaymentQueryAttemptResponse,
    PaymentResponse,
    ProductResponse,
    ReconciliationRecordResponse,
    ReconciliationResolveRequest,
    RefundRequestResponse,
    VoucherResponse,
)
from app.services.events import record_commerce_event
from app.services.seed import (
    DEFAULT_SESSION_ID,
    seed_commerce_data,
)
from app.services.sync import sync_confirmed_payments_to_orders

router = APIRouter(prefix="/merchant", tags=["merchant"])


@router.post(
    "/auth/login",
    response_model=MerchantLoginResponse,
    summary="Authenticate merchant operator with username/password and issue revocable session",
)
def merchant_login(
    payload: MerchantLoginRequest,
    response: Response,
    _rl: None = Depends(enforce_auth_rate_limit),
    db: Session = Depends(get_db),
) -> MerchantLoginResponse:
    username = payload.username.strip()
    account = db.scalar(select(MerchantAccount).where(MerchantAccount.username == username))
    if (
        account is None
        or not account.is_active
        or not verify_password(payload.password, account.password_hash)
    ):
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            "Sai tên đăng nhập hoặc mật khẩu Kênh Người Bán",
            status_code=401,
        )

    csess = db.scalar(
        select(CommerceSession).where(
            CommerceSession.merchant_id == account.merchant_id,
            CommerceSession.is_active.is_(True),
        )
    )
    if csess is None:
        seed_commerce_data(db)
        csess = db.get(CommerceSession, DEFAULT_SESSION_ID)
    if csess is None:
        raise CommerceError(
            ErrorCode.FORBIDDEN, "No active commerce session for merchant", status_code=403
        )

    ms, raw_token = create_merchant_session(db, account=account, commerce_session=csess)
    db.commit()

    settings = get_settings()
    response.set_cookie(
        key=MERCHANT_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=not settings.is_local_env,
        max_age=settings.merchant_session_ttl_seconds,
    )
    return MerchantLoginResponse(
        session_id=ms.id,
        merchant_id=ms.merchant_id,
        commerce_session_id=ms.commerce_session_id,
        role=ms.role,
        operator_id=ms.operator_id,
        merchant_token=raw_token,
        expires_at=ms.expires_at,
        source_mode=settings.source_mode,
    )


@router.post("/auth/logout", summary="Revoke active merchant session")
def merchant_logout(
    request: Request,
    response: Response,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    raw = (
        request.headers.get(MERCHANT_TOKEN_HEADER, "").strip()
        or request.cookies.get(MERCHANT_COOKIE_NAME, "").strip()
    )
    revoked = revoke_merchant_session(db, raw_token=raw) if raw else False
    response.delete_cookie(MERCHANT_COOKIE_NAME)
    return {"revoked": revoked, "session_id": principal.session_id}


@router.get("/auth/me", summary="Return current authenticated merchant session metadata")
def merchant_me(
    principal: MerchantPrincipal = Depends(get_current_merchant),
) -> dict[str, Any]:
    return {
        "session_id": principal.session_id,
        "merchant_id": principal.merchant_id,
        "commerce_session_id": principal.commerce_session_id,
        "world_epoch": principal.world_epoch,
        "role": principal.role,
        "operator_id": principal.operator_id,
    }


@router.get("/overview", summary="Real database-derived merchant operational metrics")
def get_merchant_overview(
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    mid = principal.merchant_id
    confirmed_revenue_vnd = int(
        db.scalar(
            select(
                func.coalesce(func.sum(Payment.amount_vnd - Payment.refunded_amount_vnd), 0)
            ).where(
                Payment.merchant_id == mid,
                Payment.status.in_(
                    [
                        PaymentStatus.SUCCEEDED.value,
                        PaymentStatus.PARTIALLY_REFUNDED.value,
                        PaymentStatus.REFUNDED.value,
                    ]
                ),
            )
        )
        or 0
    )
    order_count = int(db.scalar(select(func.count(Order.id)).where(Order.merchant_id == mid)) or 0)
    pending_payments = int(
        db.scalar(
            select(func.count(Payment.id)).where(
                Payment.merchant_id == mid,
                Payment.status.in_([PaymentStatus.INITIATED.value, PaymentStatus.PENDING.value]),
            )
        )
        or 0
    )
    pending_fulfillment = int(
        db.scalar(
            select(func.count(Order.id)).where(
                Order.merchant_id == mid,
                Order.status.in_([OrderStatus.CONFIRMED.value, OrderStatus.PROCESSING.value]),
            )
        )
        or 0
    )
    refund_requests = int(
        db.scalar(
            select(func.count(RefundRequest.id)).where(
                RefundRequest.merchant_id == mid,
                RefundRequest.status == RefundRequestStatus.REQUESTED.value,
            )
        )
        or 0
    )
    open_complaints = int(
        db.scalar(
            select(func.count(Complaint.id)).where(
                Complaint.merchant_id == mid,
                Complaint.status.in_(["OPEN", "UNDER_REVIEW"]),
            )
        )
        or 0
    )
    invs = list(db.scalars(select(Inventory).where(Inventory.merchant_id == mid)).all())
    total_available_inventory = sum(i.available_qty for i in invs)
    low_stock_count = sum(1 for i in invs if i.available_qty <= i.low_stock_threshold)

    live_orders = int(
        db.scalar(
            select(func.count(Order.id)).where(
                Order.merchant_id == mid, Order.live_session_id.is_not(None)
            )
        )
        or 0
    )
    unsynced_payments = int(
        db.scalar(
            select(func.count(Payment.id)).where(
                Payment.merchant_id == mid,
                Payment.status == PaymentStatus.SUCCEEDED.value,
                Payment.is_synced_to_order.is_(False),
            )
        )
        or 0
    )
    reconciliation_required_count = int(
        db.scalar(
            select(func.count(Order.id)).where(
                Order.merchant_id == mid,
                Order.reconciliation_status == ReconciliationStatus.RECONCILIATION_REQUIRED.value,
            )
        )
        or 0
    )
    outbox_pending_count = int(
        db.scalar(
            select(func.count(EventOutbox.id)).where(
                EventOutbox.status == OutboxStatus.PENDING.value
            )
        )
        or 0
    )
    outbox_dead_letter_count = int(
        db.scalar(
            select(func.count(EventOutbox.id)).where(
                EventOutbox.status.in_(
                    [OutboxStatus.DEAD_LETTER.value, OutboxStatus.REVIEW_REQUIRED.value]
                )
            )
        )
        or 0
    )

    return {
        "merchant_id": mid,
        "commerce_session_id": principal.commerce_session_id,
        "source_mode": "MOCK",
        "metrics_note_vi": (
            "Toàn bộ số liệu được truy vấn trực tiếp từ PostgreSQL (dữ liệu thử nghiệm MOCK)."
        ),
        "confirmed_revenue_vnd": confirmed_revenue_vnd,
        "order_count": order_count,
        "pending_payments": pending_payments,
        "pending_fulfillment": pending_fulfillment,
        "unsynced_payments": unsynced_payments,
        "reconciliation_required_count": reconciliation_required_count,
        "outbox_pending_count": outbox_pending_count,
        "outbox_dead_letter_count": outbox_dead_letter_count,
        "refund_requests": refund_requests,
        "open_complaints": open_complaints,
        "total_available_inventory": total_available_inventory,
        "low_stock_products_count": low_stock_count,
        "live_session_orders_count": live_orders,
    }


@router.get("/products", response_model=list[ProductResponse])
def merchant_list_products(
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> list[ProductResponse]:
    from app.api.storefront import _serialize_product

    rows = list(
        db.scalars(
            select(Product)
            .options(selectinload(Product.variants).selectinload(ProductVariant.inventory))
            .where(Product.merchant_id == principal.merchant_id)
            .order_by(Product.created_at.desc())
        ).all()
    )
    return [_serialize_product(db, p) for p in rows]


@router.post("/products", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
def merchant_create_product(
    payload: MerchantCreateProductRequest,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> ProductResponse:
    from app.api.storefront import _serialize_product

    pid = f"PROD-{uuid.uuid4().hex[:8].upper()}"
    vid = f"VAR-{uuid.uuid4().hex[:8].upper()}"
    slug = f"{payload.sku.lower()}-{uuid.uuid4().hex[:4]}"
    prod = Product(
        id=pid,
        merchant_id=principal.merchant_id,
        slug=slug,
        title=payload.title.strip(),
        category=payload.category.strip(),
        description=payload.description.strip(),
        image_urls_json=[payload.image_url.strip()],
        is_featured=payload.is_featured,
        is_active=True,
        is_test_data=True,
    )
    db.add(prod)
    db.flush()

    var = ProductVariant(
        id=vid,
        product_id=pid,
        merchant_id=principal.merchant_id,
        sku=payload.sku.strip().upper(),
        title=payload.variant_title.strip(),
        price_vnd=payload.price_vnd,
        compare_at_price_vnd=payload.compare_at_price_vnd,
        image_url=payload.image_url.strip(),
        is_active=True,
    )
    db.add(var)
    db.flush()

    db.add(
        Inventory(
            id=f"INV-{vid}",
            variant_id=vid,
            merchant_id=principal.merchant_id,
            on_hand_qty=payload.initial_stock,
            reserved_qty=0,
            low_stock_threshold=5,
            version=1,
        )
    )
    record_commerce_event(
        db,
        merchant_id=principal.merchant_id,
        commerce_session_id=principal.commerce_session_id,
        world_epoch=principal.world_epoch,
        event_type="INVENTORY_UPDATED",
        payload={"product_id": pid, "variant_id": vid, "on_hand_qty": payload.initial_stock},
    )
    db.commit()
    loaded = db.scalar(
        select(Product)
        .options(selectinload(Product.variants).selectinload(ProductVariant.inventory))
        .where(Product.id == pid)
    )
    assert loaded is not None  # noqa: S101
    return _serialize_product(db, loaded)


@router.patch("/products/{product_id}", response_model=ProductResponse)
def merchant_update_product(
    product_id: str,
    payload: MerchantUpdateProductRequest,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> ProductResponse:
    from app.api.storefront import _serialize_product

    prod = db.scalar(
        select(Product)
        .options(selectinload(Product.variants).selectinload(ProductVariant.inventory))
        .where(Product.id == product_id, Product.merchant_id == principal.merchant_id)
    )
    if prod is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Product not found", status_code=404)
    if payload.title is not None:
        prod.title = payload.title.strip()
    if payload.category is not None:
        prod.category = payload.category.strip()
    if payload.description is not None:
        prod.description = payload.description.strip()
    if payload.is_featured is not None:
        prod.is_featured = payload.is_featured
    if payload.is_active is not None:
        prod.is_active = payload.is_active
    db.commit()
    db.refresh(prod)
    return _serialize_product(db, prod)


@router.post("/products/{product_id}/variants", status_code=status.HTTP_201_CREATED)
def merchant_add_variant(
    product_id: str,
    payload: MerchantCreateVariantRequest,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    prod = db.scalar(
        select(Product).where(
            Product.id == product_id, Product.merchant_id == principal.merchant_id
        )
    )
    if prod is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Product not found", status_code=404)
    vid = f"VAR-{uuid.uuid4().hex[:8].upper()}"
    var = ProductVariant(
        id=vid,
        product_id=prod.id,
        merchant_id=principal.merchant_id,
        sku=payload.sku.strip().upper(),
        title=payload.title.strip(),
        price_vnd=payload.price_vnd,
        compare_at_price_vnd=payload.compare_at_price_vnd,
        image_url=(prod.image_urls_json[0] if prod.image_urls_json else ""),
        is_active=True,
    )
    db.add(var)
    db.flush()
    db.add(
        Inventory(
            id=f"INV-{vid}",
            variant_id=vid,
            merchant_id=principal.merchant_id,
            on_hand_qty=payload.initial_stock,
            reserved_qty=0,
            low_stock_threshold=5,
            version=1,
        )
    )
    db.commit()
    return {"variant_id": vid, "sku": var.sku, "on_hand_qty": payload.initial_stock}


@router.patch("/inventory/{variant_id}")
def merchant_adjust_inventory(
    variant_id: str,
    payload: MerchantAdjustInventoryRequest,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    inv = db.scalar(
        select(Inventory)
        .where(Inventory.variant_id == variant_id, Inventory.merchant_id == principal.merchant_id)
        .with_for_update()
    )
    if inv is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Inventory record not found", status_code=404)
    if payload.on_hand_qty < inv.reserved_qty:
        raise CommerceError(
            ErrorCode.CONFLICT,
            (
                f"Tồn kho mới ({payload.on_hand_qty}) không được nhỏ hơn "
                f"số lượng đang giữ chỗ ({inv.reserved_qty})"
            ),
            status_code=409,
        )
    prev = inv.on_hand_qty
    inv.on_hand_qty = payload.on_hand_qty
    if payload.low_stock_threshold is not None:
        inv.low_stock_threshold = payload.low_stock_threshold
    inv.version += 1
    inv.updated_at = datetime.now(UTC)

    record_commerce_event(
        db,
        merchant_id=principal.merchant_id,
        commerce_session_id=principal.commerce_session_id,
        world_epoch=principal.world_epoch,
        event_type="INVENTORY_UPDATED",
        payload={
            "variant_id": variant_id,
            "previous_on_hand_qty": prev,
            "new_on_hand_qty": inv.on_hand_qty,
            "available_qty": inv.available_qty,
        },
    )
    db.commit()
    return {
        "variant_id": inv.variant_id,
        "on_hand_qty": inv.on_hand_qty,
        "reserved_qty": inv.reserved_qty,
        "available_qty": inv.available_qty,
        "low_stock_threshold": inv.low_stock_threshold,
    }


@router.get("/orders", response_model=list[OrderResponse])
def merchant_list_orders(
    status_filter: str | None = Query(default=None, alias="status"),
    reconciliation_status: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=120),
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> list[OrderResponse]:
    from app.api.storefront import _to_order_response

    stmt = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.payments))
        .where(Order.merchant_id == principal.merchant_id)
        .order_by(Order.created_at.desc())
        .limit(100)
    )
    if status_filter and status_filter.strip():
        stmt = stmt.where(Order.status == status_filter.strip().upper())
    if reconciliation_status and reconciliation_status.strip():
        stmt = stmt.where(Order.reconciliation_status == reconciliation_status.strip().upper())
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Order.order_code.ilike(pattern),
                Order.recipient_name.ilike(pattern),
                Order.recipient_phone.ilike(pattern),
            )
        )
    return [_to_order_response(o) for o in db.scalars(stmt).all()]


@router.post(
    "/orders/{order_id}/reconciliation/acknowledge",
    response_model=ReconciliationRecordResponse,
    summary="Record operator acknowledgment/investigation note without prematurely resolving",
)
def merchant_acknowledge_reconciliation(
    order_id: str,
    payload: ReconciliationResolveRequest,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> ReconciliationRecordResponse:
    order = db.scalar(
        select(Order)
        .options(selectinload(Order.payments))
        .where(Order.id == order_id, Order.merchant_id == principal.merchant_id)
        .with_for_update()
    )
    if order is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Order not found", status_code=404)
    pay = order.payments[0] if order.payments else None
    rec = ReconciliationRecord(
        id=f"RREC-{uuid.uuid4().hex[:10].upper()}",
        order_id=order.id,
        payment_id=pay.id if pay else None,
        merchant_id=order.merchant_id,
        operator_id=principal.operator_id,
        action_type="ACKNOWLEDGE",
        note=payload.resolution_note.strip(),
        evidence_json={
            "order_status": order.status,
            "payment_status": pay.status if pay else None,
            "reconciliation_status": order.reconciliation_status,
        },
        verified_postconditions=False,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return ReconciliationRecordResponse(
        id=rec.id,
        order_id=rec.order_id,
        payment_id=rec.payment_id,
        operator_id=rec.operator_id,
        action_type=rec.action_type,
        note=rec.note,
        evidence_json=dict(rec.evidence_json or {}),
        verified_postconditions=rec.verified_postconditions,
        created_at=rec.created_at,
    )


@router.get(
    "/orders/{order_id}/reconciliation/history",
    response_model=list[ReconciliationRecordResponse],
)
def merchant_reconciliation_history(
    order_id: str,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> list[ReconciliationRecordResponse]:
    rows = list(
        db.scalars(
            select(ReconciliationRecord)
            .where(
                ReconciliationRecord.order_id == order_id,
                ReconciliationRecord.merchant_id == principal.merchant_id,
            )
            .order_by(ReconciliationRecord.created_at.desc())
        ).all()
    )
    return [
        ReconciliationRecordResponse(
            id=r.id,
            order_id=r.order_id,
            payment_id=r.payment_id,
            operator_id=r.operator_id,
            action_type=r.action_type,
            note=r.note,
            evidence_json=dict(r.evidence_json or {}),
            verified_postconditions=r.verified_postconditions,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/orders/{order_id}/reconciliation/resolve", response_model=OrderResponse)
def merchant_resolve_reconciliation(
    order_id: str,
    payload: ReconciliationResolveRequest,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> OrderResponse:
    """Evidence-based reconciliation resolution (Section 5.2).

    Re-reads authoritative order, payment, and reservation states and refuses to mark RESOLVED
    if concrete financial postconditions remain inconsistent!
    """
    from app.api.storefront import _to_order_response

    order = db.scalar(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.payments))
        .where(Order.id == order_id, Order.merchant_id == principal.merchant_id)
        .with_for_update()
    )
    if order is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Order not found", status_code=404)

    pay = order.payments[0] if order.payments else None
    active_res_count = int(
        db.scalar(
            select(func.count(InventoryReservation.id)).where(
                InventoryReservation.order_id == order.id,
                InventoryReservation.status == ReservationStatus.ACTIVE.value,
            )
        )
        or 0
    )

    # Verify concrete financial postconditions before allowing RESOLVED
    inconsistencies: list[str] = []
    if pay is not None:
        if pay.status == PaymentStatus.UNKNOWN.value:
            inconsistencies.append(
                "Payment status is still UNKNOWN — verify via provider QueryDr before resolving."
            )
        if order.status == OrderStatus.CANCELLED.value and pay.status in (
            PaymentStatus.SUCCEEDED.value,
            PaymentStatus.PARTIALLY_REFUNDED.value,
        ):
            inconsistencies.append(
                "Order is CANCELLED while payment is still captured (not fully REFUNDED) — "
                "complete full refund before marking reconciliation resolved."
            )
        if order.status == OrderStatus.PENDING_PAYMENT.value and pay.status in (
            PaymentStatus.SUCCEEDED.value,
            PaymentStatus.REFUNDED.value,
        ):
            inconsistencies.append(
                f"Order is still PENDING_PAYMENT while payment is {pay.status} — "
                "synchronize or cancel order first."
            )
    if order.status == OrderStatus.CANCELLED.value and active_res_count > 0:
        inconsistencies.append(
            f"Cancelled order still has {active_res_count} ACTIVE inventory reservation(s)."
        )

    evidence = {
        "order_status": order.status,
        "payment_status": pay.status if pay else None,
        "payment_refunded_vnd": pay.refunded_amount_vnd if pay else 0,
        "active_reservations": active_res_count,
        "inconsistencies": inconsistencies,
    }

    if inconsistencies:
        db.add(
            ReconciliationRecord(
                id=f"RREC-{uuid.uuid4().hex[:10].upper()}",
                order_id=order.id,
                payment_id=pay.id if pay else None,
                merchant_id=order.merchant_id,
                operator_id=principal.operator_id,
                action_type="RESOLVE_REJECTED",
                note=payload.resolution_note.strip(),
                evidence_json=evidence,
                verified_postconditions=False,
            )
        )
        db.commit()
        raise CommerceError(
            ErrorCode.CONFLICT,
            "RECONCILIATION_POSTCONDITION_FAILED: " + " ".join(inconsistencies),
            status_code=409,
            details=evidence,
        )

    now = datetime.now(UTC)
    order.reconciliation_status = ReconciliationStatus.RESOLVED.value
    order.reconciliation_reason = payload.resolution_note.strip()
    order.updated_at = now
    for p in order.payments:
        p.reconciliation_required = False
        p.reconciliation_note = payload.resolution_note.strip()
        p.updated_at = now

    db.add(
        ReconciliationRecord(
            id=f"RREC-{uuid.uuid4().hex[:10].upper()}",
            order_id=order.id,
            payment_id=pay.id if pay else None,
            merchant_id=order.merchant_id,
            operator_id=principal.operator_id,
            action_type="RESOLVE",
            note=payload.resolution_note.strip(),
            evidence_json=evidence,
            verified_postconditions=True,
        )
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
            "reconciliation_status": ReconciliationStatus.RESOLVED.value,
            "resolution_note": payload.resolution_note.strip(),
            "verified_postconditions": True,
            "actor": f"merchant:{principal.operator_id}",
        },
    )
    db.commit()
    db.refresh(order)
    return _to_order_response(order)


@router.patch("/orders/{order_id}/status", response_model=OrderResponse)
def merchant_update_order_status(
    order_id: str,
    payload: MerchantUpdateOrderStatusRequest,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> OrderResponse:
    from app.api.storefront import _to_order_response

    order = db.scalar(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.payments))
        .where(Order.id == order_id, Order.merchant_id == principal.merchant_id)
        .with_for_update()
    )
    if order is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Order not found", status_code=404)

    curr = OrderStatus(order.status)
    try:
        target = OrderStatus(payload.status.strip().upper())
    except ValueError as err:
        raise CommerceError(
            ErrorCode.BAD_REQUEST, f"Invalid status '{payload.status}'", status_code=400
        ) from err

    # Section 5.1: Prevent bypassing payment/inventory confirmation & cancellation workflows!
    if target == OrderStatus.CONFIRMED:
        raise CommerceError(
            ErrorCode.CONFLICT,
            (
                "MANUAL_CONFIRMATION_BLOCKED: Không thể xác nhận thủ công đơn hàng. "
                "Đơn chỉ được CONFIRMED tự động sau khi thanh toán SUCCEEDED và đồng bộ kho."
            ),
            status_code=409,
        )
    if target == OrderStatus.CANCELLED:
        raise CommerceError(
            ErrorCode.CONFLICT,
            (
                "GENERIC_CANCEL_BLOCKED: Không thể hủy đơn qua endpoint cập nhật trạng thái chung "
                "(phải qua quy trình hủy/hoàn trả giữ chỗ chuyên biệt)."
            ),
            status_code=409,
        )

    if not can_transition_order(curr, target):
        raise CommerceError(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"Invalid order transition: {curr.value} -> {target.value}",
            status_code=409,
        )

    if target in (OrderStatus.PROCESSING, OrderStatus.SHIPPED, OrderStatus.DELIVERED):
        if order.reconciliation_status == ReconciliationStatus.RECONCILIATION_REQUIRED.value or any(
            p.reconciliation_required for p in order.payments
        ):
            raise CommerceError(
                ErrorCode.CONFLICT,
                (
                    "RECONCILIATION_REQUIRED_BLOCKS_FULFILLMENT: "
                    "Đơn hàng đang cần đối soát thanh toán, không thể đóng gói/giao hàng."
                ),
                status_code=409,
            )
        if order.payment_status not in (
            PaymentStatus.SUCCEEDED.value,
            PaymentStatus.PARTIALLY_REFUNDED.value,
        ):
            raise CommerceError(
                ErrorCode.CONFLICT,
                (
                    "PAYMENT_REQUIRED_BEFORE_FULFILLMENT: "
                    "Không thể giao hàng khi thanh toán chưa SUCCEEDED"
                ),
                status_code=409,
            )

    order.status = target.value
    order.version += 1
    order.updated_at = datetime.now(UTC)
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
            "new_status": target.value,
            "actor": f"merchant:{principal.operator_id}",
        },
    )
    db.commit()
    db.refresh(order)
    return _to_order_response(order)


@router.get("/payments", response_model=list[PaymentResponse])
def merchant_list_payments(
    provider: str | None = Query(default=None, max_length=32),
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> list[PaymentResponse]:
    from app.api.storefront import _to_payment_response

    stmt = (
        select(Payment)
        .where(Payment.merchant_id == principal.merchant_id)
        .order_by(Payment.created_at.desc())
        .limit(100)
    )
    if provider and provider.strip():
        stmt = stmt.where(Payment.provider == provider.strip().upper())
    rows = list(db.scalars(stmt).all())
    return [_to_payment_response(p) for p in rows]


@router.post("/payments/{payment_id}/querydr")
def merchant_querydr_payment(
    payment_id: str,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Authorized manual QueryDr reconciliation trigger for a merchant's payment."""
    payment = db.scalar(
        select(Payment).where(
            Payment.id == payment_id,
            Payment.merchant_id == principal.merchant_id,
        )
    )
    if payment is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Payment not found", status_code=404)

    adapter = get_payment_adapter(payment.provider)
    obs = adapter.query_payment_status(db, payment_id=payment.id)
    synced = sync_confirmed_payments_to_orders(db, limit=10)
    return {
        "payment_id": obs.payment_id,
        "provider": obs.provider,
        "provider_environment": obs.provider_environment,
        "status": obs.status,
        "provider_response_code": obs.provider_response_code,
        "provider_transaction_status": obs.provider_transaction_status,
        "signature_verified": obs.signature_verified,
        "reconciliation_required": obs.reconciliation_required,
        "orders_synced": synced,
    }


@router.get(
    "/payments/{payment_id}/query-history",
    response_model=list[PaymentQueryAttemptResponse],
)
def merchant_payment_query_history(
    payment_id: str,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> list[PaymentQueryAttemptResponse]:
    rows = list(
        db.scalars(
            select(PaymentQueryAttempt)
            .where(
                PaymentQueryAttempt.payment_id == payment_id,
                PaymentQueryAttempt.merchant_id == principal.merchant_id,
            )
            .order_by(PaymentQueryAttempt.created_at.desc())
            .limit(50)
        ).all()
    )
    return [
        PaymentQueryAttemptResponse(
            id=r.id,
            payment_id=r.payment_id,
            order_id=r.order_id,
            provider=r.provider,
            request_id=r.request_id,
            command=r.command,
            http_status=r.http_status,
            provider_response_code=r.provider_response_code,
            provider_transaction_status=r.provider_transaction_status,
            signature_verified=r.signature_verified,
            mapped_payment_status=r.mapped_payment_status,
            observation_provenance=r.observation_provenance,
            error_message=r.error_message,
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.post("/payments/sync-now")
def merchant_sync_payments_now(
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    _ = principal
    synced = sync_confirmed_payments_to_orders(db, limit=50)
    return {"synced_orders": synced}


@router.get("/refunds", response_model=list[RefundRequestResponse])
def merchant_list_refunds(
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> list[RefundRequestResponse]:
    rows = list(
        db.scalars(
            select(RefundRequest)
            .where(RefundRequest.merchant_id == principal.merchant_id)
            .order_by(RefundRequest.created_at.desc())
            .limit(100)
        ).all()
    )
    return [
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
        )
        for r in rows
    ]


@router.post("/refunds/{refund_request_id}/decision")
def merchant_decide_refund(
    refund_request_id: str,
    payload: MerchantRefundDecisionRequest,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    req = db.scalar(
        select(RefundRequest)
        .where(
            RefundRequest.id == refund_request_id,
            RefundRequest.merchant_id == principal.merchant_id,
        )
        .with_for_update()
    )
    if req is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Refund request not found", status_code=404)
    if req.status != RefundRequestStatus.REQUESTED.value:
        raise CommerceError(
            ErrorCode.CONFLICT,
            f"Refund request already decided (status={req.status})",
            status_code=409,
        )

    req.merchant_note = payload.merchant_note.strip()
    if payload.decision == "REJECT":
        req.status = RefundRequestStatus.REJECTED.value
        req.updated_at = datetime.now(UTC)
        db.commit()
        return {"refund_request_id": req.id, "status": req.status, "refund_transaction_id": None}

    pay = db.get(Payment, req.payment_id)
    if pay is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Payment not found", status_code=404)
    adapter = get_payment_adapter(pay.provider)
    txn = adapter.request_refund(
        db,
        refund_request=req,
        idempotency_key=f"IDEMP-REF-{req.id}",
        operator_id=principal.operator_id,
    )
    return {
        "refund_request_id": req.id,
        "status": req.status,
        "refund_transaction_id": txn.id,
        "provider_refund_ref": txn.provider_refund_ref,
        "amount_vnd": txn.amount_vnd,
    }


@router.get("/vouchers", response_model=list[VoucherResponse])
def merchant_list_vouchers(
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> list[VoucherResponse]:
    rows = list(
        db.scalars(
            select(Voucher)
            .where(Voucher.merchant_id == principal.merchant_id)
            .order_by(Voucher.created_at.desc())
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


@router.post("/vouchers", response_model=VoucherResponse, status_code=status.HTTP_201_CREATED)
def merchant_create_voucher(
    payload: MerchantCreateVoucherRequest,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> VoucherResponse:
    now = datetime.now(UTC)
    v = Voucher(
        id=f"VCH-{uuid.uuid4().hex[:8].upper()}",
        merchant_id=principal.merchant_id,
        code=payload.code.strip().upper(),
        title=payload.title.strip(),
        discount_type=payload.discount_type,
        discount_value=payload.discount_value,
        max_discount_vnd=payload.max_discount_vnd,
        min_order_vnd=payload.min_order_vnd,
        usage_limit=payload.usage_limit,
        used_count=0,
        per_customer_limit=payload.per_customer_limit,
        starts_at=now - timedelta(minutes=1),
        ends_at=now + timedelta(days=payload.duration_days),
        is_active=True,
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return VoucherResponse(
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


@router.patch("/live/{live_id}/products/{live_product_id}")
def merchant_update_live_product(
    live_id: str,
    live_product_id: str,
    payload: MerchantUpdateLiveProductRequest,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    live = db.scalar(
        select(LiveSession).where(
            LiveSession.id == live_id, LiveSession.merchant_id == principal.merchant_id
        )
    )
    if live is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "LiveSession not found", status_code=404)
    lp = db.scalar(
        select(LiveProduct).where(
            LiveProduct.id == live_product_id, LiveProduct.live_session_id == live.id
        )
    )
    if lp is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "LiveProduct not found", status_code=404)

    now = datetime.now(UTC)
    if payload.is_pinned is True:
        others = list(
            db.scalars(select(LiveProduct).where(LiveProduct.live_session_id == live.id)).all()
        )
        for o in others:
            o.is_pinned = o.id == lp.id
    elif payload.is_pinned is False:
        lp.is_pinned = False

    if payload.flash_sale_price_vnd is not None:
        lp.flash_sale_price_vnd = payload.flash_sale_price_vnd
    if payload.flash_sale_stock_limit is not None:
        lp.flash_sale_stock_limit = payload.flash_sale_stock_limit
    if payload.flash_sale_duration_minutes is not None:
        lp.flash_sale_starts_at = now
        lp.flash_sale_ends_at = now + timedelta(minutes=payload.flash_sale_duration_minutes)
        record_commerce_event(
            db,
            merchant_id=principal.merchant_id,
            commerce_session_id=principal.commerce_session_id,
            world_epoch=principal.world_epoch,
            event_type="FLASH_SALE_STARTED",
            payload={
                "live_session_id": live.id,
                "live_product_id": lp.id,
                "variant_id": lp.variant_id,
                "flash_sale_price_vnd": lp.flash_sale_price_vnd,
                "flash_sale_stock_limit": lp.flash_sale_stock_limit,
            },
        )
    db.commit()
    return {
        "live_product_id": lp.id,
        "is_pinned": lp.is_pinned,
        "flash_sale_price_vnd": lp.flash_sale_price_vnd,
        "flash_sale_stock_limit": lp.flash_sale_stock_limit,
    }


@router.get("/complaints", response_model=list[ComplaintResponse])
def merchant_list_complaints(
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> list[ComplaintResponse]:
    rows = list(
        db.scalars(
            select(Complaint)
            .where(Complaint.merchant_id == principal.merchant_id)
            .order_by(Complaint.created_at.desc())
            .limit(100)
        ).all()
    )
    return [
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
        )
        for c in rows
    ]


@router.patch("/complaints/{complaint_id}", response_model=ComplaintResponse)
def merchant_update_complaint(
    complaint_id: str,
    payload: MerchantUpdateComplaintRequest,
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> ComplaintResponse:
    comp = db.scalar(
        select(Complaint).where(
            Complaint.id == complaint_id, Complaint.merchant_id == principal.merchant_id
        )
    )
    if comp is None:
        raise CommerceError(ErrorCode.NOT_FOUND, "Complaint not found", status_code=404)
    comp.status = payload.status
    comp.merchant_response = payload.merchant_response.strip()
    comp.updated_at = datetime.now(UTC)
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


@router.get("/events", response_model=list[CommerceEventItemResponse])
def merchant_list_events(
    limit: int = Query(default=60, ge=1, le=200),
    principal: MerchantPrincipal = Depends(get_current_merchant),
    db: Session = Depends(get_db),
) -> list[CommerceEventItemResponse]:
    events = list(
        db.scalars(
            select(CommerceEvent)
            .where(CommerceEvent.merchant_id == principal.merchant_id)
            .order_by(CommerceEvent.occurred_at.desc())
            .limit(limit)
        ).all()
    )
    ev_ids = [e.id for e in events]
    outbox_map: dict[str, EventOutbox] = {}
    if ev_ids:
        for ob in db.scalars(
            select(EventOutbox).where(EventOutbox.commerce_event_id.in_(ev_ids))
        ).all():
            outbox_map[ob.commerce_event_id] = ob
    return [
        CommerceEventItemResponse(
            id=e.id,
            event_id=e.event_id,
            event_type=e.event_type,
            source=e.source,
            merchant_id=e.merchant_id,
            commerce_session_id=e.commerce_session_id,
            world_epoch=e.world_epoch,
            order_id=e.order_id,
            payment_id=e.payment_id,
            correlation_id=e.correlation_id,
            occurred_at=e.occurred_at,
            payload_json=dict(e.payload_json or {}),
            outbox_status=outbox_map[e.id].status if e.id in outbox_map else None,
            failure_category=outbox_map[e.id].failure_category if e.id in outbox_map else None,
            finresolve_mapped_type=(
                outbox_map[e.id].finresolve_mapped_type if e.id in outbox_map else None
            ),
        )
        for e in events
    ]
