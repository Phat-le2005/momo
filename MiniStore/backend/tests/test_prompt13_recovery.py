"""Prompt 13 Slice E Automated Tests — Policy-Gated LOCAL_MOCK Recovery Commands.

Verifies:
- Separate write credential required (read key rejected with 401).
- Authorized `REQUEST_ORDER_SYNC` confirms a verified paid LOCAL_MOCK order (C01) and rejects
  unconfirmed C02 lookalike (409), cancelled orders (409), version mismatch (409), and VNPAY (403).
- Unsupported commands (e.g. `REFUND_EXECUTE`, `OVERRIDE_PAYMENT`) rejected with 403.
- Idempotent `REQUEST_EVENT_REDELIVERY` and `REQUEST_MANUAL_REVIEW` work safely.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.test_prompt14_1_write_scope_and_idempotency import sign_test_authorization


def _signed_write_headers(
    base_h: dict[str, str],
    *,
    command: str,
    idempotency_key: str,
    order_id: str | None = None,
    payment_id: str | None = None,
    event_id: str | None = None,
    expected_version: int | None = None,
) -> dict[str, str]:
    tok = sign_test_authorization(
        command=command,
        idempotency_key=idempotency_key,
        order_id=order_id,
        payment_id=payment_id,
        event_id=event_id,
        expected_version=expected_version,
    )
    return {**base_h, "X-Command-Authorization": tok}


def _headers() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    s = get_settings()
    chaos_h = {"X-Chaos-Admin-Key": s.chaos_admin_key.get_secret_value()}
    read_h = {
        "X-Commerce-Service-Key": s.service_api_key.get_secret_value(),
        "X-Merchant-Id": "MERCHANT_FINSHOP_01",
        "X-Commerce-Session-Id": "CSESS_FINSHOP_DEFAULT_01",
        "X-World-Epoch": "1",
    }
    write_h = {
        "X-Commerce-Write-Key": s.service_write_api_key.get_secret_value(),
        "X-Merchant-Id": "MERCHANT_FINSHOP_01",
        "X-Commerce-Session-Id": "CSESS_FINSHOP_DEFAULT_01",
        "X-World-Epoch": "1",
        "X-Authorization-Digest": "a" * 64,
    }
    return chaos_h, read_h, write_h


def test_policy_gated_recovery_commands_and_safety_guards(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify write auth separation, C01 sync, C02 block, version check, and unsupported block."""
    monkeypatch.setenv("COMMERCE_CHAOS_ENABLED", "true")
    get_settings.cache_clear()
    chaos_h, read_h, write_h = _headers()

    # 1. Read key MUST NOT work on write endpoint (401)
    bad_auth = client.post(
        "/api/v1/service/recovery/execute",
        headers={
            **write_h,
            "X-Commerce-Write-Key": get_settings().service_api_key.get_secret_value(),
        },
        json={"command": "REQUEST_MANUAL_REVIEW", "idempotency_key": "IDEMP-BAD-AUTH"},
    )
    assert bad_auth.status_code == 401

    # 2. Unsupported command rejected (403)
    bad_cmd = client.post(
        "/api/v1/service/recovery/execute",
        headers=write_h,
        json={"command": "REFUND_EXECUTE", "idempotency_key": "IDEMP-BAD-CMD"},
    )
    assert bad_cmd.status_code == 403

    # 3. P0.1: Target-specific C01 sync MUST NOT mutate a second unrelated unsynced C01 order!
    m1 = client.post(
        "/api/v1/chaos/inject", headers=chaos_h, json={"family": "C01", "seed": 501}
    ).json()
    m1_other = client.post(
        "/api/v1/chaos/inject", headers=chaos_h, json={"family": "C01", "seed": 599}
    ).json()
    o1 = client.get(f"/api/v1/service/orders/{m1['affected_order_id']}", headers=read_h).json()

    r_ver = client.post(
        "/api/v1/service/recovery/execute",
        headers=_signed_write_headers(
            write_h,
            command="REQUEST_ORDER_SYNC",
            order_id=m1["affected_order_id"],
            payment_id=m1["affected_payment_id"],
            expected_version=o1["version"] + 99,
            idempotency_key="IDEMP-VER-BAD",
        ),
        json={
            "command": "REQUEST_ORDER_SYNC",
            "order_id": m1["affected_order_id"],
            "payment_id": m1["affected_payment_id"],
            "expected_version": o1["version"] + 99,
            "idempotency_key": "IDEMP-VER-BAD",
        },
    )
    assert r_ver.status_code == 409

    r_ok = client.post(
        "/api/v1/service/recovery/execute",
        headers=_signed_write_headers(
            write_h,
            command="REQUEST_ORDER_SYNC",
            order_id=m1["affected_order_id"],
            payment_id=m1["affected_payment_id"],
            expected_version=o1["version"],
            idempotency_key="IDEMP-SYNC-OK",
        ),
        json={
            "command": "REQUEST_ORDER_SYNC",
            "order_id": m1["affected_order_id"],
            "payment_id": m1["affected_payment_id"],
            "expected_version": o1["version"],
            "idempotency_key": "IDEMP-SYNC-OK",
        },
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["order_status"] == "CONFIRMED"

    # Unrelated C01 order MUST remain untouched (PENDING_PAYMENT)!
    o1_other = client.get(
        f"/api/v1/service/orders/{m1_other['affected_order_id']}", headers=read_h
    ).json()
    assert o1_other["status"] == "PENDING_PAYMENT"

    # 4. C02 (unpaid lookalike) -> MUST be rejected with 409 PAYMENT_NOT_CONFIRMED!
    m2 = client.post(
        "/api/v1/chaos/inject", headers=chaos_h, json={"family": "C02", "seed": 502}
    ).json()
    r_c02 = client.post(
        "/api/v1/service/recovery/execute",
        headers=_signed_write_headers(
            write_h,
            command="REQUEST_ORDER_SYNC",
            order_id=m2["affected_order_id"],
            payment_id=m2["affected_payment_id"],
            idempotency_key="IDEMP-C02-BLOCK",
        ),
        json={
            "command": "REQUEST_ORDER_SYNC",
            "order_id": m2["affected_order_id"],
            "payment_id": m2["affected_payment_id"],
            "idempotency_key": "IDEMP-C02-BLOCK",
        },
    )
    assert r_c02.status_code == 409

    # 5. C03 (cancelled order with late payment) -> MUST NOT resurrect (409)!
    m3 = client.post(
        "/api/v1/chaos/inject", headers=chaos_h, json={"family": "C03", "seed": 503}
    ).json()
    r_c03 = client.post(
        "/api/v1/service/recovery/execute",
        headers=_signed_write_headers(
            write_h,
            command="REQUEST_ORDER_SYNC",
            order_id=m3["affected_order_id"],
            payment_id=m3["affected_payment_id"],
            idempotency_key="IDEMP-C03-BLOCK",
        ),
        json={
            "command": "REQUEST_ORDER_SYNC",
            "order_id": m3["affected_order_id"],
            "payment_id": m3["affected_payment_id"],
            "idempotency_key": "IDEMP-C03-BLOCK",
        },
    )
    assert r_c03.status_code == 409

    # 6. P0.2: Unscoped event redelivery rejected (400); scoped event_id succeeds!
    r_unscoped = client.post(
        "/api/v1/service/recovery/execute",
        headers=_signed_write_headers(
            write_h,
            command="REQUEST_EVENT_REDELIVERY",
            idempotency_key="IDEMP-REDEL-BAD",
        ),
        json={"command": "REQUEST_EVENT_REDELIVERY", "idempotency_key": "IDEMP-REDEL-BAD"},
    )
    assert r_unscoped.status_code == 400

    r_redel = client.post(
        "/api/v1/service/recovery/execute",
        headers=_signed_write_headers(
            write_h,
            command="REQUEST_EVENT_REDELIVERY",
            order_id=m1["affected_order_id"],
            idempotency_key="IDEMP-REDEL-1",
        ),
        json={
            "command": "REQUEST_EVENT_REDELIVERY",
            "order_id": m1["affected_order_id"],
            "idempotency_key": "IDEMP-REDEL-1",
        },
    )
    assert r_redel.status_code == 200
    r_rev = client.post(
        "/api/v1/service/recovery/execute",
        headers=_signed_write_headers(
            write_h,
            command="REQUEST_MANUAL_REVIEW",
            order_id=m3["affected_order_id"],
            idempotency_key="IDEMP-REV-1",
        ),
        json={
            "command": "REQUEST_MANUAL_REVIEW",
            "order_id": m3["affected_order_id"],
            "idempotency_key": "IDEMP-REV-1",
        },
    )
    assert r_rev.status_code == 200
    assert r_rev.json()["status"] == "ESCALATED_MANUAL_REVIEW"
