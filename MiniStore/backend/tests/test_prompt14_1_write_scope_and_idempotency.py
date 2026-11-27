"""Prompt 14.1+14.2 Security: Signed HMAC-SHA256 Auth, Scope Isolation & Idempotency."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from app.core.auth import hash_token
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.entities import CommerceSession, ServiceCredential
from app.services.seed import DEFAULT_MERCHANT_ID, DEFAULT_SESSION_ID


def sign_test_authorization(
    *,
    command: str,
    idempotency_key: str,
    merchant_id: str = DEFAULT_MERCHANT_ID,
    commerce_session_id: str = DEFAULT_SESSION_ID,
    world_epoch: int = 1,
    order_id: str | None = None,
    payment_id: str | None = None,
    event_id: str | None = None,
    expected_version: int | None = None,
    key_id: str | None = None,
    secret_override: str | None = None,
    expires_delta_seconds: int = 120,
    request_hash_override: str | None = None,
) -> str:
    """Produce a real HMAC-SHA256 signed PolicyEngine command authorization token."""
    settings = get_settings()
    secret = (
        secret_override
        if secret_override is not None
        else settings.command_auth_signing_secret.get_secret_value().strip()
    )
    canonical_req = {
        "merchant_id": merchant_id,
        "commerce_session_id": commerce_session_id,
        "world_epoch": world_epoch,
        "command": command.strip().upper(),
        "order_id": order_id,
        "payment_id": payment_id,
        "event_id": event_id,
        "expected_version": expected_version,
    }
    req_hash = (
        request_hash_override
        or hashlib.sha256(
            json.dumps(canonical_req, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    )
    now_ts = int(datetime.now(UTC).timestamp())
    claims: dict[str, Any] = {
        "auth_id": f"AUTH-{uuid.uuid4().hex[:10].upper()}",
        "key_id": key_id if key_id is not None else settings.command_auth_key_id,
        "policy_version": "FinResolve-Policy-v1.2",
        "policy_decision": "ALLOW_POLICY_ENGINE_LOCAL_MOCK_LOW_RISK",
        "command": command.strip().upper(),
        "idempotency_key": idempotency_key,
        "request_hash": req_hash,
        "merchant_id": merchant_id,
        "commerce_session_id": commerce_session_id,
        "world_epoch": world_epoch,
        "source_binding_id": "BIND-TEST",
        "target_order_id": order_id,
        "target_payment_id": payment_id,
        "target_event_id": event_id,
        "expected_version": expected_version,
        "approval_id": None,
        "issued_at": now_ts,
        "expires_at": now_ts + expires_delta_seconds,
    }
    raw = json.dumps(claims, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{b64}.{sig}"


def test_cryptographic_command_authorization_rejects_forgeries_and_tampering(
    client: TestClient,
) -> None:
    """Prove rejection of random 64-char digests, forged sigs, tampered targets/cmds, expired."""
    settings = get_settings()
    write_key = settings.service_write_api_key.get_secret_value()
    with get_sessionmaker()() as db:
        csess = db.get(CommerceSession, DEFAULT_SESSION_ID)
        assert csess is not None
        epoch = csess.world_epoch

    base_headers = {
        "X-Commerce-Write-Key": write_key,
        "X-Merchant-Id": DEFAULT_MERCHANT_ID,
        "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
        "X-World-Epoch": str(epoch),
    }

    # 1. Random 64-char digest MUST be rejected (403)
    r_rand = client.post(
        "/api/v1/service/recovery/execute",
        headers={
            **base_headers,
            "X-Command-Authorization": "a" * 64,
            "X-Authorization-Digest": "a" * 64,
        },
        json={
            "command": "REQUEST_MANUAL_REVIEW",
            "idempotency_key": f"IDEMP-R-{uuid.uuid4().hex[:6]}",
        },
    )
    assert r_rand.status_code == 403

    # 2. Forged signature (signed with wrong secret) MUST be rejected (403)
    k_forged = f"IDEMP-FORG-{uuid.uuid4().hex[:6]}"
    tok_forged = sign_test_authorization(
        command="REQUEST_MANUAL_REVIEW",
        idempotency_key=k_forged,
        world_epoch=epoch,
        secret_override="attacker-wrong-secret-key-0000",
    )
    r_forged = client.post(
        "/api/v1/service/recovery/execute",
        headers={**base_headers, "X-Command-Authorization": tok_forged},
        json={"command": "REQUEST_MANUAL_REVIEW", "idempotency_key": k_forged},
    )
    assert r_forged.status_code == 403

    # 3. Unauthorized key_id MUST be rejected (403)
    k_kid = f"IDEMP-KID-{uuid.uuid4().hex[:6]}"
    tok_kid = sign_test_authorization(
        command="REQUEST_MANUAL_REVIEW",
        idempotency_key=k_kid,
        world_epoch=epoch,
        key_id="untrusted-key-v99",
    )
    r_kid = client.post(
        "/api/v1/service/recovery/execute",
        headers={**base_headers, "X-Command-Authorization": tok_kid},
        json={"command": "REQUEST_MANUAL_REVIEW", "idempotency_key": k_kid},
    )
    assert r_kid.status_code == 403

    # 4. Expired authorization MUST be rejected (403)
    k_exp = f"IDEMP-EXP-{uuid.uuid4().hex[:6]}"
    tok_exp = sign_test_authorization(
        command="REQUEST_MANUAL_REVIEW",
        idempotency_key=k_exp,
        world_epoch=epoch,
        expires_delta_seconds=-30,
    )
    r_exp = client.post(
        "/api/v1/service/recovery/execute",
        headers={**base_headers, "X-Command-Authorization": tok_exp},
        json={"command": "REQUEST_MANUAL_REVIEW", "idempotency_key": k_exp},
    )
    assert r_exp.status_code == 403

    # 5. Modified target order_id (signed for ORD-A, sent for ORD-B) MUST be rejected (403)
    k_mod_ord = f"IDEMP-MODO-{uuid.uuid4().hex[:6]}"
    tok_mod_ord = sign_test_authorization(
        command="REQUEST_MANUAL_REVIEW",
        idempotency_key=k_mod_ord,
        world_epoch=epoch,
        order_id="ORD-A",
    )
    r_mod_ord = client.post(
        "/api/v1/service/recovery/execute",
        headers={**base_headers, "X-Command-Authorization": tok_mod_ord},
        json={
            "command": "REQUEST_MANUAL_REVIEW",
            "order_id": "ORD-B",
            "idempotency_key": k_mod_ord,
        },
    )
    assert r_mod_ord.status_code == 403

    # 6. Modified command type (signed for REQUEST_MANUAL_REVIEW, sent as REQUEST_ORDER_SYNC) -> 403
    k_mod_cmd = f"IDEMP-MODC-{uuid.uuid4().hex[:6]}"
    tok_mod_cmd = sign_test_authorization(
        command="REQUEST_MANUAL_REVIEW",
        idempotency_key=k_mod_cmd,
        world_epoch=epoch,
        order_id="ORD-A",
    )
    r_mod_cmd = client.post(
        "/api/v1/service/recovery/execute",
        headers={**base_headers, "X-Command-Authorization": tok_mod_cmd},
        json={"command": "REQUEST_ORDER_SYNC", "order_id": "ORD-A", "idempotency_key": k_mod_cmd},
    )
    assert r_mod_cmd.status_code == 403

    # 7. Modified request_hash MUST be rejected (403)
    k_mod_h = f"IDEMP-MODH-{uuid.uuid4().hex[:6]}"
    tok_mod_h = sign_test_authorization(
        command="REQUEST_MANUAL_REVIEW",
        idempotency_key=k_mod_h,
        world_epoch=epoch,
        request_hash_override="f" * 64,
    )
    r_mod_h = client.post(
        "/api/v1/service/recovery/execute",
        headers={**base_headers, "X-Command-Authorization": tok_mod_h},
        json={"command": "REQUEST_MANUAL_REVIEW", "idempotency_key": k_mod_h},
    )
    assert r_mod_h.status_code == 403

    # 8. Legitimate signed authorization executes cleanly (200)
    k_ok = f"IDEMP-SIGNED-OK-{uuid.uuid4().hex[:6]}"
    tok_ok = sign_test_authorization(
        command="REQUEST_MANUAL_REVIEW",
        idempotency_key=k_ok,
        world_epoch=epoch,
    )
    r_ok = client.post(
        "/api/v1/service/recovery/execute",
        headers={**base_headers, "X-Command-Authorization": tok_ok},
        json={"command": "REQUEST_MANUAL_REVIEW", "idempotency_key": k_ok},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["status"] == "ESCALATED_MANUAL_REVIEW"


def test_write_credential_scope_isolation_and_capability_enforcement(client: TestClient) -> None:
    """Verify read key rejection, scope isolation, stale world rejection, revocation & expiry."""
    settings = get_settings()
    read_key = settings.service_api_key.get_secret_value()
    write_key = settings.service_write_api_key.get_secret_value()

    with get_sessionmaker()() as db:
        csess = db.get(CommerceSession, DEFAULT_SESSION_ID)
        assert csess is not None
        current_epoch = csess.world_epoch

    k_base = f"IDEMP-SCOPE-{uuid.uuid4().hex[:8]}"
    tok_base = sign_test_authorization(
        command="REQUEST_MANUAL_REVIEW", idempotency_key=k_base, world_epoch=current_epoch
    )

    # 1. Read key MUST be rejected on write API (401)
    r_read = client.post(
        "/api/v1/service/recovery/execute",
        headers={
            "X-Commerce-Write-Key": read_key,
            "X-Merchant-Id": DEFAULT_MERCHANT_ID,
            "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
            "X-World-Epoch": str(current_epoch),
            "X-Command-Authorization": tok_base,
        },
        json={"command": "REQUEST_MANUAL_REVIEW", "idempotency_key": k_base},
    )
    assert r_read.status_code == 401

    # 2. Cross-merchant header override MUST be rejected (403)
    r_merch = client.post(
        "/api/v1/service/recovery/execute",
        headers={
            "X-Commerce-Write-Key": write_key,
            "X-Merchant-Id": "OTHER_MERCHANT_999",
            "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
            "X-World-Epoch": str(current_epoch),
            "X-Command-Authorization": tok_base,
        },
        json={"command": "REQUEST_MANUAL_REVIEW", "idempotency_key": k_base},
    )
    assert r_merch.status_code == 403

    # 3. Cross-session header override MUST be rejected (403)
    r_sess = client.post(
        "/api/v1/service/recovery/execute",
        headers={
            "X-Commerce-Write-Key": write_key,
            "X-Merchant-Id": DEFAULT_MERCHANT_ID,
            "X-Commerce-Session-Id": "OTHER_SESSION_999",
            "X-World-Epoch": str(current_epoch),
            "X-Command-Authorization": tok_base,
        },
        json={"command": "REQUEST_MANUAL_REVIEW", "idempotency_key": k_base},
    )
    assert r_sess.status_code == 403

    # 4. Stale/mismatched world epoch MUST be rejected (409)
    r_epoch = client.post(
        "/api/v1/service/recovery/execute",
        headers={
            "X-Commerce-Write-Key": write_key,
            "X-Merchant-Id": DEFAULT_MERCHANT_ID,
            "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
            "X-World-Epoch": str(current_epoch + 99),
            "X-Command-Authorization": tok_base,
        },
        json={"command": "REQUEST_MANUAL_REVIEW", "idempotency_key": k_base},
    )
    assert r_epoch.status_code == 409

    # 5. Restricted, revoked, and expired credentials rejected (403)
    limited_secret = f"limited-write-key-{uuid.uuid4().hex}"
    revoked_secret = f"revoked-write-key-{uuid.uuid4().hex}"
    expired_secret = f"expired-write-key-{uuid.uuid4().hex}"
    with get_sessionmaker()() as db:
        db.add_all(
            [
                ServiceCredential(
                    id=f"SCRED-LIM-{uuid.uuid4().hex[:8]}",
                    service_name="LIMITED_RECOVERY",
                    key_hash=hash_token(limited_secret),
                    merchant_id=DEFAULT_MERCHANT_ID,
                    commerce_session_id=DEFAULT_SESSION_ID,
                    allowed_scopes_json=["recovery:REQUEST_MANUAL_REVIEW"],
                    is_active=True,
                ),
                ServiceCredential(
                    id=f"SCRED-REV-{uuid.uuid4().hex[:8]}",
                    service_name="REVOKED_RECOVERY",
                    key_hash=hash_token(revoked_secret),
                    merchant_id=DEFAULT_MERCHANT_ID,
                    commerce_session_id=DEFAULT_SESSION_ID,
                    allowed_scopes_json=["recovery:REQUEST_MANUAL_REVIEW"],
                    is_active=True,
                    revoked_at=datetime.now(UTC),
                ),
                ServiceCredential(
                    id=f"SCRED-EXP-{uuid.uuid4().hex[:8]}",
                    service_name="EXPIRED_RECOVERY",
                    key_hash=hash_token(expired_secret),
                    merchant_id=DEFAULT_MERCHANT_ID,
                    commerce_session_id=DEFAULT_SESSION_ID,
                    allowed_scopes_json=["recovery:REQUEST_MANUAL_REVIEW"],
                    is_active=True,
                    expires_at=datetime.now(UTC) - timedelta(minutes=1),
                ),
            ]
        )
        db.commit()

    k_cap = f"IDEMP-CAP-{uuid.uuid4().hex[:8]}"
    tok_cap = sign_test_authorization(
        command="REQUEST_ORDER_SYNC",
        order_id="ORD-1",
        idempotency_key=k_cap,
        world_epoch=current_epoch,
    )
    for sec, cmd_val, oid_val, tok_val, k_val in (
        (limited_secret, "REQUEST_ORDER_SYNC", "ORD-1", tok_cap, k_cap),
        (revoked_secret, "REQUEST_MANUAL_REVIEW", None, tok_base, k_base),
        (expired_secret, "REQUEST_MANUAL_REVIEW", None, tok_base, k_base),
    ):
        r_bad = client.post(
            "/api/v1/service/recovery/execute",
            headers={
                "X-Commerce-Write-Key": sec,
                "X-Merchant-Id": DEFAULT_MERCHANT_ID,
                "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
                "X-World-Epoch": str(current_epoch),
                "X-Command-Authorization": tok_val,
            },
            json={"command": cmd_val, "order_id": oid_val, "idempotency_key": k_val},
        )
        assert r_bad.status_code == 403


def test_receiver_side_durable_idempotency_conflict_and_status_lookup(client: TestClient) -> None:
    """Verify receiver-side idempotent replay, conflicting payload 409, and status lookup."""
    settings = get_settings()
    write_key = settings.service_write_api_key.get_secret_value()
    with get_sessionmaker()() as db:
        csess = db.get(CommerceSession, DEFAULT_SESSION_ID)
        assert csess is not None
        epoch = csess.world_epoch

    idemp_key = f"IDEMP-RCV-{uuid.uuid4().hex[:8]}"
    tok = sign_test_authorization(
        command="REQUEST_MANUAL_REVIEW",
        order_id="ORD-TEST-01",
        idempotency_key=idemp_key,
        world_epoch=epoch,
    )
    headers = {
        "X-Commerce-Write-Key": write_key,
        "X-Merchant-Id": DEFAULT_MERCHANT_ID,
        "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
        "X-World-Epoch": str(epoch),
        "X-Command-Authorization": tok,
    }

    r1 = client.post(
        "/api/v1/service/recovery/execute",
        headers=headers,
        json={
            "command": "REQUEST_MANUAL_REVIEW",
            "order_id": "ORD-TEST-01",
            "idempotency_key": idemp_key,
        },
    )
    assert r1.status_code == 200
    assert r1.json().get("idempotent_replay") is not True

    # Replay succeeds even if token is subsequently expired
    tok_expired = sign_test_authorization(
        command="REQUEST_MANUAL_REVIEW",
        order_id="ORD-TEST-01",
        idempotency_key=idemp_key,
        world_epoch=epoch,
        expires_delta_seconds=-60,
    )
    r2 = client.post(
        "/api/v1/service/recovery/execute",
        headers={**headers, "X-Command-Authorization": tok_expired},
        json={
            "command": "REQUEST_MANUAL_REVIEW",
            "order_id": "ORD-TEST-01",
            "idempotency_key": idemp_key,
        },
    )
    assert r2.status_code == 200
    assert r2.json().get("idempotent_replay") is True

    # Conflicting payload with same idempotency_key -> 409 CONFLICT
    r3 = client.post(
        "/api/v1/service/recovery/execute",
        headers=headers,
        json={
            "command": "REQUEST_MANUAL_REVIEW",
            "order_id": "ORD-DIFFERENT-99",
            "idempotency_key": idemp_key,
        },
    )
    assert r3.status_code == 409

    # Authenticated status lookup works
    r_stat = client.get(f"/api/v1/service/recovery/commands/{idemp_key}", headers=headers)
    assert r_stat.status_code == 200
    body = r_stat.json()
    assert body["idempotency_key"] == idemp_key
    assert body["status"] == "EXECUTED"
    assert body["http_status"] == 200
