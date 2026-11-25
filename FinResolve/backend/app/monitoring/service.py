"""Continuous Commerce Monitor Core Service (Prompt 12 + Prompt 12.1 Hardening).

Hardened Invariants:
- P0: Bindings use server-registered `connector_id` and approved URLs only; historical bindings
  referenced by Cases cannot silently mutate scope or destination.
- P1: Full multi-page event collection within bounded work budgets, unified push/pull canonical
  event hashing (`compute_canonical_event_hash`), payload conflict detection, and continuation.
- P1: D06 missing-event detection runs against prior inbox state BEFORE ingesting pulled events,
  only for push-expected event types, then recovers & verifies in the same scan.
- P1: True lease & world-epoch fencing (`_assert_binding_lease_in_txn`) inside EVERY authoritative
  write transaction so a stale worker that lost ownership can NEVER commit checkpoints, anomalies,
  Cases, or recovery transitions.
- P1: Strict detector-specific recovery verification with explicit 5-state verdicts
  (`VERIFIED_RECOVERY`, `STILL_INCONSISTENT`, `INSUFFICIENT_EVIDENCE`, `SOURCE_UNAVAILABLE`,
  `UNKNOWN_OUTCOME`) and evidence-backed actor attribution (default `"UNKNOWN"`).
- P2: Multi-page order/payment scanning beyond 200 records + recurrence-aware anomaly reopening
  preserving prior recovery history.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.connectors.commerce import (
    CommerceConnectorError,
    CommerceReadConnector,
    resolve_registered_connector_url,
    validate_approved_commerce_url,
)
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.models.cases import Case, CaseEvent, Evidence
from app.models.enums import CaseState, EvidenceKind
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.session import DemoSession
from app.models.supervisor import NormalizedEvent, SupervisorJob
from app.monitoring.detectors import (
    CommerceSnapshot,
    DetectedAnomaly,
    detect_d06_missing_event_delivery,
    run_all_detectors,
)
from app.supervisor.service import (
    enqueue_case_investigation_job,
    ensure_supervisor_state,
    record_activity,
)

OVERLAP_WINDOW_SECONDS = 5


class StaleLeaseError(RuntimeError):
    """Raised when a monitor worker attempts an authoritative write after losing its lease."""


class RecoveryVerdict(StrEnum):
    VERIFIED_RECOVERY = "VERIFIED_RECOVERY"
    STILL_INCONSISTENT = "STILL_INCONSISTENT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


@dataclass
class ScanSummary:
    binding_id: str
    connectivity_status: str
    events_pulled: int = 0
    events_new: int = 0
    events_conflicts: int = 0
    events_scan_complete: bool = True
    anomalies_detected: int = 0
    cases_created: int = 0
    recoveries_verified: int = 0
    orders_scanned: int = 0
    payments_scanned: int = 0
    error: str | None = None


@dataclass
class VerificationResult:
    verdict: RecoveryVerdict
    recovered: bool
    recovery_source: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


def compute_canonical_event_hash(
    *,
    event_id: str,
    event_type: str,
    merchant_id: str,
    order_id: str | None,
    payment_id: str | None,
    payload: dict[str, Any] | None,
) -> str:
    """Unified canonical payload hash shared across push and pull ingestion channels."""
    canonical = {
        "event_id": event_id.strip(),
        "event_type": event_type.strip().upper(),
        "merchant_id": merchant_id.strip(),
        "order_id": order_id,
        "payment_id": payment_id,
        "payload": payload or {},
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _assert_binding_lease_in_txn(
    db: Session,
    *,
    binding_id: str,
    lease_token: str | None,
    expected_world_epoch: int | None,
) -> CommerceSourceBinding:
    """Lock `CommerceSourceBinding` and verify active lease token & world epoch inside a txn."""
    locked = db.scalar(
        select(CommerceSourceBinding)
        .where(CommerceSourceBinding.id == binding_id)
        .with_for_update()
    )
    if locked is None:
        raise StaleLeaseError(f"Binding '{binding_id}' no longer exists")
    if expected_world_epoch is not None and locked.world_epoch != expected_world_epoch:
        raise StaleLeaseError(
            f"World epoch mismatch on binding '{binding_id}': "
            f"expected {expected_world_epoch}, got {locked.world_epoch}"
        )
    if lease_token is not None:
        if locked.lease_token != lease_token:
            raise StaleLeaseError(
                f"Lease token mismatch on binding '{binding_id}': worker lost ownership"
            )
        now = datetime.now(UTC)
        if locked.locked_until is not None and locked.locked_until <= now:
            raise StaleLeaseError(f"Lease expired on binding '{binding_id}'")
    return locked


def ensure_commerce_binding(
    db: Session,
    *,
    session_id: str,
    connector_id: str | None = None,
    base_url: str | None = None,
    merchant_id: str | None = None,
    commerce_session_id: str | None = None,
    commerce_world_epoch: int | None = None,
    is_enabled: bool = True,
) -> CommerceSourceBinding:
    """Create or update a session's `CommerceSourceBinding` using approved connector destinations.

    Security invariants:
    - Always validates destination via `validate_approved_commerce_url` / registered connector.
    - Prevents silently mutating destination/scope of a binding referenced by historical Cases.
    """
    settings = get_settings()
    sup_state = ensure_supervisor_state(db, session_id)
    world_epoch = sup_state.world_epoch
    mid = (merchant_id or settings.commerce_merchant_id).strip()
    csid = (commerce_session_id or settings.commerce_session_id).strip()
    cid = (connector_id or settings.commerce_connector_id).strip()

    if base_url is not None and base_url.strip():
        url = validate_approved_commerce_url(base_url)
    elif settings.commerce_service_base_url.strip():
        url = resolve_registered_connector_url(cid)
    else:
        url = ""

    c_epoch = commerce_world_epoch or settings.commerce_world_epoch

    existing = db.scalar(
        select(CommerceSourceBinding).where(
            CommerceSourceBinding.session_id == session_id,
            CommerceSourceBinding.merchant_id == mid,
            CommerceSourceBinding.commerce_session_id == csid,
        )
    )
    now = datetime.now(UTC)
    if existing is not None:
        # Guard historical Cases against silent binding destination/scope mutation
        linked_cases = int(
            db.scalar(select(func.count(Case.id)).where(Case.source_binding_id == existing.id)) or 0
        )
        if linked_cases > 0 and url and existing.base_url and url != existing.base_url:
            raise AppError(
                ErrorCode.CONFLICT,
                "BINDING_IMMUTABLE_WITH_HISTORICAL_CASES: cannot change destination URL "
                "of a binding referenced by existing Cases",
                status_code=409,
            )
        if url:
            existing.base_url = url
        existing.connector_id = cid
        existing.world_epoch = world_epoch
        existing.commerce_world_epoch = c_epoch
        existing.is_enabled = is_enabled
        existing.world_reset_required = False
        if not is_enabled:
            existing.connectivity_status = "PAUSED"
        elif existing.connectivity_status in ("PAUSED", "WORLD_RESET_REQUIRED"):
            existing.connectivity_status = "UNCONFIGURED" if not existing.base_url else "CONNECTED"
        existing.updated_at = now
        db.commit()
        db.refresh(existing)
        return existing

    binding = CommerceSourceBinding(
        id=f"CBIND-{uuid.uuid4().hex[:10].upper()}",
        session_id=session_id,
        world_epoch=world_epoch,
        merchant_id=mid,
        commerce_session_id=csid,
        commerce_world_epoch=c_epoch,
        connector_id=cid,
        base_url=url,
        is_enabled=is_enabled,
        world_reset_required=False,
        connectivity_status="PAUSED"
        if not is_enabled
        else ("CONNECTED" if url else "UNCONFIGURED"),
        created_at=now,
        updated_at=now,
    )
    db.add(binding)
    db.commit()
    db.refresh(binding)
    return binding


def build_connector_for_binding(
    binding: CommerceSourceBinding,
    *,
    service_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> CommerceReadConnector:
    settings = get_settings()
    key = (
        service_key if service_key is not None else settings.commerce_service_key.get_secret_value()
    )
    return CommerceReadConnector(
        base_url=binding.base_url,
        service_key=key,
        merchant_id=binding.merchant_id,
        commerce_session_id=binding.commerce_session_id,
        world_epoch=binding.commerce_world_epoch,
        timeout_seconds=settings.commerce_connector_timeout_seconds,
        transport=transport,
    )


def _fetch_remote_events_paginated(
    binding: CommerceSourceBinding,
    connector: CommerceReadConnector,
    *,
    batch_size: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], bool, str | None, datetime | None, bool]:
    """Fetch multiple pages of remote Commerce events within bounded work budget.

    Returns `(events, scan_complete, next_cursor, latest_occurred, cursor_was_reset)`.
    """
    collected: list[dict[str, Any]] = []
    seen_row_ids: set[str] = set()
    cursor: str | None = binding.last_event_cursor
    latest_occurred: datetime | None = binding.last_event_occurred_at
    cursor_was_reset = False

    # First page uses bounded overlap window when we have a prior timestamp (catches late commits),
    # then subsequent pages strictly advance via `after_id` cursor so we NEVER stall on page 1!
    use_since = latest_occurred is not None and binding.events_scan_complete
    scan_complete = True

    for page_idx in range(max_pages):
        try:
            if page_idx == 0 and use_since and latest_occurred is not None:
                since_dt = latest_occurred - timedelta(seconds=OVERLAP_WINDOW_SECONDS)
                page, _ = connector.get_events(since=since_dt, limit=batch_size)
            else:
                page, _ = connector.get_events(after_id=cursor, limit=batch_size)
        except CommerceConnectorError as exc:
            if exc.status_code == 400 and cursor is not None:
                cursor = None
                latest_occurred = None
                cursor_was_reset = True
                page, _ = connector.get_events(limit=batch_size)
            else:
                raise

        if not page.items:
            scan_complete = True
            break

        for ev in page.items:
            if ev.id not in seen_row_ids:
                seen_row_ids.add(ev.id)
                collected.append(ev.model_dump())
            ev_dt = datetime.fromisoformat(ev.occurred_at.replace("Z", "+00:00"))
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=UTC)
            if latest_occurred is None or ev_dt > latest_occurred:
                latest_occurred = ev_dt
            cursor = ev.id

        if not page.has_more:
            scan_complete = True
            break
        if page.next_cursor:
            cursor = page.next_cursor
        if page_idx == max_pages - 1:
            scan_complete = False

    return collected, scan_complete, cursor, latest_occurred, cursor_was_reset


def _persist_pulled_events_fenced(
    db: Session,
    *,
    binding_id: str,
    lease_token: str | None,
    expected_world_epoch: int | None,
    pulled_events: list[dict[str, Any]],
    scan_complete: bool,
    latest_cursor: str | None,
    latest_occurred: datetime | None,
    cursor_was_reset: bool,
) -> tuple[int, int]:
    """Durably persist pulled events with unified hashing & lease fencing.

    Returns `(newly_persisted_count, payload_conflict_count)`.
    """
    locked = _assert_binding_lease_in_txn(
        db,
        binding_id=binding_id,
        lease_token=lease_token,
        expected_world_epoch=expected_world_epoch,
    )
    new_count = 0
    conflict_count = 0
    now = datetime.now(UTC)

    for ev in pulled_events:
        ev_id = str(ev["event_id"])
        ev_type = str(ev["event_type"]).strip().upper()
        ev_occurred = datetime.fromisoformat(str(ev["occurred_at"]).replace("Z", "+00:00"))
        if ev_occurred.tzinfo is None:
            ev_occurred = ev_occurred.replace(tzinfo=UTC)

        p_hash = compute_canonical_event_hash(
            event_id=ev_id,
            event_type=ev_type,
            merchant_id=locked.merchant_id,
            order_id=ev.get("order_id"),
            payment_id=ev.get("payment_id"),
            payload=ev.get("payload") or {},
        )

        existing = db.scalar(
            select(NormalizedEvent).where(
                NormalizedEvent.session_id == locked.session_id,
                NormalizedEvent.world_epoch == locked.world_epoch,
                NormalizedEvent.merchant_id == locked.merchant_id,
                NormalizedEvent.event_id == ev_id,
            )
        )
        if existing is not None:
            if existing.payload_hash != p_hash:
                conflict_count += 1
                record_activity(
                    db,
                    session_id=locked.session_id,
                    world_epoch=locked.world_epoch,
                    kind="EVENT_PAYLOAD_CONFLICT",
                    title_vi=f"Xung đột payload sự kiện {ev_id}",
                    detail_vi=(
                        f"Sự kiện {ev_id} ({ev_type}) có payload_hash khác giữa push và pull; "
                        "không ghi đè bản ghi gốc."
                    ),
                    metadata_json={
                        "event_id": ev_id,
                        "existing_hash": existing.payload_hash,
                        "pulled_hash": p_hash,
                    },
                )
            continue

        row = NormalizedEvent(
            id=f"NEVT-{uuid.uuid4().hex[:12].upper()}",
            event_id=ev_id,
            session_id=locked.session_id,
            world_epoch=locked.world_epoch,
            merchant_id=locked.merchant_id,
            verified_source="MINI_STORE_PULL_CONNECTOR",
            claimed_source=str(ev.get("source") or "MINI_STORE_COMMERCE"),
            source_mode=str(ev.get("source_mode") or "MOCK"),
            schema_version="1.0",
            event_type=ev_type,
            order_id=ev.get("order_id"),
            payment_id=ev.get("payment_id"),
            correlation_id=ev.get("correlation_id"),
            payload_hash=p_hash,
            payload_json={
                "event_payload": ev.get("payload") or {},
                "commerce_row_id": ev.get("id"),
            },
            provenance_json={
                "collection_mode": "PULL_RECONCILE",
                "source_binding_id": locked.id,
                "source_mode": str(ev.get("source_mode") or "MOCK"),
            },
            triggered_investigation=False,
            occurred_at=ev_occurred,
            received_at=now,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
            new_count += 1
        except IntegrityError:
            pass

    if pulled_events or cursor_was_reset:
        locked.last_event_cursor = latest_cursor
        locked.last_event_occurred_at = latest_occurred
        locked.events_observed_count += new_count
    locked.events_scan_complete = scan_complete
    locked.updated_at = now
    db.commit()
    return new_count, conflict_count


def collect_commerce_events(
    db: Session,
    *,
    binding: CommerceSourceBinding,
    connector: CommerceReadConnector,
    batch_size: int = 50,
    max_pages: int | None = None,
    lease_token: str | None = None,
    expected_world_epoch: int | None = None,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Pull paginated Commerce events with bounded work budget, unified hashing, and fencing."""
    settings = get_settings()
    pages_budget = max_pages or settings.monitor_max_pages_per_tick
    pulled, scan_complete, cursor, latest_dt, was_reset = _fetch_remote_events_paginated(
        binding, connector, batch_size=batch_size, max_pages=pages_budget
    )
    new_count, _ = _persist_pulled_events_fenced(
        db,
        binding_id=binding.id,
        lease_token=lease_token,
        expected_world_epoch=expected_world_epoch,
        pulled_events=pulled,
        scan_complete=scan_complete,
        latest_cursor=cursor,
        latest_occurred=latest_dt,
        cursor_was_reset=was_reset,
    )
    db.refresh(binding)
    return len(pulled), new_count, pulled


