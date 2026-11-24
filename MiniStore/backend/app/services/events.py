"""Atomic CommerceEvent + EventOutbox persistence and explicit FinResolve event mapping.

Contract alignment with `FinResolve/docs/EVENT_INTEGRATION_CONTRACT.md`:
- FinResolve's Event Inbox currently accepts a strict allowlist of normal observation events:
  `ORDER_CREATED`, `ORDER_UPDATED`, `PAYMENT_INITIATED`, `INVENTORY_UPDATED`.
- Other commerce domain events (`PAYMENT_SUCCEEDED`, `PAYMENT_FAILED`, `INVENTORY_RESERVED`,
  `INVENTORY_RELEASED`, `REFUND_REQUESTED`, `REFUND_COMPLETED`, `COMPLAINT_SUBMITTED`,
  `LIVE_SESSION_STARTED`, `FLASH_SALE_STARTED`) are persisted durably in `commerce_events`
  and `event_outbox` with `status='SKIPPED_UNSUPPORTED'` so they are preserved for Prompt 12
  without weakening FinResolve's inbox or triggering false Cases.
- Commerce NEVER creates FinResolve Cases directly and NEVER depends on FinResolve being online.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.entities import CommerceEvent, CommerceSession, EventOutbox
from app.models.enums import OutboxStatus

# Explicit mapping from Commerce event types to FinResolve `/api/v1/events/ingest` normal events
FINRESOLVE_SUPPORTED_EVENT_MAP: dict[str, str] = {
    "ORDER_CREATED": "ORDER_CREATED",
    "ORDER_UPDATED": "ORDER_UPDATED",
    "PAYMENT_INITIATED": "PAYMENT_INITIATED",
    "INVENTORY_UPDATED": "INVENTORY_UPDATED",
}


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

    ev = CommerceEvent(
        id=f"EV-{uuid.uuid4().hex[:12].upper()}",
        event_id=stable_event_id,
        event_type=norm_type,
        source="MINI_STORE_COMMERCE",
        schema_version="1.0",
        source_mode="MOCK",
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
            "adapter_version": "1.0",
            **(provenance or {}),
        },
    )
    db.add(ev)
    db.flush()

    mapped_type = FINRESOLVE_SUPPORTED_EVENT_MAP.get(norm_type)
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
        next_attempt_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(outbox)
    db.flush()
    return ev, outbox


def dispatch_pending_outbox_events(db: Session, *, limit: int = 20) -> int:
    """Optionally deliver supported outbox events to FinResolve when configured.

    If `COMMERCE_FINRESOLVE_API_BASE_URL` is empty or FinResolve is unreachable, events stay
    safely queued with bounded retry backoff and Commerce operations continue unaffected.
    """
    settings = get_settings()
    base_url = settings.finresolve_api_base_url.strip().rstrip("/")
    sess_tok = settings.finresolve_session_token.get_secret_value().strip()
    svc_tok = settings.finresolve_service_token.get_secret_value().strip()
    if not base_url or not sess_tok:
        return 0

    now = datetime.now(UTC)
    rows = list(
        db.scalars(
            select(EventOutbox)
            .where(
                EventOutbox.status == OutboxStatus.PENDING.value,
                EventOutbox.next_attempt_at <= now,
            )
            .order_by(EventOutbox.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
    )
    delivered = 0
    for row in rows:
        ev = db.get(CommerceEvent, row.commerce_event_id)
        if ev is None or not row.finresolve_mapped_type:
            row.status = OutboxStatus.SKIPPED_UNSUPPORTED.value
            continue
        headers: dict[str, str] = {"X-Demo-Session": sess_tok}
        if svc_tok:
            headers["X-Service-Token"] = svc_tok
        body = {
            "event_id": ev.event_id,
            "event_type": row.finresolve_mapped_type,
            "source": ev.source,
            "source_mode": ev.source_mode,
            "schema_version": ev.schema_version,
            "merchant_id": ev.merchant_id,
            "world_epoch": ev.world_epoch,
            "order_id": ev.order_id,
            "payment_id": ev.payment_id,
            "correlation_id": ev.correlation_id,
            "occurred_at": ev.occurred_at.isoformat(),
            "payload": ev.payload_json,
            "provenance": ev.provenance_json,
        }
        row.attempts += 1
        try:
            with httpx.Client(timeout=4.0) as client:
                resp = client.post(f"{base_url}/api/v1/events/ingest", headers=headers, json=body)
            if resp.status_code in (200, 202):
                row.status = OutboxStatus.DELIVERED.value
                row.delivered_at = datetime.now(UTC)
                row.last_error = None
                delivered += 1
            else:
                row.last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                row.next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=min(300, 5 * (2 ** min(row.attempts, 5)))
                )
        except Exception as exc:  # noqa: BLE001
            row.last_error = str(exc)[:300]
            row.next_attempt_at = datetime.now(UTC) + timedelta(
                seconds=min(300, 5 * (2 ** min(row.attempts, 5)))
            )
        row.updated_at = datetime.now(UTC)

    db.commit()
    return delivered
