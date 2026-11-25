"""Hardened Customer, Merchant, Service, and Mock Provider auth boundaries for FinShop LIVE.

Security Invariants (Prompt 10.1):
- Customers authenticate via cryptographically random, expiring, revocable `X-Customer-Token`
  (or HttpOnly cookie `finshop_customer_token`). Bootstrap NEVER leaks customer session tokens.
- Merchants authenticate via username/password verified against salted `scrypt` hash in
  `merchant_accounts`, issuing an expiring, revocable `MerchantSession` (`token_hash` stored
  in PostgreSQL). Anonymous callers cannot mint merchant sessions.
- Service connectors authenticate via `X-Commerce-Service-Key` validated against scoped
  `service_credentials` bound to `(merchant_id, commerce_session_id, allowed_scopes)` AND
  require explicit `X-World-Epoch` matching the active `CommerceSession.world_epoch`.
- Direct `LOCAL_MOCK` provider callback endpoint requires `X-Mock-Provider-Secret` matching
  server-side `COMMERCE_MOCK_PROVIDER_SECRET` and `COMMERCE_ENABLE_MOCK_PAYMENT_TEST_API=true`.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import CommerceError, ErrorCode
from app.db.session import get_db
from app.models.entities import (
    CommerceSession,
    Customer,
    Merchant,
    MerchantAccount,
    MerchantSession,
    ServiceCredential,
)

CUSTOMER_TOKEN_HEADER = "X-Customer-Token"  # noqa: S105
CUSTOMER_COOKIE_NAME = "finshop_customer_token"  # noqa: S105
MERCHANT_TOKEN_HEADER = "X-Merchant-Token"  # noqa: S105
MERCHANT_COOKIE_NAME = "finshop_merchant_token"  # noqa: S105
SERVICE_KEY_HEADER = "X-Commerce-Service-Key"  # noqa: S105
MOCK_PROVIDER_SECRET_HEADER = "X-Mock-Provider-Secret"  # noqa: S105
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
    session_id: str
    merchant_account_id: str
    merchant_id: str
    commerce_session_id: str
    world_epoch: int
    role: str
    operator_id: str


@dataclass(frozen=True)
class ServicePrincipal:
    credential_id: str
    service_name: str
    merchant_id: str
    commerce_session_id: str
    world_epoch: int
    allowed_scopes: frozenset[str]


def hash_password(password: str, *, salt_hex: str | None = None) -> str:
    """Hash a password using stdlib `hashlib.scrypt` with a 16-byte random salt."""
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time verification of a `scrypt` password hash."""
    parts = stored_hash.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n_val = int(parts[1])
        r_val = int(parts[2])
        p_val = int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected = bytes.fromhex(parts[5])
    except ValueError:
        return False
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n_val, r=r_val, p=p_val, dklen=len(expected)
    )
    return hmac.compare_digest(dk, expected)


def hash_token(raw_token: str) -> str:
    """Derive a keyed SHA-256 hash for storing opaque session/service tokens safely."""
    secret = get_settings().auth_secret.get_secret_value().encode("utf-8")
    return hmac.new(secret, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_customer_token() -> str:
    return f"ctk_{secrets.token_urlsafe(32)}"


def generate_merchant_token() -> str:
    return f"mch_{secrets.token_urlsafe(32)}"


def create_merchant_session(
    db: Session,
    *,
    account: MerchantAccount,
    commerce_session: CommerceSession,
) -> tuple[MerchantSession, str]:
    """Create a revocable, expiring MerchantSession and return `(session, raw_token)`."""
    now = datetime.now(UTC)
    ttl = get_settings().merchant_session_ttl_seconds
    raw_token = generate_merchant_token()
    ms = MerchantSession(
        id=f"MSESS-{uuid.uuid4().hex[:12].upper()}",
        merchant_account_id=account.id,
        merchant_id=account.merchant_id,
        commerce_session_id=commerce_session.id,
        token_hash=hash_token(raw_token),
        role=account.role,
        operator_id=account.username,
        expires_at=now + timedelta(seconds=ttl),
        revoked_at=None,
        created_at=now,
    )
    db.add(ms)
    db.flush()
    return ms, raw_token


def revoke_merchant_session(db: Session, *, raw_token: str) -> bool:
    tok_hash = hash_token(raw_token)
    ms = db.scalar(select(MerchantSession).where(MerchantSession.token_hash == tok_hash))
    if ms is None or ms.revoked_at is not None:
        return False
    ms.revoked_at = datetime.now(UTC)
    db.commit()
    return True


def _extract_customer_token(request: Request) -> str:
    hdr = request.headers.get(CUSTOMER_TOKEN_HEADER, "").strip()
    if hdr:
        return hdr
    return request.cookies.get(CUSTOMER_COOKIE_NAME, "").strip()


def _extract_merchant_token(request: Request) -> str:
    hdr = request.headers.get(MERCHANT_TOKEN_HEADER, "").strip()
    if hdr:
        return hdr
    return request.cookies.get(MERCHANT_COOKIE_NAME, "").strip()


def get_current_customer(
    request: Request,
    db: Session = Depends(get_db),
) -> CustomerPrincipal:
    token = _extract_customer_token(request)
    if not token:
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            f"Missing required customer session token ({CUSTOMER_TOKEN_HEADER})",
            status_code=401,
        )
    customer = db.scalar(select(Customer).where(Customer.session_token == token))
    if customer is None:
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            "Invalid or unknown customer session token",
            status_code=401,
        )
    now = datetime.now(UTC)
    if customer.revoked_at is not None:
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            "Customer session has been revoked",
            status_code=401,
        )
    if customer.token_expires_at is not None and customer.token_expires_at <= now:
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            "Customer session token has expired",
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
    token = _extract_customer_token(request)
    if not token:
        return None
    return get_current_customer(request, db)


