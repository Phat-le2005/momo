"""Dedicated Policy-Gated LOCAL_MOCK Recovery Commands API (Prompt 13 + Prompt 14 P0.1/P0.2).

Hard Security & Isolation Invariants:
- Protected by dedicated `X-Commerce-Write-Key` (read key is ALWAYS rejected!).
- P0.1: `REQUEST_ORDER_SYNC` uses `sync_single_confirmed_payment_to_order` targeting ONLY
  the explicitly authorized `(order_id, payment_id)` within `(merchant_id, commerce_session_id,
  world_epoch)` and enforcing `expected_version` under row lock. NEVER mutates unrelated orders!
- P0.2: `REQUEST_EVENT_REDELIVERY` requires explicit `event_id` (or scoped `order_id`) and joins
  `CommerceEvent` strictly on `(merchant_id, commerce_session_id, world_epoch)` so cross-merchant
  or cross-world event redelivery is impossible!
- Strictly restricted to `LOCAL_MOCK` payments; NEVER mutates VNPAY transactions.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import hash_token
from app.core.config import get_settings
from app.core.errors import CommerceError, ErrorCode
from app.db.session import get_db
from app.models.entities import (
    CommerceEvent,
    CommerceSession,
    EventOutbox,
    Order,
    Payment,
    ReceivedRecoveryCommand,
    ServiceCredential,
)
from app.models.enums import (
    OutboxStatus,
    PaymentProvider,
    ReconciliationStatus,
)
from app.services.events import record_commerce_event
from app.services.sync import sync_single_confirmed_payment_to_order

router = APIRouter(prefix="/service/recovery", tags=["service-recovery-write"])

ALLOWED_RECOVERY_COMMANDS: frozenset[str] = frozenset(
    {
        "REQUEST_ORDER_SYNC",
        "REQUEST_EVENT_REDELIVERY",
        "REQUEST_PAYMENT_RECONCILIATION",
        "REQUEST_MANUAL_REVIEW",
    }
)

STATE_CHANGING_COMMANDS: frozenset[str] = frozenset(
    {
        "REQUEST_ORDER_SYNC",
        "REQUEST_EVENT_REDELIVERY",
        "REQUEST_PAYMENT_RECONCILIATION",
    }
)


class WritePrincipal(BaseModel):
    credential_id: str
    service_name: str
    merchant_id: str
    commerce_session_id: str
    world_epoch: int
    allowed_commands: list[str]
    authorization_token: str
    authorization_digest: str


def _require_write_principal(
    x_commerce_write_key: str = Header(default=""),
    x_merchant_id: str = Header(default=""),
    x_commerce_session_id: str = Header(default=""),
    x_world_epoch: int = Header(default=0),
    x_command_authorization: str = Header(default=""),
    x_authorization_digest: str = Header(default=""),
    db: Session = Depends(get_db),
) -> WritePrincipal:
    """Authenticate write credential strictly from server-side `service_credentials` records.

    Hard Security Invariants (Prompt 14.1 P0):
    - Read credentials (missing `recovery:*` scopes) are ALWAYS rejected.
    - Merchant and commerce_session_id are enforced from server-side credential records;
      caller-supplied headers CANNOT expand or override authorized scope.
    - Revoked or expired credentials are rejected.
    - `X-World-Epoch` is validated against authoritative `CommerceSession.world_epoch` in DB
      to prevent historical/stale-world writes.
    """
    raw_key = x_commerce_write_key.strip()
    if not raw_key:
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            "Missing X-Commerce-Write-Key (read key is not permitted for writes)",
            status_code=401,
        )

    key_digest = hash_token(raw_key)
    cred = db.scalar(select(ServiceCredential).where(ServiceCredential.key_hash == key_digest))
    if cred is None:
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            "Invalid X-Commerce-Write-Key (unrecognized write credential)",
            status_code=401,
        )

    now = datetime.now(UTC)
    if not cred.is_active or cred.revoked_at is not None:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "WRITE_CREDENTIAL_REVOKED: credential is inactive or revoked",
            status_code=403,
        )
    if cred.expires_at is not None:
        exp = cred.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp <= now:
            raise CommerceError(
                ErrorCode.FORBIDDEN,
                "WRITE_CREDENTIAL_EXPIRED: credential has expired",
                status_code=403,
            )

    allowed_cmds = [
        s.split("recovery:", 1)[1].strip().upper()
        for s in (cred.allowed_scopes_json or [])
        if isinstance(s, str) and s.startswith("recovery:")
    ]
    if not allowed_cmds:
        raise CommerceError(
            ErrorCode.UNAUTHORIZED,
            "READ_CREDENTIAL_FORBIDDEN_FOR_WRITES: credential lacks recovery write capabilities",
            status_code=401,
        )

    # Server-side scope isolation: caller headers MUST NOT expand or cross credential scope
    req_merchant = x_merchant_id.strip()
    req_session = x_commerce_session_id.strip()
    if req_merchant and req_merchant != cred.merchant_id:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "CREDENTIAL_SCOPE_VIOLATION: X-Merchant-Id does not match credential merchant scope",
            status_code=403,
        )
    if req_session and req_session != cred.commerce_session_id:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "CREDENTIAL_SCOPE_VIOLATION: X-Commerce-Session-Id does not match credential scope",
            status_code=403,
        )

    # Validate authoritative world_epoch against CommerceSession in DB
    csess = db.scalar(
        select(CommerceSession).where(
            CommerceSession.id == cred.commerce_session_id,
            CommerceSession.merchant_id == cred.merchant_id,
        )
    )
    if csess is None or not csess.is_active:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "COMMERCE_SESSION_INACTIVE: authoritative commerce session not found or inactive",
            status_code=403,
        )
    if x_world_epoch < 1 or x_world_epoch != csess.world_epoch:
        raise CommerceError(
            ErrorCode.CONFLICT,
            f"STALE_WORLD_EPOCH: requested world_epoch={x_world_epoch} does not match "
            f"authoritative world_epoch={csess.world_epoch}",
            status_code=409,
            details={"reason_code": "STALE_WORLD_EPOCH"},
        )

    return WritePrincipal(
        credential_id=cred.id,
        service_name=cred.service_name,
        merchant_id=cred.merchant_id,
        commerce_session_id=cred.commerce_session_id,
        world_epoch=csess.world_epoch,
        allowed_commands=allowed_cmds,
        authorization_token=x_command_authorization.strip(),
        authorization_digest=x_authorization_digest.strip(),
    )


def _verify_signed_command_authorization(
    *,
    token: str,
    principal: WritePrincipal,
    cmd: str,
    payload: RecoveryCommandRequest,
    req_hash: str,
) -> dict[str, Any]:
    """Independently verify HMAC-SHA256 signed PolicyEngine command authorization (fail-closed)."""
    settings = get_settings()
    if not settings.recovery_write_enabled:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "RECOVERY_WRITE_DISABLED: autonomous recovery writes are explicitly disabled",
            status_code=403,
        )
    secret = settings.command_auth_signing_secret.get_secret_value().strip()
    if len(secret) < 32:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "AUTHORIZATION_VERIFICATION_UNAVAILABLE: >= 256-bit signing secret required",
            status_code=403,
        )
    if not token or "." not in token:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "MISSING_OR_MALFORMED_COMMAND_AUTHORIZATION: signed X-Command-Authorization required",
            status_code=403,
        )
    parts = token.split(".", 1)
    if len(parts) != 2 or not parts[0] or len(parts[1]) != 64:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "MALFORMED_COMMAND_AUTHORIZATION: invalid signed token format",
            status_code=403,
        )
    b64_part, provided_sig = parts
    try:
        padded = b64_part + "=" * (-len(b64_part) % 4)
        raw_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "INVALID_AUTHORIZATION_ENCODING: unable to decode authorization payload",
            status_code=403,
        ) from exc

    if not isinstance(claims, dict):
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "INVALID_AUTHORIZATION_PAYLOAD: payload must be a JSON object",
            status_code=403,
        )

    expected_sig = hmac.new(secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, provided_sig.lower()):
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "INVALID_AUTHORIZATION_SIGNATURE: cryptographic HMAC-SHA256 verification failed",
            status_code=403,
        )

    if str(claims.get("key_id") or "") != settings.command_auth_key_id:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "UNAUTHORIZED_SIGNING_KEY: unrecognized command authorization key_id",
            status_code=403,
        )

    now_epoch = int(datetime.now(UTC).timestamp())
    exp_ts = claims.get("expires_at")
    iat_ts = claims.get("issued_at")
    if not isinstance(exp_ts, int) or not isinstance(iat_ts, int) or exp_ts <= now_epoch:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "EXPIRED_COMMAND_AUTHORIZATION: authorization token has expired",
            status_code=403,
        )
    if iat_ts > now_epoch + 30:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "FUTURE_COMMAND_AUTHORIZATION: issued_at timestamp is in the future",
            status_code=403,
        )

    # Verify all bound scope, target, command, and canonical request_hash fields
    checks = (
        ("merchant_id", claims.get("merchant_id"), principal.merchant_id),
        ("commerce_session_id", claims.get("commerce_session_id"), principal.commerce_session_id),
        ("world_epoch", claims.get("world_epoch"), principal.world_epoch),
        ("command", str(claims.get("command") or "").upper(), cmd),
        ("idempotency_key", claims.get("idempotency_key"), payload.idempotency_key),
        ("request_hash", claims.get("request_hash"), req_hash),
        ("target_order_id", claims.get("target_order_id"), payload.order_id),
        ("target_payment_id", claims.get("target_payment_id"), payload.payment_id),
        ("target_event_id", claims.get("target_event_id"), payload.event_id),
        ("expected_version", claims.get("expected_version"), payload.expected_version),
    )
    for field_name, claimed_val, actual_val in checks:
        if claimed_val != actual_val:
            raise CommerceError(
                ErrorCode.FORBIDDEN,
                f"AUTHORIZATION_PAYLOAD_MISMATCH: bound '{field_name}' does not match request",
                status_code=403,
            )

    return claims


class RecoveryCommandRequest(BaseModel):
    command: str = Field(..., min_length=3, max_length=64)
    order_id: str | None = Field(default=None, max_length=64)
    payment_id: str | None = Field(default=None, max_length=64)
    event_id: str | None = Field(default=None, max_length=128)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(..., min_length=4, max_length=128)
    reason: str = Field(default="FinResolve policy-gated recovery command", max_length=500)


def _compute_receiver_request_hash(
    *,
    principal: WritePrincipal,
    cmd: str,
    payload: RecoveryCommandRequest,
) -> str:
    canonical = {
        "merchant_id": principal.merchant_id,
        "commerce_session_id": principal.commerce_session_id,
        "world_epoch": principal.world_epoch,
        "command": cmd,
        "order_id": payload.order_id,
        "payment_id": payload.payment_id,
        "event_id": payload.event_id,
        "expected_version": payload.expected_version,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


@router.get("/commands/{idempotency_key}")
def get_recovery_command_status(
    idempotency_key: str,
    principal: WritePrincipal = Depends(_require_write_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Authenticated authoritative command-status lookup for unknown-outcome reconciliation."""
    row = db.scalar(
        select(ReceivedRecoveryCommand).where(
            ReceivedRecoveryCommand.merchant_id == principal.merchant_id,
            ReceivedRecoveryCommand.commerce_session_id == principal.commerce_session_id,
            ReceivedRecoveryCommand.idempotency_key == idempotency_key,
        )
    )
    if row is None:
        raise CommerceError(
            ErrorCode.NOT_FOUND,
            f"Recovery command '{idempotency_key}' not found in authorized scope",
            status_code=404,
        )
    return {
        "id": row.id,
        "command_id": row.id,
        "idempotency_key": row.idempotency_key,
        "request_hash": row.request_hash,
        "command": row.command,
        "merchant_id": row.merchant_id,
        "commerce_session_id": row.commerce_session_id,
        "world_epoch": row.world_epoch,
        "target_order_id": row.target_order_id,
        "target_payment_id": row.target_payment_id,
        "target_event_id": row.target_event_id,
        "status": row.status,
        "http_status": row.http_status,
        "result": row.result_json,
        "executed_at": row.executed_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


@router.post("/execute")
def execute_recovery_command(
    payload: RecoveryCommandRequest,
    principal: WritePrincipal = Depends(_require_write_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    cmd = payload.command.strip().upper()
    if cmd not in ALLOWED_RECOVERY_COMMANDS:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            f"UNSUPPORTED_RECOVERY_COMMAND: '{cmd}' is not in the permitted allowlist",
            status_code=403,
        )
    if cmd not in principal.allowed_commands:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            f"CAPABILITY_NOT_GRANTED: credential '{principal.credential_id}' is not authorized "
            f"for command '{cmd}'",
            status_code=403,
        )

    now = datetime.now(UTC)
    req_hash = _compute_receiver_request_hash(principal=principal, cmd=cmd, payload=payload)

    # Receiver-side durable idempotency check under row lock
    existing = db.scalar(
        select(ReceivedRecoveryCommand)
        .where(
            ReceivedRecoveryCommand.merchant_id == principal.merchant_id,
            ReceivedRecoveryCommand.commerce_session_id == principal.commerce_session_id,
            ReceivedRecoveryCommand.idempotency_key == payload.idempotency_key,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.request_hash != req_hash:
            raise CommerceError(
                ErrorCode.CONFLICT,
                "IDEMPOTENCY_KEY_CONFLICT: idempotency_key reused with different payload",
                status_code=409,
                details={"reason_code": "IDEMPOTENCY_KEY_CONFLICT"},
            )
        if existing.status == "EXECUTING":
            raise CommerceError(
                ErrorCode.CONFLICT,
                "COMMAND_IN_FLIGHT: concurrent duplicate execution blocked",
                status_code=409,
                details={"reason_code": "COMMAND_IN_FLIGHT"},
            )
        if existing.status == "EXECUTED":
            replayed = dict(existing.result_json or {})
            replayed["idempotent_replay"] = True
            return replayed

    # Cryptographically verify HMAC-SHA256 PolicyEngine authorization BEFORE any new mutation!
    verified_claims = _verify_signed_command_authorization(
        token=principal.authorization_token,
        principal=principal,
        cmd=cmd,
        payload=payload,
        req_hash=req_hash,
    )
    verified_auth_id = str(verified_claims.get("auth_id") or "")

    rec = ReceivedRecoveryCommand(
        id=f"RCV-{uuid.uuid4().hex[:10].upper()}",
        merchant_id=principal.merchant_id,
        commerce_session_id=principal.commerce_session_id,
        world_epoch=principal.world_epoch,
        idempotency_key=payload.idempotency_key,
        request_hash=req_hash,
        command=cmd,
        target_order_id=payload.order_id,
        target_payment_id=payload.payment_id,
        target_event_id=payload.event_id,
        expected_version=payload.expected_version,
        authorization_digest=verified_auth_id or principal.authorization_digest or None,
        credential_id=principal.credential_id,
        status="EXECUTING",
        result_json={},
        executed_at=now,
        updated_at=now,
    )
    try:
        db.add(rec)
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise CommerceError(
            ErrorCode.CONFLICT,
            "COMMAND_IN_FLIGHT: concurrent duplicate command rejected by unique constraint",
            status_code=409,
            details={"reason_code": "COMMAND_IN_FLIGHT"},
        ) from exc

    identity_envelope: dict[str, Any] = {
        "command_id": rec.id,
        "command": cmd,
        "idempotency_key": payload.idempotency_key,
        "request_hash": req_hash,
        "merchant_id": principal.merchant_id,
        "commerce_session_id": principal.commerce_session_id,
        "world_epoch": principal.world_epoch,
    }

    response: dict[str, Any]
    try:
        if cmd == "REQUEST_ORDER_SYNC":
            if not payload.order_id:
                raise CommerceError(
                    ErrorCode.BAD_REQUEST,
                    "order_id is required for REQUEST_ORDER_SYNC",
                    status_code=400,
                )
            pay_stmt = (
                select(Payment)
                .join(Order, Payment.order_id == Order.id)
                .where(
                    Payment.order_id == payload.order_id,
                    Payment.merchant_id == principal.merchant_id,
                    Payment.commerce_session_id == principal.commerce_session_id,
                    Order.world_epoch == principal.world_epoch,
                )
            )
            if payload.payment_id:
                pay_stmt = pay_stmt.where(Payment.id == payload.payment_id)
            pay_ref = db.scalar(pay_stmt.order_by(Payment.created_at.desc()).limit(1))
            if pay_ref is None:
                raise CommerceError(
                    ErrorCode.NOT_FOUND,
                    "Payment/Order not found in authorized write scope",
                    status_code=404,
                )
            if pay_ref.provider != PaymentProvider.LOCAL_MOCK.value:
                raise CommerceError(
                    ErrorCode.FORBIDDEN,
                    f"VNPAY_OR_EXTERNAL_WRITE_FORBIDDEN: provider '{pay_ref.provider}' blocked",
                    status_code=403,
                )

            order, pay, outcome_status = sync_single_confirmed_payment_to_order(
                db,
                order_id=payload.order_id,
                payment_id=pay_ref.id,
                merchant_id=principal.merchant_id,
                commerce_session_id=principal.commerce_session_id,
                world_epoch=principal.world_epoch,
                expected_version=payload.expected_version,
                sync_source="FINRESOLVE_RECOVERY_COMMAND",
                commit=False,
            )
            response = {
                **identity_envelope,
                "status": outcome_status,
                "order_id": order.id,
                "order_status": order.status,
                "order_version": order.version,
                "payment_id": pay.id,
                "is_synced_to_order": pay.is_synced_to_order,
            }
        elif cmd == "REQUEST_EVENT_REDELIVERY":
            if not payload.event_id and not payload.order_id:
                raise CommerceError(
                    ErrorCode.BAD_REQUEST,
                    "SCOPED_TARGET_REQUIRED: event_id or order_id required for redelivery",
                    status_code=400,
                )
            stmt = (
                select(EventOutbox)
                .join(CommerceEvent, EventOutbox.commerce_event_id == CommerceEvent.id)
                .where(
                    CommerceEvent.merchant_id == principal.merchant_id,
                    CommerceEvent.commerce_session_id == principal.commerce_session_id,
                    CommerceEvent.world_epoch == principal.world_epoch,
                    EventOutbox.status.in_([OutboxStatus.FAILED.value, OutboxStatus.PENDING.value]),
                )
            )
            if payload.event_id:
                stmt = stmt.where(EventOutbox.event_id == payload.event_id)
            if payload.order_id:
                stmt = stmt.where(CommerceEvent.order_id == payload.order_id)

            rows = list(db.scalars(stmt.limit(25).with_for_update()).all())
            redelivered = 0
            for row in rows:
                row.status = OutboxStatus.PENDING.value
                row.next_attempt_at = now
                redelivered += 1
            response = {
                **identity_envelope,
                "status": "EXECUTED",
                "redelivered_count": redelivered,
                "event_id": payload.event_id,
                "order_id": payload.order_id,
            }
        elif cmd == "REQUEST_PAYMENT_RECONCILIATION":
            if not payload.order_id:
                raise CommerceError(ErrorCode.BAD_REQUEST, "order_id required", status_code=400)
            recon_order = db.scalar(
                select(Order)
                .where(
                    Order.id == payload.order_id,
                    Order.merchant_id == principal.merchant_id,
                    Order.commerce_session_id == principal.commerce_session_id,
                    Order.world_epoch == principal.world_epoch,
                )
                .with_for_update()
            )
            if recon_order is None:
                raise CommerceError(
                    ErrorCode.NOT_FOUND, "Order not found in scope", status_code=404
                )
            if (
                payload.expected_version is not None
                and recon_order.version != payload.expected_version
            ):
                raise CommerceError(
                    ErrorCode.CONFLICT,
                    (
                        f"VERSION_MISMATCH: expected {payload.expected_version}, "
                        f"got {recon_order.version}"
                    ),
                    status_code=409,
                    details={"reason_code": "VERSION_MISMATCH"},
                )
            recon_order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
            recon_order.reconciliation_reason = payload.reason
            recon_order.updated_at = now
            if payload.payment_id:
                pay_row = db.get(Payment, payload.payment_id)
                if pay_row is not None and pay_row.order_id == recon_order.id:
                    pay_row.reconciliation_required = True
                    pay_row.reconciliation_note = payload.reason
            response = {
                **identity_envelope,
                "status": "EXECUTED",
                "order_id": recon_order.id,
                "reconciliation_status": recon_order.reconciliation_status,
            }
        else:
            record_commerce_event(
                db,
                merchant_id=principal.merchant_id,
                commerce_session_id=principal.commerce_session_id,
                world_epoch=principal.world_epoch,
                event_type="MANUAL_REVIEW_REQUESTED",
                order_id=payload.order_id,
                payment_id=payload.payment_id,
                payload={
                    "reason": payload.reason,
                    "idempotency_key": payload.idempotency_key,
                    "requested_by": "FINRESOLVE_RECOVERY_COMMAND",
                },
            )
            response = {
                **identity_envelope,
                "status": "ESCALATED_MANUAL_REVIEW",
                "order_id": payload.order_id,
                "payment_id": payload.payment_id,
            }
    except CommerceError:
        db.rollback()
        raise

    # Persist receiver-side execution result atomically in the same transaction
    rec.status = "EXECUTED"
    rec.http_status = 200
    rec.result_json = response
    rec.updated_at = datetime.now(UTC)
    db.commit()
    return response
