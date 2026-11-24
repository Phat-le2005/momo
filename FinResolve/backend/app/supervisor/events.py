"""Normalized Business Event contract & ingestion boundary for Mini Store / VNPAY (Prompt 09.1).

Keeps Supervisor job submission completely independent of Challenge Lab so future Commerce
and Payment adapters feed directly into the same durable PostgreSQL orchestration queue.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.models.cases import Case, CaseEvent, Evidence
from app.models.enums import CaseState, EvidenceKind
from app.models.session import DemoSession
from app.models.supervisor import SupervisorJob
from app.supervisor.service import (
    enqueue_case_investigation_job,
    ensure_supervisor_state,
    record_activity,
)


class NormalizedBusinessEvent(BaseModel):
    """Canonical event contract for Commerce & Payment integrations (Mini Store + VNPAY)."""

    event_id: str = Field(..., min_length=3, max_length=128)
    event_type: str = Field(..., min_length=3, max_length=64)
    source: str = Field(..., min_length=2, max_length=64)
    source_mode: str = Field(default="MOCK", max_length=32)
    schema_version: str = Field(default="1.0", max_length=16)
    merchant_id: str = Field(default="MERCHANT_MOCK_01", max_length=64)
    session_id: str | None = Field(default=None, max_length=64)
    world_epoch: int | None = Field(default=None, ge=1)
    order_id: str | None = Field(default=None, max_length=64)
    payment_id: str | None = Field(default=None, max_length=64)
    correlation_id: str | None = Field(default=None, max_length=128)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class IngestEventResponse(BaseModel):
    event_id: str
    session_id: str
    world_epoch: int
    case_id: str
    job_id: str
    job_status: str
    source_mode: str


def ingest_normalized_business_event(
    db: Session,
    *,
    session_obj: DemoSession,
    event: NormalizedBusinessEvent,
) -> IngestEventResponse:
    """Persist a normalized business/payment event, create a Case, and enqueue a Supervisor job."""
    if event.source_mode.strip().upper() != "MOCK":
        raise AppError(
            ErrorCode.BAD_REQUEST,
            "Only source_mode='MOCK' is supported in this milestone",
            status_code=400,
        )
    if event.session_id and event.session_id != session_obj.id:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "Event session_id does not match authenticated session",
            status_code=403,
        )

    state = ensure_supervisor_state(db, session_obj.id)
    now = datetime.now(UTC)

    record_activity(
        db,
        session_id=session_obj.id,
        world_epoch=state.world_epoch,
        kind="EVENT_RECEIVED",
        title_vi=f"Tiếp nhận sự kiện {event.event_type} từ {event.source}",
        detail_vi=(
            f"order_id={event.order_id or '—'}, payment_id={event.payment_id or '—'}, "
            f"event_id={event.event_id}"
        ),
        metadata_json={
            "event_id": event.event_id,
            "event_type": event.event_type,
            "source": event.source,
            "correlation_id": event.correlation_id,
        },
    )

    case_id = f"CASE-{uuid.uuid4().hex[:10].upper()}"
    case_type = "REFUND_REQUEST" if "REFUND" in event.event_type.upper() else "PAYMENT_DISPUTE"
    goal = (
        f"Điều tra sự kiện {event.event_type} "
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

    ev_kind = (
        EvidenceKind.CLAIM.value
        if event.source.upper() in ("CUSTOMER_MESSAGE", "RECEIPT_UPLOAD", "USER_CLAIM")
        else EvidenceKind.OBSERVED_FACT.value
    )
    evd = Evidence(
        id=f"EVD-{uuid.uuid4().hex[:10].upper()}",
        case_id=case_id,
        session_id=session_obj.id,
        kind=ev_kind,
        source=event.source,
        source_ref=f"event:{event.event_id}",
        payload_json={
            "event_id": event.event_id,
            "event_type": event.event_type,
            "schema_version": event.schema_version,
            "merchant_id": event.merchant_id,
            "order_id": event.order_id,
            "payment_id": event.payment_id,
            "correlation_id": event.correlation_id,
            "occurred_at": event.occurred_at.isoformat(),
            "received_at": event.received_at.isoformat(),
            "provenance": event.provenance,
            **event.payload,
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
                "source": event.source,
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
        title_vi=f"Tạo hồ sơ {case_id} từ sự kiện {event.event_type}",
        detail_vi=goal,
        metadata_json={"case_id": case_id, "event_id": event.event_id},
    )

    job: SupervisorJob = enqueue_case_investigation_job(
        db,
        session_id=session_obj.id,
        case_id=case_id,
        reason_vi=f"Xếp hàng đợi điều tra tự động cho sự kiện {event.event_type}",
        payload_extra={"event_id": event.event_id, "correlation_id": event.correlation_id},
    )
    db.commit()

    return IngestEventResponse(
        event_id=event.event_id,
        session_id=session_obj.id,
        world_epoch=state.world_epoch,
        case_id=case_id,
        job_id=job.id,
        job_status=job.status,
        source_mode="MOCK",
    )
