"""Atomic CommerceEvent + EventOutbox persistence and hardened two-phase FinResolve delivery.

Contract alignment with `FinResolve/docs/EVENT_INTEGRATION_CONTRACT.md` (Prompt 10.1):
- FinResolve's Event Inbox currently accepts a strict allowlist of normal observation events:
  `ORDER_CREATED`, `ORDER_UPDATED`, `PAYMENT_INITIATED`, `INVENTORY_UPDATED`.
- Other commerce domain events (`PAYMENT_SUCCEEDED`, `PAYMENT_FAILED`, `INVENTORY_RESERVED`,
  `INVENTORY_RELEASED`, `REFUND_REQUESTED`, `REFUND_COMPLETED`, `COMPLAINT_SUBMITTED`,
  `LIVE_SESSION_STARTED`, `FLASH_SALE_STARTED`, `PAYMENT_RECONCILIATION_REQUIRED`,
  `INVENTORY_INCONSISTENCY_DETECTED`) are persisted durably in `commerce_events` and
  `event_outbox` with `status='SKIPPED_UNSUPPORTED'` so they are preserved for Prompt 12
  without weakening FinResolve's inbox or triggering false Cases.
- Outbox dispatch uses a strict two-phase claim/deliver/finalize pattern:
  - Phase 1 (short DB txn): claim bounded batch with `FOR UPDATE SKIP LOCKED` + lease token,
    then commit and release locks BEFORE any external HTTP call.
  - Phase 2 (no DB txn held): HTTP POST to FinResolve `/api/v1/events/ingest` requiring BOTH
    `X-Demo-Session` AND `X-Service-Token`.
  - Phase 3 (short DB txn): verify `claim_token`, mark `DELIVERED`, `DEAD_LETTER` (permanent
    auth/contract errors), `REVIEW_REQUIRED` (stale world epoch), or retry with backoff.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import CommerceEvent, CommerceSession, EventOutbox
from app.models.enums import OutboxFailureCategory, OutboxStatus

FINRESOLVE_SUPPORTED_EVENT_MAP: dict[str, str] = {
    "ORDER_CREATED": "ORDER_CREATED",
    "ORDER_UPDATED": "ORDER_UPDATED",
    "PAYMENT_INITIATED": "PAYMENT_INITIATED",
    "INVENTORY_UPDATED": "INVENTORY_UPDATED",
}


@dataclass(frozen=True)
class _ClaimedOutboxItem:
    outbox_id: str
    claim_token: str
    event_id: str
    mapped_type: str
    source: str
    source_mode: str
    schema_version: str
    merchant_id: str
    world_epoch: int
    order_id: str | None
    payment_id: str | None
    correlation_id: str | None
    occurred_at_iso: str
    payload_json: dict[str, Any]
    provenance_json: dict[str, Any]


def record_commerce_event(
    db: Session,
    *,
    merchant_id: str,
    commerce_session_id: str,
    world_epoch: int,
    event_type: str,
    order_id: str | None = None,
    payment_id: str | None = None,
    correlation_id: str | None = None,
    payload: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    finresolve_session_id: str | None = None,
    event_id: str | None = None,
) -> tuple[CommerceEvent, EventOutbox]:
    """Persist a durable `CommerceEvent` and its `EventOutbox` record inside the caller's txn."""
    now = datetime.now(UTC)
    norm_type = event_type.strip().upper()
    stable_event_id = event_id or f"CEVT-{uuid.uuid4().hex[:14].upper()}"

    if finresolve_session_id is None:
        csess = db.get(CommerceSession, commerce_session_id)
        if csess is not None:
            finresolve_session_id = csess.finresolve_session_id

    # Derive truthful source_mode provenance (never mislabel VNPAY_SANDBOX/TEST_FIXTURE as MOCK)
    derived_source_mode = (
        str(
            (provenance or {}).get("source_mode")
            or (payload or {}).get("provenance")
            or (payload or {}).get("provider_environment")
            or ""
        )
        .strip()
        .upper()
    )
    if derived_source_mode not in (
        "MOCK",
        "SANDBOX",
        "TEST_FIXTURE",
        "VNPAY_SANDBOX",
        "PRODUCTION",
    ):
        if payment_id:
            from app.models.entities import Payment

            pay_row = db.get(Payment, payment_id)
            if pay_row is not None and pay_row.provider_environment:
                derived_source_mode = pay_row.provider_environment.strip().upper()
    if derived_source_mode not in (
        "MOCK",
        "SANDBOX",
        "TEST_FIXTURE",
        "VNPAY_SANDBOX",
        "PRODUCTION",
    ):
        derived_source_mode = get_settings().source_mode.strip().upper() or "MOCK"

    ev = CommerceEvent(
        id=f"EV-{uuid.uuid4().hex[:12].upper()}",
        event_id=stable_event_id,
        event_type=norm_type,
        source="MINI_STORE_COMMERCE",
        schema_version="1.0",
        source_mode=derived_source_mode,
        merchant_id=merchant_id,
        commerce_session_id=commerce_session_id,
        finresolve_session_id=finresolve_session_id,
        world_epoch=world_epoch,
        order_id=order_id,
        payment_id=payment_id,
        correlation_id=correlation_id or f"CORR-{uuid.uuid4().hex[:10].upper()}",
        occurred_at=now,
        payload_json=dict(payload or {}),
        provenance_json={
            "source_system": "FINSHOP_LIVE_COMMERCE",
            "adapter_version": "1.1",
            **(provenance or {}),
        },
    )
    db.add(ev)
    db.flush()

    mapped_type = FINRESOLVE_SUPPORTED_EVENT_MAP.get(norm_type)
    target_epoch = get_settings().finresolve_target_world_epoch
    outbox = EventOutbox(
        id=f"OUT-{uuid.uuid4().hex[:12].upper()}",
        commerce_event_id=ev.id,
        event_id=stable_event_id,
        event_type=norm_type,
        finresolve_mapped_type=mapped_type,
        status=(
            OutboxStatus.PENDING.value
            if mapped_type is not None
            else OutboxStatus.SKIPPED_UNSUPPORTED.value
        ),
        attempts=0,
        target_finresolve_world_epoch=target_epoch,
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(outbox)
    db.flush()
    return ev, outbox


def _worker_identity() -> str:
    return f"{socket.gethostname()[:24]}:{os.getpid()}"


def _claim_outbox_batch(
    db: Session,
    *,
    limit: int,
    worker_id: str,
    lease_seconds: int,
) -> list[_ClaimedOutboxItem]:
    """Phase 1: Claim a bounded batch of PENDING rows with a lease and release DB locks."""
    now = datetime.now(UTC)
    lease_until = now + timedelta(seconds=lease_seconds)
    rows = list(
        db.scalars(
            select(EventOutbox)
            .where(
                EventOutbox.status == OutboxStatus.PENDING.value,
                EventOutbox.next_attempt_at <= now,
                or_(EventOutbox.locked_until.is_(None), EventOutbox.locked_until <= now),
            )
            .order_by(EventOutbox.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
    )
    claimed: list[_ClaimedOutboxItem] = []
    for row in rows:
        ev = db.get(CommerceEvent, row.commerce_event_id)
        if ev is None or not row.finresolve_mapped_type:
            row.status = OutboxStatus.SKIPPED_UNSUPPORTED.value
            row.updated_at = now
            continue
        token = uuid.uuid4().hex
        row.claim_token = token
        row.claimed_by = worker_id
        row.locked_until = lease_until
        row.updated_at = now
        effective_epoch = row.target_finresolve_world_epoch or ev.world_epoch
        claimed.append(
            _ClaimedOutboxItem(
                outbox_id=row.id,
                claim_token=token,
                event_id=ev.event_id,
                mapped_type=row.finresolve_mapped_type,
                source=ev.source,
                source_mode=ev.source_mode,
                schema_version=ev.schema_version,
                merchant_id=ev.merchant_id,
                world_epoch=effective_epoch,
                order_id=ev.order_id,
                payment_id=ev.payment_id,
                correlation_id=ev.correlation_id,
                occurred_at_iso=ev.occurred_at.isoformat(),
                payload_json=dict(ev.payload_json or {}),
                provenance_json=dict(ev.provenance_json or {}),
            )
        )
    db.commit()
    return claimed


def dispatch_pending_outbox_events(
    db: Session,
    *,
    limit: int = 20,
    worker_id: str | None = None,
) -> int:
    """Deliver supported outbox events to FinResolve using two-phase lease processing.

    Requires BOTH `COMMERCE_FINRESOLVE_SESSION_TOKEN` AND `COMMERCE_FINRESOLVE_SERVICE_TOKEN`
    alongside `COMMERCE_FINRESOLVE_API_BASE_URL`. If any is missing, events stay safely queued
    (`PENDING`) and Commerce operations continue unaffected.
    """
    settings = get_settings()
    base_url = settings.finresolve_api_base_url.strip().rstrip("/")
    sess_tok = settings.finresolve_session_token.get_secret_value().strip()
    svc_tok = settings.finresolve_service_token.get_secret_value().strip()
    if not base_url or not sess_tok or not svc_tok:
        return 0

    wid = worker_id or _worker_identity()
    claimed = _claim_outbox_batch(
        db,
        limit=limit,
        worker_id=wid,
        lease_seconds=settings.outbox_lease_seconds,
    )
    if not claimed:
        return 0

    delivered = 0
    headers = {
        "X-Demo-Session": sess_tok,
        "X-Service-Token": svc_tok,
    }
    max_attempts = settings.outbox_max_attempts

    for item in claimed:
        # Phase 2: External HTTP call with ZERO database row locks held
        body = {
            "event_id": item.event_id,
            "event_type": item.mapped_type,
            "source": item.source,
            "source_mode": item.source_mode,
            "schema_version": item.schema_version,
            "merchant_id": item.merchant_id,
            "world_epoch": item.world_epoch,
            "order_id": item.order_id,
            "payment_id": item.payment_id,
            "correlation_id": item.correlation_id,
            "occurred_at": item.occurred_at_iso,
            "payload": item.payload_json,
            "provenance": item.provenance_json,
        }
        status_code: int | None = None
        resp_text = ""
        retry_after_header: str | None = None
        err_msg: str | None = None
        try:
            with httpx.Client(timeout=4.0) as client:
                resp = client.post(f"{base_url}/api/v1/events/ingest", headers=headers, json=body)
            status_code = resp.status_code
            resp_text = resp.text[:300]
            retry_after_header = resp.headers.get("Retry-After")
        except Exception as exc:  # noqa: BLE001
            err_msg = str(exc)[:300]

        # Phase 3: Short DB transaction verifying claim_token before updating status
        row = db.scalar(
            select(EventOutbox)
            .where(
                EventOutbox.id == item.outbox_id,
                EventOutbox.claim_token == item.claim_token,
            )
            .with_for_update()
        )
        if row is None:
            db.rollback()
            continue

        now = datetime.now(UTC)
        row.attempts += 1
        row.claim_token = None
        row.claimed_by = None
        row.locked_until = None
        row.updated_at = now

        if status_code in (200, 202):
            row.status = OutboxStatus.DELIVERED.value
            row.delivered_at = now
            row.last_error = None
            row.failure_category = None
            delivered += 1
        elif status_code in (401, 403):
            row.status = OutboxStatus.DEAD_LETTER.value
            row.failure_category = OutboxFailureCategory.PERMANENT_AUTH.value
            row.last_error = f"HTTP {status_code} (permanent auth failure): {resp_text[:200]}"
        elif status_code == 409 and ("world" in resp_text.lower() or "epoch" in resp_text.lower()):
            row.status = OutboxStatus.REVIEW_REQUIRED.value
            row.failure_category = OutboxFailureCategory.STALE_WORLD.value
            row.last_error = f"HTTP 409 (stale world_epoch): {resp_text[:200]}"
        elif status_code == 429:
            # HTTP 429 is transient rate limiting — honor valid Retry-After with bounded retry!
            row.failure_category = OutboxFailureCategory.TRANSIENT.value
            row.last_error = f"HTTP 429 (rate limited): {resp_text[:200]}"
            if row.attempts >= max_attempts:
                row.status = OutboxStatus.DEAD_LETTER.value
            else:
                backoff_s = min(300, 5 * (2 ** min(row.attempts, 5)))
                if retry_after_header and retry_after_header.strip().isdigit():
                    backoff_s = max(1, min(300, int(retry_after_header.strip())))
                row.next_attempt_at = now + timedelta(seconds=backoff_s)
        elif status_code is not None and 400 <= status_code < 500:
            row.status = OutboxStatus.DEAD_LETTER.value
            row.failure_category = OutboxFailureCategory.PERMANENT_CONTRACT.value
            row.last_error = f"HTTP {status_code} (permanent contract failure): {resp_text[:200]}"
        else:
            row.failure_category = OutboxFailureCategory.TRANSIENT.value
            row.last_error = (
                f"HTTP {status_code}: {resp_text[:200]}"
                if status_code is not None
                else (err_msg or "Network delivery error")
            )
            if row.attempts >= max_attempts:
                row.status = OutboxStatus.DEAD_LETTER.value
            else:
                backoff_s = min(300, 5 * (2 ** min(row.attempts, 5)))
                row.next_attempt_at = now + timedelta(seconds=backoff_s)

        db.commit()

    return delivered
