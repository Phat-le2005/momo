"""Prompt 14.1 Phase 1 Security Tests: Write Credential Scope Isolation & Capability Enforcement."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.core.auth import hash_token
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.models.entities import CommerceSession, ServiceCredential
from app.services.seed import DEFAULT_MERCHANT_ID, DEFAULT_SESSION_ID


def test_write_credential_scope_isolation_and_capability_enforcement(client: TestClient) -> None:
    """Verify read key rejection, scope isolation, stale world rejection, revocation & expiry."""
    settings = get_settings()
    read_key = settings.service_api_key.get_secret_value()
    write_key = settings.service_write_api_key.get_secret_value()
    valid_digest = "a" * 64

    with get_sessionmaker()() as db:
        csess = db.get(CommerceSession, DEFAULT_SESSION_ID)
        assert csess is not None
        current_epoch = csess.world_epoch

    if True:
        # 1. Read key MUST be rejected on write API
        r_read = client.post(
            "/api/v1/service/recovery/execute",
            headers={
                "X-Commerce-Write-Key": read_key,
                "X-Merchant-Id": DEFAULT_MERCHANT_ID,
                "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
                "X-World-Epoch": str(current_epoch),
                "X-Authorization-Digest": valid_digest,
            },
            json={
                "command": "REQUEST_MANUAL_REVIEW",
                "idempotency_key": f"IDEMP-READ-{uuid.uuid4().hex[:8]}",
            },
        )
        assert r_read.status_code == 403

        # 2. Cross-merchant header override MUST be rejected
        r_merch = client.post(
            "/api/v1/service/recovery/execute",
            headers={
                "X-Commerce-Write-Key": write_key,
                "X-Merchant-Id": "OTHER_MERCHANT_999",
                "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
                "X-World-Epoch": str(current_epoch),
                "X-Authorization-Digest": valid_digest,
            },
            json={
                "command": "REQUEST_MANUAL_REVIEW",
                "idempotency_key": f"IDEMP-MERCH-{uuid.uuid4().hex[:8]}",
            },
        )
        assert r_merch.status_code == 403

        # 3. Cross-session header override MUST be rejected
        r_sess = client.post(
            "/api/v1/service/recovery/execute",
            headers={
                "X-Commerce-Write-Key": write_key,
                "X-Merchant-Id": DEFAULT_MERCHANT_ID,
                "X-Commerce-Session-Id": "OTHER_SESSION_999",
                "X-World-Epoch": str(current_epoch),
                "X-Authorization-Digest": valid_digest,
            },
            json={
                "command": "REQUEST_MANUAL_REVIEW",
                "idempotency_key": f"IDEMP-SESS-{uuid.uuid4().hex[:8]}",
            },
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
                "X-Authorization-Digest": valid_digest,
            },
            json={
                "command": "REQUEST_MANUAL_REVIEW",
                "idempotency_key": f"IDEMP-EPOCH-{uuid.uuid4().hex[:8]}",
            },
        )
        assert r_epoch.status_code == 409

        # 5. Missing X-Authorization-Digest on state-changing command MUST be rejected
        r_nodigest = client.post(
            "/api/v1/service/recovery/execute",
            headers={
                "X-Commerce-Write-Key": write_key,
                "X-Merchant-Id": DEFAULT_MERCHANT_ID,
                "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
                "X-World-Epoch": str(current_epoch),
            },
            json={
                "command": "REQUEST_ORDER_SYNC",
                "order_id": "ORD-MISSING",
                "idempotency_key": f"IDEMP-NODIG-{uuid.uuid4().hex[:8]}",
            },
        )
        assert r_nodigest.status_code == 403

        # 6. Restricted capability credential cannot execute ungranted commands
        limited_secret = f"limited-write-key-{uuid.uuid4().hex}"
        limited_cred = ServiceCredential(
            id=f"SCRED-LIM-{uuid.uuid4().hex[:8]}",
            service_name="LIMITED_RECOVERY",
            key_hash=hash_token(limited_secret),
            merchant_id=DEFAULT_MERCHANT_ID,
            commerce_session_id=DEFAULT_SESSION_ID,
            allowed_scopes_json=["recovery:REQUEST_MANUAL_REVIEW"],
            is_active=True,
        )
        revoked_secret = f"revoked-write-key-{uuid.uuid4().hex}"
        revoked_cred = ServiceCredential(
            id=f"SCRED-REV-{uuid.uuid4().hex[:8]}",
            service_name="REVOKED_RECOVERY",
            key_hash=hash_token(revoked_secret),
            merchant_id=DEFAULT_MERCHANT_ID,
            commerce_session_id=DEFAULT_SESSION_ID,
            allowed_scopes_json=["recovery:REQUEST_MANUAL_REVIEW"],
            is_active=True,
            revoked_at=datetime.now(UTC),
        )
        expired_secret = f"expired-write-key-{uuid.uuid4().hex}"
        expired_cred = ServiceCredential(
            id=f"SCRED-EXP-{uuid.uuid4().hex[:8]}",
            service_name="EXPIRED_RECOVERY",
            key_hash=hash_token(expired_secret),
            merchant_id=DEFAULT_MERCHANT_ID,
            commerce_session_id=DEFAULT_SESSION_ID,
            allowed_scopes_json=["recovery:REQUEST_MANUAL_REVIEW"],
            is_active=True,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        with get_sessionmaker()() as db:
            db.add_all([limited_cred, revoked_cred, expired_cred])
            db.commit()

        r_cap = client.post(
            "/api/v1/service/recovery/execute",
            headers={
                "X-Commerce-Write-Key": limited_secret,
                "X-Merchant-Id": DEFAULT_MERCHANT_ID,
                "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
                "X-World-Epoch": str(current_epoch),
                "X-Authorization-Digest": valid_digest,
            },
            json={
                "command": "REQUEST_ORDER_SYNC",
                "order_id": "ORD-1",
                "idempotency_key": f"IDEMP-CAP-{uuid.uuid4().hex[:8]}",
            },
        )
        assert r_cap.status_code == 403

        r_rev = client.post(
            "/api/v1/service/recovery/execute",
            headers={
                "X-Commerce-Write-Key": revoked_secret,
                "X-Merchant-Id": DEFAULT_MERCHANT_ID,
                "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
                "X-World-Epoch": str(current_epoch),
                "X-Authorization-Digest": valid_digest,
            },
            json={
                "command": "REQUEST_MANUAL_REVIEW",
                "idempotency_key": f"IDEMP-REV-{uuid.uuid4().hex[:8]}",
            },
        )
        assert r_rev.status_code == 403

        r_exp = client.post(
            "/api/v1/service/recovery/execute",
            headers={
                "X-Commerce-Write-Key": expired_secret,
                "X-Merchant-Id": DEFAULT_MERCHANT_ID,
                "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
                "X-World-Epoch": str(current_epoch),
                "X-Authorization-Digest": valid_digest,
            },
            json={
                "command": "REQUEST_MANUAL_REVIEW",
                "idempotency_key": f"IDEMP-EXP-{uuid.uuid4().hex[:8]}",
            },
        )
        assert r_exp.status_code == 403

        # 7. Authorized write credential succeeds on permitted command
        r_ok = client.post(
            "/api/v1/service/recovery/execute",
            headers={
                "X-Commerce-Write-Key": write_key,
                "X-Merchant-Id": DEFAULT_MERCHANT_ID,
                "X-Commerce-Session-Id": DEFAULT_SESSION_ID,
                "X-World-Epoch": str(current_epoch),
                "X-Authorization-Digest": valid_digest,
            },
            json={
                "command": "REQUEST_MANUAL_REVIEW",
                "idempotency_key": f"IDEMP-OK-{uuid.uuid4().hex[:8]}",
            },
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["status"] == "ESCALATED_MANUAL_REVIEW"
