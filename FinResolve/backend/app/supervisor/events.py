"""Normalized Business Event contract & hardened durable inbox for Mini Store / VNPAY.

Hard Security & Integration Invariants (Prompt 09.1.1 Section 6):
1. Service-Ingestion Authentication & Provenance:
   - Never trusts caller-asserted `source` or `provenance.signature_verified`.
   - Verified service callers (`X-Service-Token` signed HMAC) get `verified_source` from the
     authenticated principal and may produce `Evidence(kind="OBSERVED_FACT")` (still never
     auto-marking payment verified without ledger reconciliation).
   - Unauthenticated / guest callers are tagged `verified_source="UNTRUSTED_GUEST_CLIENT"` and
     produce `Evidence(kind="CLAIM")` strictly.
2. Durable Event Inbox & Idempotency (`normalized_events`):
   - Unique scope `(session_id, world_epoch, merchant_id, event_id)`.
   - Duplicate event with identical `payload_hash` -> idempotent replay (0 new Cases/jobs).
   - Same `event_id` with different `payload_hash` -> `409 CONFLICT`.
   - Caller `payload` is nested under `"event_payload"` so it can NEVER overwrite canonical
     metadata (`event_id`, `merchant_id`, `verified_source`, `provenance`).
3. Separation of Observation from Investigation:
   - Normal business events (`ORDER_CREATED`, `ORDER_UPDATED`, `PAYMENT_INITIATED`,
     `INVENTORY_UPDATED`) are persisted in `normalized_events` with 0 Cases, 0 jobs, 0 LLM calls.
   - Only anomaly/investigation triggers create 1 Case (respecting `DemoSession.max_cases`
     under `FOR UPDATE`) and enqueue 1 Supervisor job.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.core.session import ServiceIngestionPrincipal
from app.models.cases import Case, CaseEvent, Evidence
from app.models.enums import CaseState, EvidenceKind
from app.models.session import DemoSession
from app.models.supervisor import NormalizedEvent, SupervisorJob
from app.supervisor.service import (
    enqueue_case_investigation_job,
    ensure_supervisor_state,
    record_activity,
)

ALLOWED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})
MAX_EVENT_PAYLOAD_BYTES = 32_768

NORMAL_OBSERVATION_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "ORDER_CREATED",
        "ORDER_UPDATED",
        "PAYMENT_INITIATED",
        "INVENTORY_UPDATED",
    }
)
ANOMALY_INVESTIGATION_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "PAYMENT_SYNC_MISMATCH",
        "REFUND_ANOMALY",
        "DUPLICATE_REFUND_DETECTED",
        "ORDER_STALLED",
        "PAYMENT_DISPUTE_REPORTED",
    }
)
ALLOWED_EVENT_TYPES: frozenset[str] = (
    NORMAL_OBSERVATION_EVENT_TYPES | ANOMALY_INVESTIGATION_EVENT_TYPES
)


class NormalizedBusinessEvent(BaseModel):
    """Canonical event contract for Commerce & Payment integrations (Mini Store + VNPAY)."""

    event_id: str = Field(..., min_length=3, max_length=128)
    event_type: str = Field(..., min_length=3, max_length=64)
    source: str = Field(..., min_length=2, max_length=64)
    source_mode: str = Field(default="MOCK", max_length=32)
    schema_version: str = Field(default="1.0", max_length=16)
    merchant_id: str = Field(default="MERCHANT_MOCK_01", min_length=2, max_length=64)
    session_id: str | None = Field(default=None, max_length=64)
    world_epoch: int | None = Field(default=None, ge=1)
    order_id: str | None = Field(default=None, max_length=64)
    payment_id: str | None = Field(default=None, max_length=64)
    correlation_id: str | None = Field(default=None, max_length=128)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    received_at: datetime | None = Field(default=None)
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class IngestEventResponse(BaseModel):
    event_id: str
    session_id: str
    world_epoch: int
    merchant_id: str
    verified_source: str
    event_type: str
    triggered_investigation: bool
    idempotent_replay: bool = False
    case_id: str | None = None
    job_id: str | None = None
    job_status: str
    source_mode: str


def _compute_canonical_event_hash(event: NormalizedBusinessEvent, norm_type: str) -> str:
    canonical = {
        "event_type": norm_type,
        "schema_version": event.schema_version.strip(),
        "merchant_id": event.merchant_id.strip(),
        "order_id": event.order_id,
        "payment_id": event.payment_id,
        "correlation_id": event.correlation_id,
        "payload": event.payload,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    if len(raw) > MAX_EVENT_PAYLOAD_BYTES:
        raise AppError(
            ErrorCode.BAD_REQUEST,
            f"Event payload exceeds maximum allowed size ({MAX_EVENT_PAYLOAD_BYTES} bytes)",
            status_code=400,
        )
    return hashlib.sha256(raw).hexdigest()


def _replay_or_conflict(
    db: Session,
    *,
    existing: NormalizedEvent,
    payload_hash: str,
) -> IngestEventResponse:
    if existing.payload_hash != payload_hash:
        raise AppError(
            ErrorCode.CONFLICT,
            f"EVENT_ID_PAYLOAD_CONFLICT: event_id '{existing.event_id}' was already ingested "
            "with a different payload",
            status_code=409,
            details={"event_id": existing.event_id, "merchant_id": existing.merchant_id},
        )
    job_status = "OBSERVED_ONLY"
    if existing.linked_job_id:
        job = db.get(SupervisorJob, existing.linked_job_id)
        job_status = job.status if job is not None else "QUEUED"
    return IngestEventResponse(
        event_id=existing.event_id,
        session_id=existing.session_id,
        world_epoch=existing.world_epoch,
        merchant_id=existing.merchant_id,
        verified_source=existing.verified_source,
        event_type=existing.event_type,
        triggered_investigation=existing.triggered_investigation,
        idempotent_replay=True,
        case_id=existing.linked_case_id,
        job_id=existing.linked_job_id,
        job_status=job_status,
        source_mode=existing.source_mode,
    )


def ingest_normalized_business_event(
    db: Session,
    *,
    session_obj: DemoSession,
    event: NormalizedBusinessEvent,
    service_principal: ServiceIngestionPrincipal | None = None,
) -> IngestEventResponse:
    """Validate, deduplicate, and persist a normalized business/payment event in the durable inbox.

    Normal business events are stored as observations (0 Cases, 0 jobs, 0 LLM calls).
    Only anomaly/investigation triggers create 1 Case and enqueue 1 Supervisor job.
    """
    if event.source_mode.strip().upper() != "MOCK":
        raise AppError(
            ErrorCode.BAD_REQUEST,
            "Only source_mode='MOCK' is supported in this milestone",
            status_code=400,
        )
    if event.schema_version.strip() not in ALLOWED_SCHEMA_VERSIONS:
        raise AppError(
            ErrorCode.BAD_REQUEST,
            f"Unsupported schema_version '{event.schema_version}'; allowed: "
            f"{sorted(ALLOWED_SCHEMA_VERSIONS)}",
            status_code=400,
        )
    norm_type = event.event_type.strip().upper()
    if norm_type not in ALLOWED_EVENT_TYPES:
        raise AppError(
            ErrorCode.BAD_REQUEST,
            f"Unsupported event_type '{event.event_type}'; allowed: {sorted(ALLOWED_EVENT_TYPES)}",
            status_code=400,
        )
    if event.session_id and event.session_id != session_obj.id:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "Event session_id does not match authenticated session",
            status_code=403,
        )

    clean_merchant = event.merchant_id.strip()
    if service_principal is not None:
        if service_principal.session_id != session_obj.id:
            raise AppError(
                ErrorCode.FORBIDDEN,
                "Service token session mismatch",
                status_code=403,
            )
        if clean_merchant != service_principal.merchant_id:
            raise AppError(
                ErrorCode.FORBIDDEN,
                f"MERCHANT_SCOPE_MISMATCH: Event merchant_id '{clean_merchant}' does not match "
                f"authenticated service principal merchant '{service_principal.merchant_id}'",
                status_code=403,
            )
        verified_source = service_principal.verified_source
        ev_kind = EvidenceKind.OBSERVED_FACT.value
    else:
        verified_source = "UNTRUSTED_GUEST_CLIENT"
        ev_kind = EvidenceKind.CLAIM.value

    payload_hash = _compute_canonical_event_hash(event, norm_type)

    state = ensure_supervisor_state(db, session_obj.id)
    if event.world_epoch is not None and event.world_epoch != state.world_epoch:
        raise AppError(
            ErrorCode.CONFLICT,
            f"STALE_WORLD_EPOCH: Event world_epoch={event.world_epoch} does not match "
            f"active session world_epoch={state.world_epoch}",
            status_code=409,
            details={
                "event_world_epoch": event.world_epoch,
                "active_world_epoch": state.world_epoch,
            },
        )

    existing = db.scalar(
        select(NormalizedEvent).where(
            NormalizedEvent.session_id == session_obj.id,
            NormalizedEvent.world_epoch == state.world_epoch,
            NormalizedEvent.merchant_id == clean_merchant,
            NormalizedEvent.event_id == event.event_id,
        )
    )
    if existing is not None:
        return _replay_or_conflict(db, existing=existing, payload_hash=payload_hash)

    now = datetime.now(UTC)  # Server-generated received_at (never caller-supplied)
    should_investigate = norm_type in ANOMALY_INVESTIGATION_EVENT_TYPES
    server_provenance: dict[str, Any] = {
        "verified_source": verified_source,
        "claimed_source": event.source,
        "service_authenticated": service_principal is not None,
        "server_received_at": now.isoformat(),
        "caller_provenance": dict(event.provenance),
    }

    case_id: str | None = None
    job_id: str | None = None
    job_status = "OBSERVED_ONLY"

    inbox_row = NormalizedEvent(
        id=f"NEV-{uuid.uuid4().hex[:12].upper()}",
        event_id=event.event_id,
        session_id=session_obj.id,
        world_epoch=state.world_epoch,
        merchant_id=clean_merchant,
        verified_source=verified_source,
        claimed_source=event.source,
        source_mode="MOCK",
        schema_version=event.schema_version.strip(),
        event_type=norm_type,
        order_id=event.order_id,
        payment_id=event.payment_id,
        correlation_id=event.correlation_id,
        payload_hash=payload_hash,
        payload_json=dict(event.payload),
        provenance_json=server_provenance,
        occurred_at=event.occurred_at,
        received_at=now,
        triggered_investigation=should_investigate,
        linked_case_id=None,
        linked_job_id=None,
    )

    try:
        with db.begin_nested():
            db.add(inbox_row)
            db.flush()
    except IntegrityError:
        dup = db.scalar(
            select(NormalizedEvent).where(
                NormalizedEvent.session_id == session_obj.id,
                NormalizedEvent.world_epoch == state.world_epoch,
                NormalizedEvent.merchant_id == clean_merchant,
                NormalizedEvent.event_id == event.event_id,
            )
        )
        if dup is not None:
            return _replay_or_conflict(db, existing=dup, payload_hash=payload_hash)
        raise

    record_activity(
        db,
        session_id=session_obj.id,
        world_epoch=state.world_epoch,
        kind="EVENT_RECEIVED",
        title_vi=f"Tiếp nhận sự kiện {norm_type} từ {verified_source}",
        detail_vi=(
            f"order_id={event.order_id or '—'}, payment_id={event.payment_id or '—'}, "
            f"event_id={event.event_id} "
            f"({'kích hoạt điều tra' if should_investigate else 'lưu quan sát, 0 LLM calls'})"
        ),
        metadata_json={
            "event_id": event.event_id,
            "event_type": norm_type,
            "verified_source": verified_source,
            "claimed_source": event.source,
            "triggered_investigation": should_investigate,
            "correlation_id": event.correlation_id,
        },
    )

    if should_investigate:
        # Enforce atomic session case quota under FOR UPDATE lock
        db.refresh(session_obj, with_for_update=True)
        if session_obj.case_count >= session_obj.max_cases:
            db.rollback()
            raise AppError(
                ErrorCode.RATE_LIMITED,
                f"Session quota exceeded: maximum of {session_obj.max_cases} cases reached",
                status_code=429,
                details={
                    "max_cases": session_obj.max_cases,
                    "case_count": session_obj.case_count,
                },
            )

        case_id = f"CASE-{uuid.uuid4().hex[:10].upper()}"
        case_type = "REFUND_REQUEST" if "REFUND" in norm_type else "PAYMENT_DISPUTE"
        goal = (
            f"Điều tra sự kiện {norm_type} "
            f"(order={event.order_id or 'N/A'}, payment={event.payment_id or 'N/A'})"
        )
        case_obj = Case(
            id=case_id,
            session_id=session_obj.id,
            type=case_type,
            goal=goal,
            state=CaseState.NEW.value,
            version=1,
            owner="supervisor",
            created_at=now,
            updated_at=now,
        )
        db.add(case_obj)

        # Keep canonical metadata strictly separate from caller payload (no key overwriting!)
        evd = Evidence(
            id=f"EVD-{uuid.uuid4().hex[:10].upper()}",
            case_id=case_id,
            session_id=session_obj.id,
            kind=ev_kind,
            source=verified_source,
            source_ref=f"event:{event.event_id}",
            payload_json={
                "event_id": event.event_id,
                "event_type": norm_type,
                "schema_version": event.schema_version.strip(),
                "merchant_id": clean_merchant,
                "verified_source": verified_source,
                "claimed_source": event.source,
                "order_id": event.order_id,
                "payment_id": event.payment_id,
                "correlation_id": event.correlation_id,
                "occurred_at": event.occurred_at.isoformat(),
                "received_at": now.isoformat(),
                "provenance": server_provenance,
                "event_payload": dict(event.payload),
                "text": str(
                    event.payload.get("text")
                    or f"Sự kiện {norm_type} trên đơn {event.order_id or 'N/A'} "
                    f"(mã GD {event.payment_id or 'N/A'})"
                ),
            },
            confidence=1.0 if ev_kind == EvidenceKind.OBSERVED_FACT.value else None,
            observed_at=event.occurred_at,
        )
        db.add(evd)
        db.add(
            CaseEvent(
                id=f"EVT-{uuid.uuid4().hex[:10].upper()}",
                case_id=case_id,
                session_id=session_obj.id,
                type="CASE_CREATED",
                payload_json={
                    "initial_state": CaseState.NEW.value,
                    "event_id": event.event_id,
                    "verified_source": verified_source,
                    "evidence_kind": ev_kind,
                },
                created_at=now,
            )
        )
        session_obj.case_count += 1
        session_obj.updated_at = now
        db.flush()

        record_activity(
            db,
            session_id=session_obj.id,
            world_epoch=state.world_epoch,
            case_id=case_id,
            kind="CASE_CREATED",
            title_vi=f"Tạo hồ sơ {case_id} từ sự kiện {norm_type}",
            detail_vi=goal,
            metadata_json={"case_id": case_id, "event_id": event.event_id},
        )

        job = enqueue_case_investigation_job(
            db,
            session_id=session_obj.id,
            case_id=case_id,
            reason_vi=f"Xếp hàng đợi điều tra tự động cho sự kiện {norm_type}",
            payload_extra={
                "event_id": event.event_id,
                "correlation_id": event.correlation_id,
                "verified_source": verified_source,
            },
        )
        job_id = job.id
        job_status = job.status
        inbox_row.linked_case_id = case_id
        inbox_row.linked_job_id = job_id

    db.commit()

    return IngestEventResponse(
        event_id=event.event_id,
        session_id=session_obj.id,
        world_epoch=state.world_epoch,
        merchant_id=clean_merchant,
        verified_source=verified_source,
        event_type=norm_type,
        triggered_investigation=should_investigate,
        idempotent_replay=False,
        case_id=case_id,
        job_id=job_id,
        job_status=job_status,
        source_mode="MOCK",
    )
