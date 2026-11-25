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

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import hash_token
from app.core.errors import CommerceError, ErrorCode
from app.db.session import get_db
from app.models.entities import (
    CommerceEvent,
    CommerceSession,
    EventOutbox,
    Order,
    Payment,
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
    authorization_digest: str


def _require_write_principal(
    x_commerce_write_key: str = Header(default=""),
    x_merchant_id: str = Header(default=""),
    x_commerce_session_id: str = Header(default=""),
    x_world_epoch: int = Header(default=0),
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
            ErrorCode.FORBIDDEN,
            "READ_CREDENTIAL_FORBIDDEN_FOR_WRITES: credential lacks recovery write capabilities",
            status_code=403,
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
        )

    return WritePrincipal(
        credential_id=cred.id,
        service_name=cred.service_name,
        merchant_id=cred.merchant_id,
        commerce_session_id=cred.commerce_session_id,
        world_epoch=csess.world_epoch,
        allowed_commands=allowed_cmds,
        authorization_digest=x_authorization_digest.strip(),
    )


class RecoveryCommandRequest(BaseModel):
    command: str = Field(..., min_length=3, max_length=64)
    order_id: str | None = Field(default=None, max_length=64)
    payment_id: str | None = Field(default=None, max_length=64)
    event_id: str | None = Field(default=None, max_length=128)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: str = Field(..., min_length=4, max_length=128)
    reason: str = Field(default="FinResolve policy-gated recovery command", max_length=500)


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
    if cmd in STATE_CHANGING_COMMANDS and len(principal.authorization_digest) < 32:
        raise CommerceError(
            ErrorCode.FORBIDDEN,
            "MISSING_COMMAND_AUTHORIZATION: verified PolicyEngine X-Authorization-Digest required",
            status_code=403,
        )

    now = datetime.now(UTC)

    if cmd == "REQUEST_ORDER_SYNC":
        if not payload.order_id:
            raise CommerceError(
                ErrorCode.BAD_REQUEST,
                "order_id is required for REQUEST_ORDER_SYNC",
                status_code=400,
            )
        # Resolve payment strictly within authorized scope and verify LOCAL_MOCK before syncing
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

        # P0.1: Target-specific transactional sync (NEVER mutates any unrelated order!)
        order, pay, outcome_status = sync_single_confirmed_payment_to_order(
            db,
            order_id=payload.order_id,
            payment_id=pay_ref.id,
            merchant_id=principal.merchant_id,
            commerce_session_id=principal.commerce_session_id,
            world_epoch=principal.world_epoch,
            expected_version=payload.expected_version,
            sync_source="FINRESOLVE_RECOVERY_COMMAND",
        )
        return {
            "command": cmd,
            "status": outcome_status,
            "order_id": order.id,
            "order_status": order.status,
            "order_version": order.version,
            "payment_id": pay.id,
            "is_synced_to_order": pay.is_synced_to_order,
            "idempotency_key": payload.idempotency_key,
        }

    if cmd == "REQUEST_EVENT_REDELIVERY":
        # P0.2: Strictly scoped event redelivery requiring explicit event_id or order_id
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
        db.commit()
        return {
            "command": cmd,
            "status": "EXECUTED",
            "redelivered_count": redelivered,
            "event_id": payload.event_id,
            "order_id": payload.order_id,
            "idempotency_key": payload.idempotency_key,
        }

    if cmd == "REQUEST_PAYMENT_RECONCILIATION":
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
            raise CommerceError(ErrorCode.NOT_FOUND, "Order not found in scope", status_code=404)
        if payload.expected_version is not None and recon_order.version != payload.expected_version:
            raise CommerceError(
                ErrorCode.CONFLICT,
                f"VERSION_MISMATCH: expected {payload.expected_version}, got {recon_order.version}",
                status_code=409,
            )
        recon_order.reconciliation_status = ReconciliationStatus.RECONCILIATION_REQUIRED.value
        recon_order.reconciliation_reason = payload.reason
        recon_order.updated_at = now
        if payload.payment_id:
            pay_row = db.get(Payment, payload.payment_id)
            if pay_row is not None and pay_row.order_id == recon_order.id:
                pay_row.reconciliation_required = True
                pay_row.reconciliation_note = payload.reason
        db.commit()
        return {
            "command": cmd,
            "status": "EXECUTED",
            "order_id": recon_order.id,
            "reconciliation_status": recon_order.reconciliation_status,
            "idempotency_key": payload.idempotency_key,
        }

    # REQUEST_MANUAL_REVIEW (pure audit event, zero financial mutation)
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
    db.commit()
    return {
        "command": cmd,
        "status": "ESCALATED_MANUAL_REVIEW",
        "order_id": payload.order_id,
        "payment_id": payload.payment_id,
        "idempotency_key": payload.idempotency_key,
    }