def _create_commerce_case_and_job(
    db: Session,
    *,
    binding: CommerceSourceBinding,
    obs: AnomalyObservation,
    candidate: DetectedAnomaly,
) -> tuple[str | None, str | None]:
    """Create 1 `Case(source_system="COMMERCE_HTTP")` + 1 `SupervisorJob` respecting quotas."""
    settings = get_settings()
    sess = db.scalar(
        select(DemoSession).where(DemoSession.id == binding.session_id).with_for_update()
    )
    if sess is None:
        return None, None

    current_cases = int(
        db.scalar(select(func.count(Case.id)).where(Case.session_id == sess.id)) or 0
    )
    if current_cases >= sess.max_cases:
        return None, None

    active_investigations = int(
        db.scalar(
            select(func.count(AnomalyObservation.id)).where(
                AnomalyObservation.session_id == sess.id,
                AnomalyObservation.lifecycle_state == "INVESTIGATING",
            )
        )
        or 0
    )
    if active_investigations >= settings.monitor_max_concurrent_investigations:
        return None, None

    now = datetime.now(UTC)
    case_id = f"CASE-MON-{uuid.uuid4().hex[:10].upper()}"
    goal = (
        f"[Autonomous Monitor {candidate.detector_code}] Investigate {candidate.anomaly_type}: "
        f"{candidate.rationale[:360]}"
    )
    idemp_key = f"MON:{binding.world_epoch}:{obs.occurrence_count}:{candidate.fingerprint}"[:128]
    case = Case(
        id=case_id,
        session_id=sess.id,
        type=candidate.case_type,
        goal=goal,
        state=CaseState.NEW.value,
        owner="autonomous-monitor",
        source_system="COMMERCE_HTTP",
        source_binding_id=binding.id,
        idempotency_key=idemp_key,
        created_at=now,
        updated_at=now,
    )
    db.add(case)
    db.flush()

    ev_row = Evidence(
        id=f"EVD-{uuid.uuid4().hex[:10].upper()}",
        case_id=case.id,
        session_id=sess.id,
        kind=EvidenceKind.OBSERVED_FACT.value,
        source="CONTINUOUS_COMMERCE_MONITOR",
        source_ref=obs.id,
        payload_json={
            "detector_code": candidate.detector_code,
            "anomaly_type": candidate.anomaly_type,
            "fingerprint": candidate.fingerprint,
            "occurrence_count": obs.occurrence_count,
            "merchant_id": binding.merchant_id,
            "commerce_session_id": binding.commerce_session_id,
            "affected_order_id": candidate.affected_order_id,
            "affected_payment_id": candidate.affected_payment_id,
            "affected_entities": candidate.affected_entities,
            "evidence_refs": candidate.evidence_refs,
            "rationale": candidate.rationale,
            "recommended_action_hint": candidate.recommended_action_hint,
        },
        observed_at=now,
    )
    db.add(ev_row)
    db.add(
        CaseEvent(
            id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
            case_id=case.id,
            session_id=sess.id,
            type="ANOMALY_CASE_CREATED_BY_MONITOR",
            payload_json={
                "anomaly_observation_id": obs.id,
                "detector_code": candidate.detector_code,
                "anomaly_type": candidate.anomaly_type,
                "occurrence_count": obs.occurrence_count,
                "source_system": "COMMERCE_HTTP",
                "source_binding_id": binding.id,
            },
            created_at=now,
        )
    )

    sup_state = ensure_supervisor_state(db, sess.id)
    job: SupervisorJob = enqueue_case_investigation_job(
        db,
        session_id=sess.id,
        case_id=case.id,
        reason_vi=f"Autonomous Monitor [{candidate.detector_code}] {candidate.anomaly_type}",
        payload_extra={"detector_code": candidate.detector_code, "anomaly_id": obs.id},
    )
    record_activity(
        db,
        session_id=sess.id,
        world_epoch=sup_state.world_epoch,
        kind="MONITOR_ANOMALY_DETECTED",
        title_vi=f"[{candidate.detector_code}] {candidate.anomaly_type} detected",
        detail_vi=candidate.rationale[:300],
        case_id=case.id,
        job_id=job.id,
        metadata_json={
            "anomaly_id": obs.id,
            "detector_code": candidate.detector_code,
            "severity": candidate.severity,
            "source_binding_id": binding.id,
            "occurrence_count": obs.occurrence_count,
        },
    )
    return case.id, job.id