def get_current_merchant(
    request: Request,
    db: Session = Depends(get_db),
) -> MerchantPrincipal:
    raw = _extract_merchant_token(request)
    if not raw or not raw.startswith("mch_"):
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            f"Authenticated merchant session ({MERCHANT_TOKEN_HEADER}) is required",
            status_code=401,
        )
    tok_hash = hash_token(raw)
    ms = db.scalar(select(MerchantSession).where(MerchantSession.token_hash == tok_hash))
    if ms is None:
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            "Invalid or unknown merchant session",
            status_code=401,
        )
    now = datetime.now(UTC)
    if ms.revoked_at is not None:
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            "Merchant session has been revoked",
            status_code=401,
        )
    if ms.expires_at <= now:
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            "Merchant session has expired; please sign in again",
            status_code=401,
        )

    account = db.get(MerchantAccount, ms.merchant_account_id)
    merchant = db.get(Merchant, ms.merchant_id)
    csess = db.get(CommerceSession, ms.commerce_session_id)
    if (
        account is None
        or not account.is_active
        or merchant is None
        or not merchant.is_active
        or csess is None
        or not csess.is_active
    ):
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "Merchant account or commerce session is inactive",
            status_code=403,
        )
    if csess.merchant_id != merchant.id or account.merchant_id != merchant.id:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "Merchant/session ownership mismatch",
            status_code=403,
        )
    return MerchantPrincipal(
        session_id=ms.id,
        merchant_account_id=account.id,
        merchant_id=merchant.id,
        commerce_session_id=csess.id,
        world_epoch=csess.world_epoch,
        role=ms.role,
        operator_id=ms.operator_id,
    )


def get_service_principal(
    request: Request,
    db: Session = Depends(get_db),
) -> ServicePrincipal:
    """Authenticate service-to-service read connectors with per-credential scope binding."""
    provided_key = request.headers.get(SERVICE_KEY_HEADER, "").strip()
    if not provided_key:
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            f"Missing required service header: {SERVICE_KEY_HEADER}",
            status_code=401,
        )

    merchant_id = request.headers.get(MERCHANT_ID_HEADER, "").strip()
    session_id = request.headers.get(SESSION_ID_HEADER, "").strip()
    world_epoch_raw = request.headers.get(WORLD_EPOCH_HEADER, "").strip()

    if not merchant_id or not session_id or not world_epoch_raw:
        raise CommerceError(
            ErrorCode.BAD_REQUEST,
            (
                f"Headers {MERCHANT_ID_HEADER}, {SESSION_ID_HEADER}, and "
                f"{WORLD_EPOCH_HEADER} are all required"
            ),
            status_code=400,
        )

    try:
        req_epoch = int(world_epoch_raw)
    except ValueError as err:
        raise CommerceError(
            ErrorCode.BAD_REQUEST, "X-World-Epoch must be an integer", status_code=400
        ) from err

    key_hash = hash_token(provided_key)
    cred = db.scalar(select(ServiceCredential).where(ServiceCredential.key_hash == key_hash))
    if cred is None:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "Invalid or unknown commerce service connector credentials",
            status_code=403,
        )

    now = datetime.now(UTC)
    if not cred.is_active or cred.revoked_at is not None:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "Commerce service credential is revoked or inactive",
            status_code=403,
        )
    if cred.expires_at is not None and cred.expires_at <= now:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "Commerce service credential has expired",
            status_code=403,
        )

    # Strictly enforce credential-bound merchant & session scope (no arbitrary impersonation)
    if cred.merchant_id != merchant_id or cred.commerce_session_id != session_id:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            (
                "CREDENTIAL_SCOPE_MISMATCH: service credential is not authorized for "
                "requested merchant_id / commerce_session_id"
            ),
            status_code=403,
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
    if req_epoch != csess.world_epoch:
        raise CommerceError(
            ErrorCode.CONFLICT,
            (f"STALE_WORLD_EPOCH: requested world_epoch={req_epoch} != active {csess.world_epoch}"),
            status_code=409,
        )

    return ServicePrincipal(
        credential_id=cred.id,
        service_name=cred.service_name,
        merchant_id=merchant_id,
        commerce_session_id=csess.id,
        world_epoch=csess.world_epoch,
        allowed_scopes=frozenset(cred.allowed_scopes_json or []),
    )


def verify_mock_provider_secret(request: Request) -> None:
    """Protect direct LOCAL_MOCK provider callback endpoint from anonymous/browser callers."""
    settings = get_settings()
    if settings.source_mode != "MOCK" or not settings.enable_mock_payment_test_api:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "LOCAL_MOCK payment provider callback API is disabled",
            status_code=403,
        )
    provided = request.headers.get(MOCK_PROVIDER_SECRET_HEADER, "").strip()
    expected = settings.mock_provider_secret.get_secret_value().strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            f"Valid server-side {MOCK_PROVIDER_SECRET_HEADER} is required",
            status_code=403,
        )
