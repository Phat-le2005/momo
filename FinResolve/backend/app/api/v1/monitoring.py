"""Continuous Commerce Monitoring API routes (Prompt 12 Section 16).

Exposes real-time telemetry, binding configuration, anomaly list with lifecycle timeline,
manual scan trigger, and independent recovery verification — strictly backed by PostgreSQL
and authenticated MiniStore HTTP reads (zero fabricated counts).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.session import get_current_session
from app.db.session import get_db
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.session import DemoSession
from app.models.supervisor import NormalizedEvent
from app.monitoring.service import (
    build_connector_for_binding,
    ensure_commerce_binding,
    run_binding_scan,
    verify_anomaly_recovery,
)
from app.supervisor.service import ensure_supervisor_state

router = APIRouter(prefix="/monitoring", tags=["continuous-monitor"])


class MonitoringBindingRequest(BaseModel):
    base_url: str | None = Field(default=None, max_length=255)
    merchant_id: str | None = Field(default=None, max_length=64)
    commerce_session_id: str | None = Field(default=None, max_length=64)
    commerce_world_epoch: int | None = Field(default=None, ge=1)
    is_enabled: bool = True


class MonitoringBindingResponse(BaseModel):
    id: str
    session_id: str
    world_epoch: int
    merchant_id: str
    commerce_session_id: str
    commerce_world_epoch: int
    base_url: str
    is_enabled: bool
    connectivity_status: str
    last_event_cursor: str | None
    last_heartbeat_at: datetime | None
    last_successful_scan_at: datetime | None
    last_error: str | None
    events_observed_count: int


class MonitoringStatusResponse(BaseModel):
    monitor_enabled_in_config: bool
    monitor_state: str
    connectivity_status: str
    payment_provider_mode: str
    vnpay_status: str
    last_heartbeat_at: datetime | None
    last_successful_scan_at: datetime | None
    last_error: str | None
    events_observed_count: int
    active_anomalies_count: int
    open_investigations_count: int
    verified_recoveries_count: int
    pending_reconciliation_count: int
    bindings: list[MonitoringBindingResponse]


class AnomalyTimelineStage(BaseModel):
    stage: str
    completed: bool
    timestamp: datetime | None = None
    detail: str | None = None


class AnomalyObservationResponse(BaseModel):
    id: str
    fingerprint: str
    detector_code: str
    anomaly_type: str
    severity: str
    lifecycle_state: str
    merchant_id: str
    affected_order_id: str | None
    affected_payment_id: str | None
    rationale: str
    observation_count: int
    first_observed_at: datetime
    last_observed_at: datetime
    linked_case_id: str | None
    linked_job_id: str | None
    recovery_source: str | None
    verified_recovered_at: datetime | None
    evidence_refs: list[dict[str, Any]]
    verification: dict[str, Any]
    timeline: list[AnomalyTimelineStage]


def _binding_to_response(b: CommerceSourceBinding) -> MonitoringBindingResponse:
    return MonitoringBindingResponse(
        id=b.id,
        session_id=b.session_id,
        world_epoch=b.world_epoch,
        merchant_id=b.merchant_id,
        commerce_session_id=b.commerce_session_id,
        commerce_world_epoch=b.commerce_world_epoch,
        base_url=b.base_url,
        is_enabled=b.is_enabled,
        connectivity_status=b.connectivity_status,
        last_event_cursor=b.last_event_cursor,
        last_heartbeat_at=b.last_heartbeat_at,
        last_successful_scan_at=b.last_successful_scan_at,
        last_error=b.last_error,
        events_observed_count=b.events_observed_count,
    )


def _anomaly_to_response(a: AnomalyObservation) -> AnomalyObservationResponse:
    timeline = [
        AnomalyTimelineStage(
            stage="Observed",
            completed=True,
            timestamp=a.first_observed_at,
            detail=f"Observed {a.observation_count}x",
        ),
        AnomalyTimelineStage(
            stage="Detected",
            completed=True,
            timestamp=a.first_observed_at,
            detail=f"{a.detector_code} ({a.anomaly_type})",
        ),
        AnomalyTimelineStage(
            stage="Case Created",
            completed=a.linked_case_id is not None,
            timestamp=a.created_at if a.linked_case_id else None,
            detail=a.linked_case_id,
        ),
        AnomalyTimelineStage(
            stage="Investigating",
            completed=a.lifecycle_state in ("INVESTIGATING", "RECOVERY_PENDING", "RECOVERED"),
            timestamp=a.updated_at if a.linked_job_id else None,
            detail=a.linked_job_id,
        ),
        AnomalyTimelineStage(
            stage="Verified Recovery",
            completed=a.lifecycle_state == "RECOVERED" and a.verified_recovered_at is not None,
            timestamp=a.verified_recovered_at,
            detail=a.recovery_source,
        ),
    ]
    return AnomalyObservationResponse(
        id=a.id,
        fingerprint=a.fingerprint,
        detector_code=a.detector_code,
        anomaly_type=a.anomaly_type,
        severity=a.severity,
        lifecycle_state=a.lifecycle_state,
        merchant_id=a.merchant_id,
        affected_order_id=a.affected_order_id,
        affected_payment_id=a.affected_payment_id,
        rationale=a.rationale,
        observation_count=a.observation_count,
        first_observed_at=a.first_observed_at,
        last_observed_at=a.last_observed_at,
        linked_case_id=a.linked_case_id,
        linked_job_id=a.linked_job_id,
        recovery_source=a.recovery_source,
        verified_recovered_at=a.verified_recovered_at,
        evidence_refs=list(a.evidence_refs_json or []),
        verification=dict(a.verification_json or {}),
        timeline=timeline,
    )


@router.get("/status", response_model=MonitoringStatusResponse)
def get_monitoring_status(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> MonitoringStatusResponse:
    settings = get_settings()
    sup_state = ensure_supervisor_state(db, current_session.id)
    world_epoch = sup_state.world_epoch
    bindings = list(
        db.scalars(
            select(CommerceSourceBinding)
            .where(CommerceSourceBinding.session_id == current_session.id)
            .order_by(CommerceSourceBinding.created_at.asc())
        ).all()
    )

    events_count = int(
        db.scalar(
            select(func.count(NormalizedEvent.id)).where(
                NormalizedEvent.session_id == current_session.id,
                NormalizedEvent.world_epoch == world_epoch,
            )
        )
        or 0
    )
    active_anomalies = int(
        db.scalar(
            select(func.count(AnomalyObservation.id)).where(
                AnomalyObservation.session_id == current_session.id,
                AnomalyObservation.world_epoch == world_epoch,
                AnomalyObservation.lifecycle_state != "RECOVERED",
            )
        )
        or 0
    )
    open_investigations = int(
        db.scalar(
            select(func.count(AnomalyObservation.id)).where(
                AnomalyObservation.session_id == current_session.id,
                AnomalyObservation.world_epoch == world_epoch,
                AnomalyObservation.lifecycle_state == "INVESTIGATING",
            )
        )
        or 0
    )
    verified_recoveries = int(
        db.scalar(
            select(func.count(AnomalyObservation.id)).where(
                AnomalyObservation.session_id == current_session.id,
                AnomalyObservation.world_epoch == world_epoch,
                AnomalyObservation.lifecycle_state == "RECOVERED",
            )
        )
        or 0
    )
    pending_reconciliation = int(
        db.scalar(
            select(func.count(AnomalyObservation.id)).where(
                AnomalyObservation.session_id == current_session.id,
                AnomalyObservation.world_epoch == world_epoch,
                AnomalyObservation.detector_code.in_(["D01", "D02", "D04", "D07"]),
                AnomalyObservation.lifecycle_state != "RECOVERED",
            )
        )
        or 0
    )

    primary = bindings[0] if bindings else None
    if primary is None:
        monitor_state = "UNCONFIGURED"
        conn_status = "UNCONFIGURED"
    elif not primary.is_enabled:
        monitor_state = "PAUSED"
        conn_status = "PAUSED"
    elif primary.connectivity_status == "CONNECTED":
        monitor_state = "ACTIVE"
        conn_status = "CONNECTED"
    else:
        monitor_state = primary.connectivity_status
        conn_status = primary.connectivity_status

    return MonitoringStatusResponse(
        monitor_enabled_in_config=settings.continuous_monitor_enabled,
        monitor_state=monitor_state,
        connectivity_status=conn_status,
        payment_provider_mode="LOCAL_MOCK",
        vnpay_status="SANDBOX_CREDENTIALS_PENDING",
        last_heartbeat_at=primary.last_heartbeat_at if primary else None,
        last_successful_scan_at=primary.last_successful_scan_at if primary else None,
        last_error=primary.last_error if primary else None,
        events_observed_count=events_count,
        active_anomalies_count=active_anomalies,
        open_investigations_count=open_investigations,
        verified_recoveries_count=verified_recoveries,
        pending_reconciliation_count=pending_reconciliation,
        bindings=[_binding_to_response(b) for b in bindings],
    )


@router.get("/anomalies", response_model=list[AnomalyObservationResponse])
def list_monitoring_anomalies(
    lifecycle_state: str | None = Query(default=None, max_length=32),
    detector_code: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=50, ge=1, le=200),
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> list[AnomalyObservationResponse]:
    sup_state = ensure_supervisor_state(db, current_session.id)
    stmt = select(AnomalyObservation).where(
        AnomalyObservation.session_id == current_session.id,
        AnomalyObservation.world_epoch == sup_state.world_epoch,
    )
    if lifecycle_state:
        stmt = stmt.where(AnomalyObservation.lifecycle_state == lifecycle_state.strip().upper())
    if detector_code:
        stmt = stmt.where(AnomalyObservation.detector_code == detector_code.strip().upper())
    rows = list(
        db.scalars(stmt.order_by(AnomalyObservation.last_observed_at.desc()).limit(limit)).all()
    )
    return [_anomaly_to_response(a) for a in rows]


@router.post("/bindings", response_model=MonitoringBindingResponse)
def upsert_monitoring_binding(
    payload: MonitoringBindingRequest,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> MonitoringBindingResponse:
    binding = ensure_commerce_binding(
        db,
        session_id=current_session.id,
        base_url=payload.base_url,
        merchant_id=payload.merchant_id,
        commerce_session_id=payload.commerce_session_id,
        commerce_world_epoch=payload.commerce_world_epoch,
        is_enabled=payload.is_enabled,
    )
    return _binding_to_response(binding)


@router.post("/scan-now")
def trigger_manual_scan(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    bindings = list(
        db.scalars(
            select(CommerceSourceBinding).where(
                CommerceSourceBinding.session_id == current_session.id,
                CommerceSourceBinding.is_enabled.is_(True),
            )
        ).all()
    )
    if not bindings:
        raise AppError(
            ErrorCode.BAD_REQUEST,
            "No enabled CommerceSourceBinding configured for this session",
            status_code=400,
        )
    results = []
    for b in bindings:
        s = run_binding_scan(db, binding=b)
        results.append(
            {
                "binding_id": s.binding_id,
                "connectivity_status": s.connectivity_status,
                "events_pulled": s.events_pulled,
                "events_new": s.events_new,
                "anomalies_detected": s.anomalies_detected,
                "cases_created": s.cases_created,
                "recoveries_verified": s.recoveries_verified,
                "error": s.error,
            }
        )
    return {"scans": results}


@router.post("/anomalies/{anomaly_id}/verify")
def verify_single_anomaly_recovery(
    anomaly_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    obs = db.scalar(
        select(AnomalyObservation).where(
            AnomalyObservation.id == anomaly_id,
            AnomalyObservation.session_id == current_session.id,
        )
    )
    if obs is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Anomaly '{anomaly_id}' not found", status_code=404)
    binding = db.get(CommerceSourceBinding, obs.source_binding_id)
    if binding is None:
        raise AppError(
            ErrorCode.NOT_FOUND, "Associated CommerceSourceBinding not found", status_code=404
        )
    conn = build_connector_for_binding(binding)
    recovered, detail = verify_anomaly_recovery(db, obs=obs, connector=conn)
    return {
        "anomaly_id": obs.id,
        "recovered": recovered,
        "lifecycle_state": obs.lifecycle_state,
        "recovery_source": obs.recovery_source,
        "detail": detail,
        "verification": obs.verification_json,
    }