def record_or_update_anomaly(
    db: Session,
    *,
    binding: CommerceSourceBinding,
    candidate: DetectedAnomaly,
    auto_create_case: bool = True,
    lease_token: str | None = None,
    expected_world_epoch: int | None = None,
) -> tuple[AnomalyObservation, bool]:
    """Durably upsert `AnomalyObservation` with lease fencing, row locking, and recurrence."""
    locked_binding = _assert_binding_lease_in_txn(
        db,
        binding_id=binding.id,
        lease_token=lease_token,
        expected_world_epoch=expected_world_epoch,
    )
    now = datetime.now(UTC)
    existing = db.scalar(
        select(AnomalyObservation)
        .where(
            AnomalyObservation.session_id == locked_binding.session_id,
            AnomalyObservation.world_epoch == locked_binding.world_epoch,
            AnomalyObservation.fingerprint == candidate.fingerprint,
        )
        .with_for_update()
    )
    if existing is not None:
        existing.observation_count += 1
        existing.last_observed_at = now
        existing.evidence_refs_json = candidate.evidence_refs
        existing.rationale = candidate.rationale
        existing.updated_at = now
        created_case = False

        # P2 Recurrence: if previously RECOVERED anomaly recurs, preserve history & reopen cleanly!
        if existing.lifecycle_state == "RECOVERED":
            verif_hist = dict(existing.verification_json or {})
            priors = list(verif_hist.get("prior_recoveries") or [])
            priors.append(
                {
                    "occurrence": existing.occurrence_count,
                    "recovered_at": (
                        existing.verified_recovered_at.isoformat()
                        if existing.verified_recovered_at
                        else None
                    ),
                    "recovery_source": existing.recovery_source,
                    "verdict": existing.verification_verdict,
                    "prior_case_id": existing.linked_case_id,
                }
            )
            verif_hist["prior_recoveries"] = priors
            existing.verification_json = verif_hist
            existing.occurrence_count += 1
            existing.lifecycle_state = "OBSERVED"
            existing.linked_case_id = None
            existing.linked_job_id = None
            existing.recovery_source = None
            existing.verification_verdict = None
            existing.verified_recovered_at = None

        if (
            auto_create_case
            and existing.linked_case_id is None
            and existing.lifecycle_state == "OBSERVED"
        ):
            cid, jid = _create_commerce_case_and_job(
                db, binding=locked_binding, obs=existing, candidate=candidate
            )
            if cid is not None:
                existing.linked_case_id = cid
                existing.linked_job_id = jid
                existing.lifecycle_state = "INVESTIGATING"
                created_case = True
        db.commit()
        db.refresh(existing)
        return existing, created_case

    obs = AnomalyObservation(
        id=f"ANOM-{uuid.uuid4().hex[:10].upper()}",
        fingerprint=candidate.fingerprint,
        session_id=locked_binding.session_id,
        world_epoch=locked_binding.world_epoch,
        source_binding_id=locked_binding.id,
        merchant_id=locked_binding.merchant_id,
        detector_code=candidate.detector_code,
        anomaly_type=candidate.anomaly_type,
        severity=candidate.severity,
        lifecycle_state="OBSERVED",
        affected_order_id=candidate.affected_order_id,
        affected_payment_id=candidate.affected_payment_id,
        affected_entities_json=candidate.affected_entities,
        evidence_refs_json=candidate.evidence_refs,
        rationale=candidate.rationale,
        observation_count=1,
        occurrence_count=1,
        first_observed_at=now,
        last_observed_at=now,
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(obs)
            db.flush()
    except IntegrityError:
        existing2 = db.scalar(
            select(AnomalyObservation)
            .where(
                AnomalyObservation.session_id == locked_binding.session_id,
                AnomalyObservation.world_epoch == locked_binding.world_epoch,
                AnomalyObservation.fingerprint == candidate.fingerprint,
            )
            .with_for_update()
        )
        assert existing2 is not None  # noqa: S101
        existing2.observation_count += 1
        existing2.last_observed_at = now
        existing2.updated_at = now
        db.commit()
        db.refresh(existing2)
        return existing2, False

    created_case = False
    if auto_create_case:
        cid, jid = _create_commerce_case_and_job(
            db, binding=locked_binding, obs=obs, candidate=candidate
        )
        if cid is not None:
            obs.linked_case_id = cid
            obs.linked_job_id = jid
            obs.lifecycle_state = "INVESTIGATING"
            created_case = True

    db.commit()
    db.refresh(obs)
    return obs, created_case


def evaluate_recovery_postconditions(
    db: Session,
    *,
    obs: AnomalyObservation,
    connector: CommerceReadConnector,
) -> VerificationResult:
    """Strictly evaluate detector-specific recovery postconditions against authoritative HTTP state.

    Never assumes actor attribution without authoritative audit evidence (defaults to `UNKNOWN`).
    """
    now = datetime.now(UTC)
    code = obs.detector_code.upper()
    evidence: dict[str, Any] = {"checked_at": now.isoformat(), "detector_code": code}

    try:
        if code == "D01":
            if not obs.affected_order_id or not obs.affected_payment_id:
                return VerificationResult(
                    RecoveryVerdict.INSUFFICIENT_EVIDENCE,
                    False,
                    "UNKNOWN",
                    "Missing affected order/payment identity for D01 verification",
                    evidence,
                )
            ord_data = connector.get_order(obs.affected_order_id)["data"]
            pay_data = connector.get_payment(obs.affected_payment_id)["data"]
            evidence["order_status"] = ord_data.get("status")
            evidence["order_reconciliation"] = ord_data.get("reconciliation_status")
            evidence["payment_status"] = pay_data.get("status")
            evidence["payment_is_synced"] = pay_data.get("is_synced_to_order")
            evidence["payment_amount_vnd"] = pay_data.get("amount_vnd")
            evidence["order_total_vnd"] = ord_data.get("total_vnd")

            amount_ok = ord_data.get("total_vnd") is None or int(
                pay_data.get("amount_vnd") or 0
            ) == int(ord_data.get("total_vnd") or 0)
            no_conflict = (
                not bool(pay_data.get("reconciliation_required", False))
                and str(ord_data.get("reconciliation_status") or "CLEAR").upper()
                != "RECONCILIATION_REQUIRED"
            )
            if (
                str(pay_data.get("status") or "").upper() == "SUCCEEDED"
                and bool(pay_data.get("is_synced_to_order", False))
                and str(ord_data.get("status") or "").upper()
                in ("CONFIRMED", "PROCESSING", "SHIPPED", "DELIVERED")
                and amount_ok
                and no_conflict
            ):
                # Attribute actor ONLY if supported by authoritative order timeline audit event
                actor = "UNKNOWN"
                try:
                    tl = connector.get_order_timeline(obs.affected_order_id)["data"]
                    for ev in reversed(tl):
                        payload = ev.get("payload") or {}
                        src = str(payload.get("sync_source") or "")
                        if src == "COMMERCE_ORDER_SYNC_WORKER":
                            actor = "COMMERCE_WORKER"
                            break
                        if src == "FINRESOLVE_RECOVERY_COMMAND":
                            actor = "AI_APPROVED_ACTION"
                            break
                except CommerceConnectorError:
                    pass
                return VerificationResult(
                    RecoveryVerdict.VERIFIED_RECOVERY,
                    True,
                    actor,
                    f"D01 verified: payment synced, order {ord_data.get('status')}, no conflict",
                    evidence,
                )
            return VerificationResult(
                RecoveryVerdict.STILL_INCONSISTENT,
                False,
                "UNKNOWN",
                "D01 postconditions not satisfied (order not confirmed/synced or conflict active)",
                evidence,
            )

        if code == "D02":
            if not obs.affected_order_id or not obs.affected_payment_id:
                return VerificationResult(
                    RecoveryVerdict.INSUFFICIENT_EVIDENCE,
                    False,
                    "UNKNOWN",
                    "Missing order/payment identity for D02 verification",
                    evidence,
                )
            ord_data = connector.get_order(obs.affected_order_id)["data"]
            pay_data = connector.get_payment(obs.affected_payment_id)["data"]
            ord_status = str(ord_data.get("status") or "").upper()
            pay_status = str(pay_data.get("status") or "").upper()
            evidence["order_status"] = ord_status
            evidence["payment_status"] = pay_status
            evidence["refunded_amount_vnd"] = pay_data.get("refunded_amount_vnd")
            evidence["amount_vnd"] = pay_data.get("amount_vnd")

            # Cancelled order MUST NOT be resurrected; RESOLVED label alone is insufficient!
            if ord_status != "CANCELLED":
                return VerificationResult(
                    RecoveryVerdict.STILL_INCONSISTENT,
                    False,
                    "UNKNOWN",
                    f"D02 violation: cancelled order changed to '{ord_status}'",
                    evidence,
                )
            full_refunded = (
                pay_status == "REFUNDED"
                and int(pay_data.get("refunded_amount_vnd") or 0)
                >= int(pay_data.get("amount_vnd") or 0)
                and not bool(pay_data.get("reconciliation_required", False))
            )
            if full_refunded:
                return VerificationResult(
                    RecoveryVerdict.VERIFIED_RECOVERY,
                    True,
                    "UNKNOWN",
                    "D02 verified: order stayed CANCELLED and payment fully REFUNDED",
                    evidence,
                )
            return VerificationResult(
                RecoveryVerdict.STILL_INCONSISTENT,
                False,
                "UNKNOWN",
                "D02 unresolved: payment not fully refunded (RESOLVED label alone insufficient)",
                evidence,
            )

        if code == "D03" and obs.affected_order_id:
            ord_data = connector.get_order(obs.affected_order_id)["data"]
            st = str(ord_data.get("status") or "").upper()
            evidence["order_status"] = st
            if st in ("SHIPPED", "DELIVERED", "CANCELLED"):
                return VerificationResult(
                    RecoveryVerdict.VERIFIED_RECOVERY,
                    True,
                    "UNKNOWN",
                    f"D03 verified: order progressed to {st}",
                    evidence,
                )
            return VerificationResult(
                RecoveryVerdict.STILL_INCONSISTENT,
                False,
                "UNKNOWN",
                f"D03 still stalled in {st}",
                evidence,
            )

        if code == "D04" and obs.affected_payment_id:
            pay_data = connector.get_payment(obs.affected_payment_id)["data"]
            amt = int(pay_data.get("amount_vnd") or 0)
            refunded = int(pay_data.get("refunded_amount_vnd") or 0)
            pending = int(pay_data.get("pending_refund_amount_vnd") or 0)
            unknown = int(pay_data.get("unknown_refund_amount_vnd") or 0)
            committed = refunded + pending + unknown
            evidence.update(
                {
                    "amount_vnd": amt,
                    "refunded_amount_vnd": refunded,
                    "pending_refund_amount_vnd": pending,
                    "unknown_refund_amount_vnd": unknown,
                    "committed_refund_vnd": committed,
                    "reconciliation_required": pay_data.get("reconciliation_required"),
                }
            )
            if (
                amt > 0
                and committed <= amt
                and unknown == 0
                and not bool(pay_data.get("reconciliation_required", False))
            ):
                return VerificationResult(
                    RecoveryVerdict.VERIFIED_RECOVERY,
                    True,
                    "UNKNOWN",
                    "D04 verified: refund accounting balanced with 0 unknown/excessive amounts",
                    evidence,
                )
            return VerificationResult(
                RecoveryVerdict.STILL_INCONSISTENT,
                False,
                "UNKNOWN",
                "D04 unresolved: excessive or unknown refund balance remains",
                evidence,
            )

        if code == "D05":
            variant_id = (obs.affected_entities_json or {}).get("variant_id")
            inv_list, _ = connector.get_inventory(
                variant_id=str(variant_id) if variant_id else None
            )
            if not inv_list:
                # NEVER infer recovery from an empty inventory response!
                return VerificationResult(
                    RecoveryVerdict.INSUFFICIENT_EVIDENCE,
                    False,
                    "UNKNOWN",
                    "D05 cannot verify: inventory query returned empty list",
                    evidence,
                )
            broken = [
                i
                for i in inv_list
                if int(i.get("on_hand_qty") or 0) < 0
                or int(i.get("reserved_qty") or 0) < 0
                or int(i.get("reserved_qty") or 0) > int(i.get("on_hand_qty") or 0)
            ]
            evidence["checked_variants"] = len(inv_list)
            evidence["broken_variants"] = len(broken)
            if not broken:
                return VerificationResult(
                    RecoveryVerdict.VERIFIED_RECOVERY,
                    True,
                    "UNKNOWN",
                    "D05 verified: affected variant inventory invariants satisfied",
                    evidence,
                )
            return VerificationResult(
                RecoveryVerdict.STILL_INCONSISTENT,
                False,
                "UNKNOWN",
                f"D05 still inconsistent across {len(broken)} variant(s)",
                evidence,
            )

        if code == "D06":
            ev_id = (obs.affected_entities_json or {}).get("event_id")
            if not ev_id:
                return VerificationResult(
                    RecoveryVerdict.INSUFFICIENT_EVIDENCE,
                    False,
                    "UNKNOWN",
                    "Missing event_id for D06 verification",
                    evidence,
                )
            exists = db.scalar(
                select(NormalizedEvent.id).where(
                    NormalizedEvent.session_id == obs.session_id,
                    NormalizedEvent.world_epoch == obs.world_epoch,
                    NormalizedEvent.event_id == str(ev_id),
                )
            )
            evidence["event_present_in_inbox"] = exists is not None
            if exists is not None:
                return VerificationResult(
                    RecoveryVerdict.VERIFIED_RECOVERY,
                    True,
                    "COMMERCE_PULL_RECONCILE",
                    f"D06 verified: event {ev_id} recovered into durable inbox",
                    evidence,
                )
            return VerificationResult(
                RecoveryVerdict.STILL_INCONSISTENT,
                False,
                "UNKNOWN",
                f"D06 unresolved: event {ev_id} still missing from inbox",
                evidence,
            )

        if code == "D07":
            if obs.affected_payment_id:
                pay_data = connector.get_payment(obs.affected_payment_id)["data"]
                p_status = str(pay_data.get("status") or "").upper()
                recon_req = bool(pay_data.get("reconciliation_required", False))
                evidence["payment_status"] = p_status
                evidence["reconciliation_required"] = recon_req
                # Must have conclusive non-UNKNOWN status AND cleared reconciliation
                if (
                    p_status in ("SUCCEEDED", "FAILED", "REFUNDED", "PARTIALLY_REFUNDED")
                    and not recon_req
                ):
                    if obs.affected_order_id:
                        ord_data = connector.get_order(obs.affected_order_id)["data"]
                        evidence["order_reconciliation_status"] = ord_data.get(
                            "reconciliation_status"
                        )
                        if (
                            str(ord_data.get("reconciliation_status") or "").upper()
                            == "RECONCILIATION_REQUIRED"
                        ):
                            return VerificationResult(
                                RecoveryVerdict.STILL_INCONSISTENT,
                                False,
                                "UNKNOWN",
                                "D07 unresolved: order still marked RECONCILIATION_REQUIRED",
                                evidence,
                            )
                    return VerificationResult(
                        RecoveryVerdict.VERIFIED_RECOVERY,
                        True,
                        "UNKNOWN",
                        f"D07 verified: conclusive payment status {p_status} and conflict cleared",
                        evidence,
                    )
                return VerificationResult(
                    RecoveryVerdict.STILL_INCONSISTENT,
                    False,
                    "UNKNOWN",
                    f"D07 unresolved: status={p_status}, reconciliation_required={recon_req}",
                    evidence,
                )
            return VerificationResult(
                RecoveryVerdict.INSUFFICIENT_EVIDENCE,
                False,
                "UNKNOWN",
                "D07 missing affected payment reference",
                evidence,
            )

        if code == "D08":
            settings = get_settings()
            metrics, _ = connector.get_metrics()
            evidence["outbox_lag_count"] = metrics.outbox_lag_count
            evidence["unsynced_payments"] = metrics.unsynced_payments
            if (
                metrics.outbox_lag_count < settings.monitor_outbox_lag_threshold
                and metrics.unsynced_payments < settings.monitor_unsynced_backlog_threshold
            ):
                return VerificationResult(
                    RecoveryVerdict.VERIFIED_RECOVERY,
                    True,
                    "UNKNOWN",
                    "D08 verified: operational backlog back below threshold",
                    evidence,
                )
            return VerificationResult(
                RecoveryVerdict.STILL_INCONSISTENT,
                False,
                "UNKNOWN",
                "D08 unresolved: operational backlog still above threshold",
                evidence,
            )

        if code in ("OPEN_STAT", "OPEN_MV"):
            telem_payload, _ = connector.get_telemetry()
            metrics, _ = connector.get_metrics()
            w60 = (telem_payload.get("windows") or {}).get("60s") or {}
            lat_p95 = float((w60.get("latency_ms") or {}).get("p95") or 0.0)
            err_rate = float(w60.get("error_rate") or 0.0)
            outbox_lag = float(metrics.outbox_lag_count)
            evidence["current_p95_latency_ms"] = lat_p95
            evidence["current_error_rate"] = err_rate
            evidence["current_outbox_lag"] = outbox_lag

            # Compare against baseline recorded during detection (never mark recovered merely
            # because deterministic D01..D08 detectors do not fire!)
            prior_verif = dict(obs.verification_json or {})
            healthy_streak = int(prior_verif.get("consecutive_healthy_windows") or 0)
            # Determine baseline upper bounds from persisted evidence_refs
            lat_limit = 250.0
            err_limit = 0.05
            lag_limit = 5.0
            for ref in obs.evidence_refs_json or []:
                for m in ref.get("metrics") or []:
                    mname = str(m.get("metric") or "")
                    bmed = float(m.get("baseline_median") or 0.0)
                    if mname == "p95_latency_ms" and bmed > 0:
                        lat_limit = max(bmed * 2.0, bmed + 50.0)
                    elif mname == "error_rate":
                        err_limit = max(bmed + 0.05, 0.05)
                    elif mname == "outbox_lag":
                        lag_limit = max(bmed + 3.0, 5.0)

            is_healthy_now = (
                lat_p95 <= lat_limit and err_rate <= err_limit and outbox_lag <= lag_limit
            )
            if is_healthy_now:
                healthy_streak += 1
                evidence["consecutive_healthy_windows"] = healthy_streak
                if healthy_streak >= 2:
                    return VerificationResult(
                        RecoveryVerdict.VERIFIED_RECOVERY,
                        True,
                        "OBSERVED_BASELINE_RETURN",
                        f"{code} verified sustained recovery across {healthy_streak} fresh windows",
                        evidence,
                    )
                return VerificationResult(
                    RecoveryVerdict.STILL_INCONSISTENT,
                    False,
                    "UNKNOWN",
                    f"{code} healthy window ({healthy_streak}/2); awaiting sustained recovery",
                    evidence,
                )
            evidence["consecutive_healthy_windows"] = 0
            return VerificationResult(
                RecoveryVerdict.STILL_INCONSISTENT,
                False,
                "UNKNOWN",
                f"{code} still anomalous (p95={lat_p95}ms, err_rate={err_rate}, lag={outbox_lag})",
                evidence,
            )

        return VerificationResult(
            RecoveryVerdict.INSUFFICIENT_EVIDENCE,
            False,
            "UNKNOWN",
            f"Unsupported detector code '{code}' for verification",
            evidence,
        )
    except CommerceConnectorError as exc:
        evidence["error"] = str(exc)
        return VerificationResult(
            RecoveryVerdict.SOURCE_UNAVAILABLE,
            False,
            "UNKNOWN",
            f"Verification blocked (Commerce unavailable): {exc}",
            evidence,
        )


def verify_anomaly_recovery(
    db: Session,
    *,
    obs: AnomalyObservation,
    connector: CommerceReadConnector,
    lease_token: str | None = None,
    expected_world_epoch: int | None = None,
) -> tuple[bool, str]:
    """Independently verify recovery with lease fencing and structured verdict persistence."""
    now = datetime.now(UTC)
    if obs.lifecycle_state == "RECOVERED":
        return True, "Already verified recovered"

    result = evaluate_recovery_postconditions(db, obs=obs, connector=connector)

    # Re-verify lease & world epoch inside transaction before persisting outcome!
    _assert_binding_lease_in_txn(
        db,
        binding_id=obs.source_binding_id,
        lease_token=lease_token,
        expected_world_epoch=expected_world_epoch,
    )
    locked_obs = db.scalar(
        select(AnomalyObservation).where(AnomalyObservation.id == obs.id).with_for_update()
    )
    if locked_obs is None:
        return False, "Anomaly observation not found"

    merged_verif = dict(locked_obs.verification_json or {})
    merged_verif.update(result.evidence)
    merged_verif["verdict"] = result.verdict.value
    locked_obs.verification_json = merged_verif
    locked_obs.verification_verdict = result.verdict.value
    locked_obs.updated_at = now

    if result.recovered and result.verdict == RecoveryVerdict.VERIFIED_RECOVERY:
        locked_obs.lifecycle_state = "RECOVERED"
        locked_obs.recovery_source = result.recovery_source
        locked_obs.verified_recovered_at = now
        record_activity(
            db,
            session_id=locked_obs.session_id,
            world_epoch=locked_obs.world_epoch,
            kind="MONITOR_RECOVERY_VERIFIED",
            title_vi=f"[{locked_obs.detector_code}] Recovery verified ({result.recovery_source})",
            detail_vi=(
                f"Anomaly {locked_obs.id} ({locked_obs.anomaly_type}) verified recovered "
                "against MiniStore HTTP state."
            ),
            case_id=locked_obs.linked_case_id,
            metadata_json=result.evidence,
        )
        db.commit()
        db.refresh(obs)
        return True, result.detail

    db.commit()
    db.refresh(obs)
    return False, result.detail


def _fetch_paginated_business_records(
    binding: CommerceSourceBinding,
    connector: CommerceReadConnector,
    *,
    max_pages: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], datetime, dict[str, Any]]:
    """Fetch prioritized pending/reconciliation + paginated orders & payments beyond 200 records."""
    orders_map: dict[str, dict[str, Any]] = {}
    payments_map: dict[str, dict[str, Any]] = {}
    obs_at = datetime.now(UTC)

    # 1. Priority scan: unsynced & reconciliation-required records first (never missed!)
    p_unsynced, obs_at = connector.list_payments(is_synced=False, limit=200)
    for p in p_unsynced:
        if p.get("payment_id"):
            payments_map[str(p["payment_id"])] = p
    p_recon, _ = connector.list_payments(reconciliation_required=True, limit=200)
    for p in p_recon:
        if p.get("payment_id"):
            payments_map[str(p["payment_id"])] = p

    o_recon, _ = connector.list_orders(reconciliation_status="RECONCILIATION_REQUIRED", limit=200)
    for o in o_recon:
        if o.get("id"):
            orders_map[str(o["id"])] = o

    # 2. Paginated full scan within bounded page budget
    o_cursor: str | None = None
    o_pages = 0
    for _ in range(max_pages):
        batch, obs_at = connector.list_orders(after_id=o_cursor, limit=200)
        o_pages += 1
        for o in batch:
            if o.get("id"):
                orders_map[str(o["id"])] = o
        if len(batch) < 200:
            break
        o_cursor = str(batch[-1]["id"])

    p_cursor: str | None = None
    p_pages = 0
    for _ in range(max_pages):
        batch, _ = connector.list_payments(after_id=p_cursor, limit=200)
        p_pages += 1
        for p in batch:
            if p.get("payment_id"):
                payments_map[str(p["payment_id"])] = p
        if len(batch) < 200:
            break
        p_cursor = str(batch[-1]["payment_id"])

    coverage = {
        "orders_scanned": len(orders_map),
        "orders_pages": o_pages,
        "payments_scanned": len(payments_map),
        "payments_pages": p_pages,
        "scanned_at": obs_at.isoformat(),
    }
    return list(orders_map.values()), list(payments_map.values()), obs_at, coverage


