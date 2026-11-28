"""Read-only VNPAY PAY 2.1.0 reference client. Dry-run by default."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import ssl
import sys
from datetime import UTC, datetime, timedelta, timezone
from time import monotonic
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

ENDPOINT = "https://sandbox.vnpayment.vn/merchant_webapi/api/transaction"
TZ = timezone(timedelta(hours=7))
REQUEST_FIELDS = (
    "RequestId",
    "Version",
    "Command",
    "TmnCode",
    "TxnRef",
    "TransactionDate",
    "CreateDate",
    "IpAddr",
    "OrderInfo",
)
RESPONSE_FIELDS = (
    "ResponseId",
    "Command",
    "ResponseCode",
    "Message",
    "TmnCode",
    "TxnRef",
    "Amount",
    "BankCode",
    "PayDate",
    "TransactionNo",
    "TransactionType",
    "TransactionStatus",
    "OrderInfo",
    "PromotionCode",
    "PromotionAmount",
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def canonical(body: dict[str, Any], fields: tuple[str, ...]) -> bytes:
    values = [body.get("vnp_" + field) for field in fields]
    if any(value is not None and type(value) not in (str, int) for value in values):
        raise ValueError("Unexpected canonical field type")
    return "|".join("" if value is None else str(value) for value in values).encode("utf-8")


def digest(key: str, data: bytes) -> str:
    return hmac.new(key.encode("utf-8"), data, hashlib.sha512).hexdigest()


def reference(
    *,
    merchant: str,
    key: str,
    txn_ref: str,
    pay_date: str,
    ip: str,
    live: bool = False,
    style: str = "numeric",
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9]{8}", merchant) or not key or key != key.strip():
        raise ValueError("Invalid credential shape")
    if not re.fullmatch(r"[A-Za-z0-9]{1,100}", txn_ref) or not re.fullmatch(r"[0-9]{14}", pay_date):
        raise ValueError("Invalid original PAY identity")
    now = datetime.now(TZ)
    if datetime.strptime(pay_date, "%Y%m%d%H%M%S").replace(tzinfo=TZ) > now:
        raise ValueError("Original PAY timestamp is in the future")
    request_id = str(secrets.randbelow(9 * 10**11) + 10**11)
    if style == "php-sample":
        request_id = str(secrets.randbelow(10_000) + 1)
    body: dict[str, str | int] = {
        "vnp_RequestId": int(request_id) if style == "php-sample" else request_id,
        "vnp_TmnCode": merchant,
        "vnp_Command": "querydr",
        "vnp_TxnRef": txn_ref,
        "vnp_OrderInfo": {
            "python-sample": "kiem tra gd",
            "php-sample": "Query transaction",
        }.get(style, f"Truy van giao dich {txn_ref}"),
        "vnp_TransactionDate": pay_date,
        "vnp_CreateDate": now.strftime("%Y%m%d%H%M%S"),
        "vnp_IpAddr": str(ipaddress.ip_address(ip)),
        "vnp_Version": "2.1.0",
    }
    preimage = canonical(body, REQUEST_FIELDS)
    body["vnp_SecureHash"] = digest(key, preimage)
    data = json.dumps(body).encode("utf-8")
    parsed = json.loads(data)
    independent = b"|".join(str(parsed["vnp_" + field]).encode() for field in REQUEST_FIELDS)
    local_check = independent == preimage and hmac.compare_digest(
        digest(key, independent), parsed["vnp_SecureHash"]
    )
    result: dict[str, Any] = {
        "style": style,
        "live": live,
        "endpoint": ENDPOINT,
        "request_id": request_id,
        "utc": now.astimezone(UTC).isoformat(),
        "gmt7": now.isoformat(),
        "fields": {k: type(v).__name__ for k, v in parsed.items()},
        "canonical_sha256": hashlib.sha256(preimage).hexdigest(),
        "local_signature_verified": local_check,
        "original_pay_date": pay_date,
        "ip_class": "loopback" if ipaddress.ip_address(ip).is_loopback else "non-loopback",
    }
    if not live or not local_check:
        return result
    return submit(data, key, result)


def submit(data: bytes, key: str, result: dict[str, Any]) -> dict[str, Any]:
    body = json.loads(data)
    signature = body.get("vnp_SecureHash")
    if (
        not isinstance(signature, str)
        or not re.fullmatch(r"[0-9a-f]{128}", signature)
        or body.get("vnp_Command") != "querydr"
        or not hmac.compare_digest(digest(key, canonical(body, REQUEST_FIELDS)), signature)
    ):
        raise ValueError("Only locally verified QueryDR requests may be sent")
    request = Request(ENDPOINT, data, {"Content-Type": "application/json"}, method="POST")
    opener = build_opener(
        ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()), NoRedirect()
    )
    started = monotonic()
    try:
        response = opener.open(request, timeout=12)
    except HTTPError as error:
        response = error
    with response:
        result.update(http_status=response.status, content_type=response.headers.get_content_type())
        raw = response.read(65537)
    result["latency_ms"] = round((monotonic() - started) * 1000, 1)
    if len(raw) > 65536:
        raise ValueError("QueryDR response exceeded diagnostic limit")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        return {**result, "response_json": False}
    if not isinstance(payload, dict):
        return {**result, "response_json": False}
    received = payload.get("vnp_SecureHash")
    verified = False
    if isinstance(received, str) and re.fullmatch(r"[0-9A-Fa-f]{128}", received):
        verified = hmac.compare_digest(
            digest(key, canonical(payload, RESPONSE_FIELDS)), received.lower()
        )
    result.update(
        fields_received=sorted(
            set(payload).intersection({"vnp_" + k for k in RESPONSE_FIELDS} | {"vnp_SecureHash"})
        ),
        additional_fields_count=len(
            set(payload) - {"vnp_" + k for k in RESPONSE_FIELDS} - {"vnp_SecureHash"}
        ),
        response_code=code_only(payload.get("vnp_ResponseCode")),
        transaction_status=code_only(payload.get("vnp_TransactionStatus")),
        signature_state="VALID" if verified else "MISSING" if received is None else "INVALID",
        provider_message="Unexpected error"
        if payload.get("vnp_Message") == "Unexpected error"
        else None,
    )
    return result


def code_only(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9]{2}", value) else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-query", action="store_true")
    parser.add_argument(
        "--style", choices=("numeric", "python-sample", "php-sample"), default="numeric"
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--ip", default="127.0.0.1")
    args = parser.parse_args()
    if args.self_test:
        if args.live_query:
            parser.error("--self-test is offline")
        values = (
            "QDR0001",
            "2.1.0",
            "querydr",
            "DEMO0001",
            "FS001",
            "20261129103000",
            "20261129103200",
            "127.0.0.1",
            "Truy van giao dich FS001",
        )
        expected = (
            "89636d07a03f4d020932575410979170723814bf8f802d61b85b12dd2c1a313158"
            "47b821689c03e4fa5c68f0cb67fcd57bc4b330f981ab7641dbb9a7a77789e8"
        )
        fields = dict(zip(("vnp_" + name for name in REQUEST_FIELDS), values, strict=True))
        if (
            digest("VNPAY-RESEARCH-FAKE-KEY-DO-NOT-USE", canonical(fields, REQUEST_FIELDS))
            != expected
        ):
            sys.exit("QueryDR synthetic signing vector failed")
        print("QueryDR synthetic signing vector PASS")
        return
    identity = json.load(sys.stdin)
    result = reference(
        merchant=os.environ["COMMERCE_VNPAY_TMN_CODE"],
        key=os.environ["COMMERCE_VNPAY_HASH_SECRET"],
        txn_ref=identity["txn_ref"],
        pay_date=identity["pay_date"],
        ip=args.ip,
        live=args.live_query,
        style=args.style,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
