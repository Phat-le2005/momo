"""Separate Customer, Merchant, and Service authentication boundaries for FinShop LIVE.

Security Invariants:
- Customers authenticate via opaque session-scoped `X-Customer-Token`; cross-customer access is
  strictly rejected with 403/404.
- Merchants authenticate via server-signed HMAC `X-Merchant-Token` bound to `(merchant_id,
  commerce_session_id, role)`; browser guests cannot self-assert merchant privileges.
- External service connectors (e.g. future FinResolve read connectors) authenticate via
  `X-Commerce-Service-Key` (never exposed to browser code) plus explicit merchant/session/world
  scope validation.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import CommerceError, ErrorCode
from app.db.session import get_db
from app.models.entities import CommerceSession, Customer, Merchant

CUSTOMER_TOKEN_HEADER = "X-Customer-Token"  # noqa: S105
MERCHANT_TOKEN_HEADER = "X-Merchant-Token"  # noqa: S105
SERVICE_KEY_HEADER = "X-Commerce-Service-Key"  # noqa: S105
MERCHANT_ID_HEADER = "X-Merchant-Id"
SESSION_ID_HEADER = "X-Commerce-Session-Id"
WORLD_EPOCH_HEADER = "X-World-Epoch"


@dataclass(frozen=True)
class CustomerPrincipal:
    customer: Customer
    session: CommerceSession
    merchant_id: str


@dataclass(frozen=True)
class MerchantPrincipal:
    merchant_id: str
    commerce_session_id: str
    world_epoch: int
    role: str
    operator_id: str


@dataclass(frozen=True)
class ServicePrincipal:
    service_name: str
    merchant_id: str
    commerce_session_id: str
    world_epoch: int


def _sign_merchant_token(
    *, merchant_id: str, commerce_session_id: str, role: str, operator_id: str
) -> str:
    secret = get_settings().auth_secret.get_secret_value().encode("utf-8")
    msg = f"mch_v1|{merchant_id}|{commerce_session_id}|{role}|{operator_id}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def issue_merchant_token(
    *,
    merchant_id: str,
    commerce_session_id: str,
    role: str = "MERCHANT_OWNER",
    operator_id: str = "merchant_demo_01",
) -> str:
    norm_role = role.strip().upper()
    if norm_role not in {"MERCHANT_OWNER", "MERCHANT_OPS"}:
        raise CommerceError(ErrorCode.FORBIDDEN, "Invalid merchant role", status_code=403)
    sig = _sign_merchant_token(
        merchant_id=merchant_id,
        commerce_session_id=commerce_session_id,
        role=norm_role,
        operator_id=operator_id,
    )
    return f"mch_{merchant_id}.{commerce_session_id}.{norm_role}.{operator_id}.{sig}"


def get_current_customer(
    request: Request,
    db: Session = Depends(get_db),
) -> CustomerPrincipal:
    token = request.headers.get(CUSTOMER_TOKEN_HEADER, "").strip()
    if not token:
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            f"Missing required customer header: {CUSTOMER_TOKEN_HEADER}",
            status_code=401,
        )
    customer = db.scalar(select(Customer).where(Customer.session_token == token))
    if customer is None:
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            "Invalid or unknown customer session token",
            status_code=401,
        )
    csess = db.get(CommerceSession, customer.commerce_session_id)
    if csess is None or not csess.is_active:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "Commerce session is inactive or expired",
            status_code=403,
        )
    return CustomerPrincipal(
        customer=customer,
        session=csess,
        merchant_id=customer.merchant_id,
    )


def get_optional_customer(
    request: Request,
    db: Session = Depends(get_db),
) -> CustomerPrincipal | None:
    token = request.headers.get(CUSTOMER_TOKEN_HEADER, "").strip()
    if not token:
        return None
    return get_current_customer(request, db)


def get_current_merchant(
    request: Request,
    db: Session = Depends(get_db),
) -> MerchantPrincipal:
    raw = request.headers.get(MERCHANT_TOKEN_HEADER, "").strip()
    if not raw or not raw.startswith("mch_"):
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            f"Valid server-signed {MERCHANT_TOKEN_HEADER} is required for merchant operations",
            status_code=403,
        )
    parts = raw[4:].split(".")
    if len(parts) != 5:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "Malformed merchant authorization token",
            status_code=403,
        )
    merchant_id, session_id, role, operator_id, sig = parts
    expected = _sign_merchant_token(
        merchant_id=merchant_id,
        commerce_session_id=session_id,
        role=role,
        operator_id=operator_id,
    )
    if not hmac.compare_digest(sig, expected):
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "Merchant token signature mismatch",
            status_code=403,
        )
    merchant = db.get(Merchant, merchant_id)
    csess = db.get(CommerceSession, session_id)
    if merchant is None or not merchant.is_active or csess is None or not csess.is_active:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "Merchant or commerce session is inactive",
            status_code=403,
        )
    if csess.merchant_id != merchant.id:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "Merchant/session ownership mismatch",
            status_code=403,
        )
    return MerchantPrincipal(
        merchant_id=merchant.id,
        commerce_session_id=csess.id,
        world_epoch=csess.world_epoch,
        role=role,
        operator_id=operator_id,
    )


def get_service_principal(
    request: Request,
    db: Session = Depends(get_db),
) -> ServicePrincipal:
    """Authenticate service-to-service read connectors with strict scope validation."""
    provided_key = request.headers.get(SERVICE_KEY_HEADER, "").strip()
    expected_key = get_settings().service_api_key.get_secret_value().strip()
    if not provided_key or not hmac.compare_digest(provided_key, expected_key):
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "Invalid or missing commerce service connector credentials",
            status_code=403,
        )

    merchant_id = request.headers.get(MERCHANT_ID_HEADER, "").strip()
    session_id = request.headers.get(SESSION_ID_HEADER, "").strip()
    world_epoch_raw = request.headers.get(WORLD_EPOCH_HEADER, "").strip()

    if not merchant_id or not session_id:
        raise CommerceError(
            ErrorCode.BAD_REQUEST,
            f"Headers {MERCHANT_ID_HEADER} and {SESSION_ID_HEADER} are required",
            status_code=400,
        )

    csess = db.get(CommerceSession, session_id)
    if csess is None or not csess.is_active:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "Target commerce_session_id is inactive or unknown",
            status_code=403,
        )
    if csess.merchant_id != merchant_id:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "CROSS_MERCHANT_ACCESS_BLOCKED: session does not belong to requested merchant_id",
            status_code=403,
        )
    if world_epoch_raw:
        try:
            req_epoch = int(world_epoch_raw)
        except ValueError as err:
            raise CommerceError(
                ErrorCode.BAD_REQUEST, "X-World-Epoch must be an integer", status_code=400
            ) from err
        if req_epoch != csess.world_epoch:
            raise CommerceError(
                ErrorCode.CONFLICT,
                (
                    f"STALE_WORLD_EPOCH: requested world_epoch={req_epoch} "
                    f"!= active {csess.world_epoch}"
                ),
                status_code=409,
            )

    return ServicePrincipal(
        service_name="FINRESOLVE_READ_CONNECTOR",
        merchant_id=merchant_id,
        commerce_session_id=csess.id,
        world_epoch=csess.world_epoch,
    )