def run_binding_scan(
    db: Session,
    *,
    binding: CommerceSourceBinding,
    connector: CommerceReadConnector | None = None,
    transport: httpx.BaseTransport | None = None,
    service_key: str | None = None,
    auto_create_cases: bool = True,
    lease_token: str | None = None,
    expected_world_epoch: int | None = None,
) -> ScanSummary:
    """Run one hardened autonomous monitoring scan with fencing, D06 ordering, and pagination."""
    settings = get_settings()
    summary = ScanSummary(binding_id=binding.id, connectivity_status=binding.connectivity_status)
    start_monotonic = time.monotonic()
    now = datetime.now(UTC)

    if not binding.is_enabled or binding.world_reset_required:
        summary.connectivity_status = (
            "WORLD_RESET_REQUIRED" if binding.world_reset_required else "PAUSED"
        )
        return summary
    if not binding.base_url:
        binding.connectivity_status = "UNCONFIGURED"
        binding.updated_at = now
        db.commit()
        summary.connectivity_status = "UNCONFIGURED"
        return summary

    conn = connector or build_connector_for_binding(
        binding, service_key=service_key, transport=transport
    )

    try:
        # Step 1 (D06 Fix): Snapshot prior inbox event IDs BEFORE ingesting pulled events!
        prior_ingested_ids = set(
            db.scalars(
                select(NormalizedEvent.event_id).where(
                    NormalizedEvent.session_id == binding.session_id,
                    NormalizedEvent.world_epoch == binding.world_epoch,
                    NormalizedEvent.merchant_id == binding.merchant_id,
                )
            ).all()
        )

        # Step 2: Fetch remote events across pages within work budget (without persisting yet)
        remote_events, ev_complete, next_ev_cursor, latest_ev_dt, ev_reset = (
            _fetch_remote_events_paginated(
                binding,
                conn,
                batch_size=settings.monitor_batch_size,
                max_pages=settings.monitor_max_pages_per_tick,
            )
        )
        summary.events_pulled = len(remote_events)
        summary.events_scan_complete = ev_complete

        # Step 3 (D06 Fix): Evaluate D06 against PRIOR inbox state (push-expected events only!)
        d06_snapshot = CommerceSnapshot(
            merchant_id=binding.merchant_id,
            commerce_session_id=binding.commerce_session_id,
            observed_at=datetime.now(UTC),
            ingested_event_ids=prior_ingested_ids,
            remote_events=remote_events,
        )
        d06_candidates = detect_d06_missing_event_delivery(
            d06_snapshot, grace_seconds=settings.monitor_payment_sync_grace_seconds
        )
        for cand in d06_candidates:
            _, created_case = record_or_update_anomaly(
                db,
                binding=binding,
                candidate=cand,
                auto_create_case=auto_create_cases,
                lease_token=lease_token,
                expected_world_epoch=expected_world_epoch,
            )
            if created_case:
                summary.cases_created += 1

        # Step 4: Durably persist pulled events (recovers missing events) under lease fence
        new_ev, conflicts = _persist_pulled_events_fenced(
            db,
            binding_id=binding.id,
            lease_token=lease_token,
            expected_world_epoch=expected_world_epoch,
            pulled_events=remote_events,
            scan_complete=ev_complete,
            latest_cursor=next_ev_cursor,
            latest_occurred=latest_ev_dt,
            cursor_was_reset=ev_reset,
        )
        summary.events_new = new_ev
        summary.events_conflicts = conflicts

        if time.monotonic() - start_monotonic > settings.monitor_max_scan_seconds:
            summary.connectivity_status = "CONNECTED"
            return summary

        # Step 5: Fetch prioritized & paginated authoritative Commerce records (beyond 200!)
        orders, payments, obs_at, coverage = _fetch_paginated_business_records(
            binding, conn, max_pages=settings.monitor_max_pages_per_tick
        )
        summary.orders_scanned = len(orders)
        summary.payments_scanned = len(payments)
        refunds, _ = conn.list_refunds(limit=200)
        inventory, _ = conn.get_inventory()
        metrics, _ = conn.get_metrics()

        snapshot = CommerceSnapshot(
            merchant_id=binding.merchant_id,
            commerce_session_id=binding.commerce_session_id,
            observed_at=obs_at,
            orders=orders,
            payments=payments,
            refunds=refunds,
            inventory=inventory,
            metrics=metrics,
            ingested_event_ids=prior_ingested_ids,
            remote_events=remote_events,
        )

        # Step 6: Run deterministic detectors D01..D05, D07, D08 (D06 already evaluated above)
        candidates = run_all_detectors(
            snapshot,
            payment_sync_grace_seconds=settings.monitor_payment_sync_grace_seconds,
            order_stall_seconds=settings.monitor_order_stall_seconds,
            outbox_lag_threshold=settings.monitor_outbox_lag_threshold,
            unsynced_backlog_threshold=settings.monitor_unsynced_backlog_threshold,
            include_missing_events=False,
        )

        # Step 6b (Prompt 14.1 Part B): Real telemetry + Open-World Hybrid Detection (Layer 2 & 3)
        open_world_candidates: list[DetectedAnomaly] = []
        try:
            from app.models.open_world import BusinessObservation
            from app.observability.open_world_detector import (
                TelemetrySignalVector,
                run_open_world_hybrid_detection,
            )

            telem_data, telem_obs_at = conn.get_telemetry()
            w60 = (telem_data.get("windows") or {}).get("60s") or {}
            w10 = (telem_data.get("windows") or {}).get("10s") or {}
            active_win = w10 if int(w10.get("request_count") or 0) > 0 else w60
            has_samples = bool(
                active_win.get("has_samples", int(active_win.get("request_count") or 0) > 0)
            )
            sig_vec = TelemetrySignalVector(
                rps=float(active_win.get("rps") or 0.0),
                p95_latency_ms=float((active_win.get("latency_ms") or {}).get("p95") or 0.0),
                error_rate=float(active_win.get("error_rate") or 0.0),
                outbox_lag=float(metrics.outbox_lag_count),
                unsynced_payments=float(metrics.unsynced_payments),
                order_throughput=float(len(orders)),
                refund_count=float(len(refunds)),
            )
            db.add(
                BusinessObservation(
                    id=f"OBS-{uuid.uuid4().hex[:10].upper()}",
                    session_id=binding.session_id,
                    world_epoch=binding.world_epoch,
                    merchant_id=binding.merchant_id,
                    commerce_session_id=binding.commerce_session_id,
                    source_system="MINI_STORE_TELEMETRY",
                    category="OPERATIONAL",
                    observation_type="TELEMETRY_WINDOW_SNAPSHOT",
                    evidence_kind="OBSERVED_FACT",
                    entity_refs_json={"binding_id": binding.id},
                    attributes_json={
                        "has_samples": has_samples,
                        "truncated": bool(active_win.get("truncated", False)),
                        "last_sample_age_ms": active_win.get("last_sample_age_ms"),
                        "rps": sig_vec.rps,
                        "p95_latency_ms": sig_vec.p95_latency_ms,
                        "error_rate": sig_vec.error_rate,
                        "outbox_lag": sig_vec.outbox_lag,
                        "unsynced_payments": sig_vec.unsynced_payments,
                        "order_throughput": sig_vec.order_throughput,
                        "refund_count": sig_vec.refund_count,
                    },
                    provenance_json={
                        "endpoint": "/api/v1/service/telemetry",
                        "measured_from_real_requests": bool(
                            telem_data.get("measured_from_real_requests", True)
                        ),
                    },
                    verification_status="VERIFIED",
                    freshness_ms=int(active_win.get("last_sample_age_ms") or 0),
                    observed_at=telem_obs_at,
                )
            )
            open_world_candidates, baseline_evals = run_open_world_hybrid_detection(
                db,
                session_id=binding.session_id,
                merchant_id=binding.merchant_id,
                service_name="ministore-commerce",
                signals=sig_vec,
                regime="NORMAL",
            )
            coverage["open_world"] = {
                "telemetry_available": True,
                "has_samples": has_samples,
                "signals": {
                    "rps": sig_vec.rps,
                    "p95_latency_ms": sig_vec.p95_latency_ms,
                    "error_rate": sig_vec.error_rate,
                    "outbox_lag": sig_vec.outbox_lag,
                    "unsynced_payments": sig_vec.unsynced_payments,
                },
                "baselines": {
                    k: {
                        "warm_up_complete": v.warm_up_complete,
                        "median": v.baseline_median,
                        "robust_z": v.robust_z_score,
                        "is_anomalous": v.is_anomalous,
                    }
                    for k, v in baseline_evals.items()
                },
                "unknown_anomalies_detected": len(open_world_candidates),
            }
        except CommerceConnectorError:
            coverage["open_world"] = {"telemetry_available": False}

        all_candidates = [*candidates, *open_world_candidates]
        summary.anomalies_detected = len(all_candidates) + len(d06_candidates)

        for cand in all_candidates:
            _, created_case = record_or_update_anomaly(
                db,
                binding=binding,
                candidate=cand,
                auto_create_case=auto_create_cases,
                lease_token=lease_token,
                expected_world_epoch=expected_world_epoch,
            )
            if created_case:
                summary.cases_created += 1

        # Step 7: Reconcile stranded/UNKNOWN_OUTCOME recovery commands, then verify open anomalies
        try:
            from app.connectors.commerce_write import reconcile_stranded_recovery_commands

            reconcile_stranded_recovery_commands(
                db,
                binding=binding,
                transport=transport,
                read_connector=conn,
            )
        except Exception as rec_exc:  # noqa: BLE001
            coverage["stranded_reconciliation_error"] = str(rec_exc)[:200]

        open_anomalies = list(
            db.scalars(
                select(AnomalyObservation).where(
                    AnomalyObservation.source_binding_id == binding.id,
                    AnomalyObservation.lifecycle_state.in_(
                        ["OBSERVED", "INVESTIGATING", "RECOVERY_PENDING"]
                    ),
                )
            ).all()
        )
        for obs in open_anomalies:
            recovered, _ = verify_anomaly_recovery(
                db,
                obs=obs,
                connector=conn,
                lease_token=lease_token,
                expected_world_epoch=expected_world_epoch,
            )
            if recovered:
                summary.recoveries_verified += 1

        # Step 8: Fenced telemetry & connectivity update
        locked = _assert_binding_lease_in_txn(
            db,
            binding_id=binding.id,
            lease_token=lease_token,
            expected_world_epoch=expected_world_epoch,
        )
        locked.connectivity_status = "CONNECTED"
        locked.last_successful_scan_at = datetime.now(UTC)
        locked.last_heartbeat_at = datetime.now(UTC)
        locked.last_error = None
        locked.last_scan_coverage_json = coverage
        locked.updated_at = datetime.now(UTC)
        db.commit()
        db.refresh(binding)
        summary.connectivity_status = "CONNECTED"
        return summary

    except StaleLeaseError as exc:
        db.rollback()
        summary.connectivity_status = "LEASE_LOST"
        summary.error = str(exc)
        return summary

    except CommerceConnectorError as exc:
        try:
            locked = _assert_binding_lease_in_txn(
                db,
                binding_id=binding.id,
                lease_token=lease_token,
                expected_world_epoch=expected_world_epoch,
            )
            locked.connectivity_status = "DISCONNECTED"
            locked.last_error = str(exc)[:500]
            locked.last_heartbeat_at = datetime.now(UTC)
            locked.updated_at = datetime.now(UTC)
            db.commit()
        except StaleLeaseError:
            db.rollback()
        summary.connectivity_status = "DISCONNECTED"
        summary.error = str(exc)
        return summary
