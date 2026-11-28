"""Execute only the inspected QueryDR function from VNPAY's Python sample."""

from __future__ import annotations

import argparse
import ast
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from querydr_reference import ENDPOINT, REQUEST_FIELDS, TZ, canonical, digest, submit

SOURCE_SHA256 = "7074edf366b1316e12bfac021242bd41efcb74c67e8c31782a941f4eb694fb47"


def sample_query(
    raw_source: bytes,
    *,
    merchant: str,
    key: str,
    txn_ref: str,
    pay_date: str,
    live: bool = False,
    fixed_time: datetime | None = None,
    fixed_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    if hashlib.sha256(raw_source).hexdigest() != SOURCE_SHA256:
        raise ValueError("Re-inspect the official sample before adopting a new revision")
    if not re.fullmatch(r"[A-Za-z0-9]{8}", merchant) or not key or key != key.strip():
        raise ValueError("Invalid credential shape")
    now = fixed_time or datetime.now(TZ)
    if not re.fullmatch(r"[A-Za-z0-9]{1,100}", txn_ref) or not re.fullmatch(r"[0-9]{14}", pay_date):
        raise ValueError("Invalid original PAY identity")
    if datetime.strptime(pay_date, "%Y%m%d%H%M%S").replace(tzinfo=TZ) > now:
        raise ValueError("Original PAY timestamp is in the future")
    request_id = fixed_id or str(secrets.randbelow(9 * 10**11) + 10**11)
    result: dict[str, Any] = {}
    preimage = b""

    def capture(url: str, headers: dict[str, str], data: str) -> SimpleNamespace:
        nonlocal preimage
        if url != ENDPOINT or headers != {"Content-Type": "application/json"} or result:
            raise ValueError("Unexpected sample request")
        raw_body = data.encode("utf-8")
        body = json.loads(raw_body)
        preimage = canonical(body, REQUEST_FIELDS)
        verified = hmac.compare_digest(digest(key, preimage), body["vnp_SecureHash"])
        result.update(
            style="official-python-query",
            endpoint=url,
            live=live,
            request_id=request_id,
            gmt7=now.isoformat(),
            original_pay_date=pay_date,
            ip_class="loopback",
            fields={k: type(v).__name__ for k, v in body.items()},
            canonical_sha256=hashlib.sha256(preimage).hexdigest(),
            local_signature_verified=verified,
        )
        if live and verified:
            result.update(submit(raw_body, key, result))
        return SimpleNamespace(status_code=200, text="{}")

    tree = ast.parse(raw_source)
    query = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "query"
    )
    scope = {
        "settings": SimpleNamespace(
            VNPAY_API_URL=ENDPOINT,
            VNPAY_HASH_SECRET_KEY=key,
            VNPAY_TMN_CODE=merchant,
        ),
        "datetime": SimpleNamespace(now=lambda: now),
        "n_str": request_id,
        "get_client_ip": lambda _request: "127.0.0.1",
        "hmac": hmac,
        "hashlib": hashlib,
        "json": json,
        "requests": SimpleNamespace(post=capture),
        "render": lambda *_args: None,
    }
    # Compile only the inspected function from the pinned official source.
    exec(  # noqa: S102
        compile(ast.Module(body=[query], type_ignores=[]), "<VNPAY QueryDR sample>", "exec"), scope
    )
    scope["query"](
        SimpleNamespace(method="POST", POST={"order_id": txn_ref, "trans_date": pay_date})
    )
    return result, preimage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample_views", type=Path)
    parser.add_argument("--live-query", action="store_true")
    args = parser.parse_args()
    identity = json.load(sys.stdin)
    result, _ = sample_query(
        args.sample_views.read_bytes(),
        merchant=os.environ["COMMERCE_VNPAY_TMN_CODE"],
        key=os.environ["COMMERCE_VNPAY_HASH_SECRET"],
        txn_ref=identity["txn_ref"],
        pay_date=identity["pay_date"],
        live=args.live_query,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
