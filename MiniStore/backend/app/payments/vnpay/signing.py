"""Official VNPAY API v2.1.0 cryptographic signing, encoding, and timezone utilities.

Protocol authority:
- Payment URL & IPN/ReturnURL (`pay`):
  - Exclude `vnp_SecureHash` and `vnp_SecureHashType`.
  - Sort remaining `vnp_*` parameters by ASCII key ascending.
  - Encode values with `urllib.parse.quote_plus(str(value))` (space -> `+`).
  - Join as `key=encoded_value&...` and compute `HMAC-SHA512(vnp_HashSecret, hash_data)`.
- QueryDr Request (`vnp_Command=querydr`):
  `vnp_RequestId|vnp_Version|vnp_Command|vnp_TmnCode|vnp_TxnRef|vnp_TransactionDate|vnp_CreateDate|vnp_IpAddr|vnp_OrderInfo`
- QueryDr Response:
  `vnp_ResponseId|vnp_Command|vnp_ResponseCode|vnp_Message|vnp_TmnCode|vnp_TxnRef|vnp_Amount|vnp_BankCode|vnp_PayDate|vnp_TransactionNo|vnp_TransactionType|vnp_TransactionStatus|vnp_OrderInfo|vnp_PromotionCode|vnp_PromotionAmount`
- Refund Request (`vnp_Command=refund`):
  `vnp_RequestId|vnp_Version|vnp_Command|vnp_TmnCode|vnp_TransactionType|vnp_TxnRef|vnp_Amount|vnp_TransactionNo|vnp_TransactionDate|vnp_CreateBy|vnp_CreateDate|vnp_IpAddr|vnp_OrderInfo`
- Refund Response:
  `vnp_ResponseId|vnp_Command|vnp_ResponseCode|vnp_Message|vnp_TmnCode|vnp_TxnRef|vnp_Amount|vnp_BankCode|vnp_PayDate|vnp_TransactionNo|vnp_TransactionType|vnp_TransactionStatus|vnp_OrderInfo`

Security invariants:
- Constant-time `hmac.compare_digest` for all signature checks.
- Integer VND -> VNPAY `vnp_Amount` (`amount_vnd * 100`) conversion happens ONLY here.
- Vietnam timezone GMT+7 (`Asia/Ho_Chi_Minh`) used for all `YYYYMMDDHHMMSS` timestamps.
- Never log `hash_secret` or raw signed URLs containing sensitive tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus

VIETNAM_TZ = timezone(timedelta(hours=7))


def format_vnpay_timestamp(dt: datetime) -> str:
    """Format a timezone-aware datetime as VNPAY `YYYYMMDDHHMMSS` in Vietnam timezone (GMT+7)."""
    return dt.astimezone(VIETNAM_TZ).strftime("%Y%m%d%H%M%S")


def vnd_to_vnp_amount(amount_vnd: int) -> str:
    """Convert internal integer VND amount to VNPAY `vnp_Amount` string (`amount_vnd * 100`)."""
    if not isinstance(amount_vnd, int) or amount_vnd <= 0:
        raise ValueError("amount_vnd must be a positive integer")
    return str(amount_vnd * 100)


def vnp_amount_to_vnd(raw_vnp_amount: str | int) -> int:
    """Parse VNPAY `vnp_Amount` (`amount_vnd * 100`) back to exact integer VND."""
    raw_str = str(raw_vnp_amount).strip()
    if not re.fullmatch(r"[0-9]{1,12}", raw_str):
        raise ValueError("Invalid vnp_Amount")
    val = int(raw_str)
    if val <= 0 or val % 100 != 0:
        raise ValueError(f"vnp_Amount must be a positive multiple of 100, got {val}")
    return val // 100


def _hmac_sha512_hex(secret: str, data: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()


def build_vnpay_hash_data(params: dict[str, Any]) -> str:
    """Build canonical sorted query string for VNPAY 2.1.0 payment URL / IPN / ReturnURL."""
    pairs: list[str] = []
    for key in sorted(params.keys()):
        if not key.startswith("vnp_") or key in {"vnp_SecureHash", "vnp_SecureHashType"}:
            continue
        val = params[key]
        if val is None or str(val) == "":
            continue
        encoded_key = quote_plus(str(key))
        encoded_val = quote_plus(str(val))
        pairs.append(f"{encoded_key}={encoded_val}")
    return "&".join(pairs)


def sign_vnpay_params(params: dict[str, Any], *, hash_secret: str) -> str:
    """Compute HMAC-SHA512 hex signature over sorted VNPAY `pay`/IPN/ReturnURL params."""
    hash_data = build_vnpay_hash_data(params)
    return _hmac_sha512_hex(hash_secret.strip(), hash_data)


def build_vnpay_payment_url(
    base_payment_url: str,
    params: dict[str, Any],
    *,
    hash_secret: str,
) -> tuple[str, str]:
    """Return `(signed_redirect_url, secure_hash)` following VNPAY 2.1.0 specification."""
    query = build_vnpay_hash_data(params)
    secure_hash = _hmac_sha512_hex(hash_secret.strip(), query)
    sep = "&" if "?" in base_payment_url else "?"
    return f"{base_payment_url}{sep}{query}&vnp_SecureHash={secure_hash}", secure_hash


def verify_vnpay_params_signature(params: dict[str, Any], *, hash_secret: str) -> bool:
    """Constant-time verification of `vnp_SecureHash` on VNPAY IPN or ReturnURL params."""
    received = str(params.get("vnp_SecureHash") or "").strip()
    if not _valid_hash(received) or not hash_secret.strip():
        return False
    expected = sign_vnpay_params(params, hash_secret=hash_secret)
    return hmac.compare_digest(expected.lower(), received.lower())


def _valid_hash(value: str) -> bool:
    return re.fullmatch(r"[0-9a-fA-F]{128}", value) is not None


def sign_querydr_request(payload: dict[str, Any], *, hash_secret: str) -> str:
    """Sign VNPAY 2.1.0 `querydr` request using documented pipe-delimited format."""
    fields = (
        "vnp_RequestId",
        "vnp_Version",
        "vnp_Command",
        "vnp_TmnCode",
        "vnp_TxnRef",
        "vnp_TransactionDate",
        "vnp_CreateDate",
        "vnp_IpAddr",
        "vnp_OrderInfo",
    )
    data = "|".join(str(payload.get(f, "") or "") for f in fields)
    return _hmac_sha512_hex(hash_secret.strip(), data)


def verify_querydr_response_signature(response: dict[str, Any], *, hash_secret: str) -> bool:
    """Verify VNPAY 2.1.0 `querydr` response signature using documented pipe-delimited format."""
    received = str(response.get("vnp_SecureHash") or "").strip()
    if not _valid_hash(received) or not hash_secret.strip():
        return False

    fields = (
        "vnp_ResponseId",
        "vnp_Command",
        "vnp_ResponseCode",
        "vnp_Message",
        "vnp_TmnCode",
        "vnp_TxnRef",
        "vnp_Amount",
        "vnp_BankCode",
        "vnp_PayDate",
        "vnp_TransactionNo",
        "vnp_TransactionType",
        "vnp_TransactionStatus",
        "vnp_OrderInfo",
        "vnp_PromotionCode",
        "vnp_PromotionAmount",
    )
    values = [response.get(field) for field in fields]
    if any(value is not None and type(value) not in (str, int) for value in values):
        return False
    data = "|".join("" if value is None else str(value) for value in values)
    return hmac.compare_digest(_hmac_sha512_hex(hash_secret.strip(), data), received.lower())


def sign_refund_request(payload: dict[str, Any], *, hash_secret: str) -> str:
    """Sign VNPAY 2.1.0 `refund` request using documented pipe-delimited format."""
    fields = (
        "vnp_RequestId",
        "vnp_Version",
        "vnp_Command",
        "vnp_TmnCode",
        "vnp_TransactionType",
        "vnp_TxnRef",
        "vnp_Amount",
        "vnp_TransactionNo",
        "vnp_TransactionDate",
        "vnp_CreateBy",
        "vnp_CreateDate",
        "vnp_IpAddr",
        "vnp_OrderInfo",
    )
    data = "|".join(str(payload.get(f, "") or "") for f in fields)
    return _hmac_sha512_hex(hash_secret.strip(), data)


def verify_refund_response_signature(response: dict[str, Any], *, hash_secret: str) -> bool:
    """Verify VNPAY 2.1.0 `refund` response signature using documented pipe-delimited format."""
    received = str(response.get("vnp_SecureHash") or "").strip()
    if not _valid_hash(received) or not hash_secret.strip():
        return False
    fields = (
        "vnp_ResponseId",
        "vnp_Command",
        "vnp_ResponseCode",
        "vnp_Message",
        "vnp_TmnCode",
        "vnp_TxnRef",
        "vnp_Amount",
        "vnp_BankCode",
        "vnp_PayDate",
        "vnp_TransactionNo",
        "vnp_TransactionType",
        "vnp_TransactionStatus",
        "vnp_OrderInfo",
    )
    data = "|".join(str(response.get(f, "") or "") for f in fields)
    expected = _hmac_sha512_hex(hash_secret.strip(), data)
    return hmac.compare_digest(expected.lower(), received.lower())
