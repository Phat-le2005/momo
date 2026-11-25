"""Continuous Commerce Monitor Core Service (Prompt 12 Sections 10, 12, 15).

Responsibilities:
1. Durable checkpoint pull collector (`collect_commerce_events`) with bounded overlap +
   `NormalizedEvent` deduplication; advances cursor ONLY after commit.
2. Autonomous anomaly detection & deduplication (`record_or_update_anomaly`) using unique
   `(session_id, world_epoch, fingerprint)` under PostgreSQL `IntegrityError` handling.
3. Automatic Case + SupervisorJob scheduling bound to `source_system="COMMERCE_HTTP"` and
   `source_binding_id=binding.id`, respecting `DemoSession.max_cases` & concurrency cap
   (retaining anomalies in `OBSERVED` when quota is reached — never discarding!).
4. Independent recovery verification (`verify_anomaly_recovery`) that re-reads authoritative
   MiniStore HTTP records and marks `RECOVERED` ONLY when postconditions are verified.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.connectors.commerce import CommerceConnectorError, CommerceReadConnector
from app.core.config import get_settings
from app.models.cases import Case, CaseEvent, Evidence
from app.models.enums import CaseState, EvidenceKind
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.session import DemoSession
from app.models.supervisor import NormalizedEvent, SupervisorJob
from app.monitoring.detectors import CommerceSnapshot, DetectedAnomaly, run_all_detectors
from app.supervisor.service import (
    enqueue_case_investigation_job,
    ensure_supervisor_state,
    record_activity,
)

OVERLAP_WINDOW_SECONDS = 5


@dataclass
class ScanSummary:
    binding_id: str
    connectivity_status: str
    events_pulled: int = 0
    events_new: int = 0
    anomalies_detected: int = 0
    cases_created: int = 0
    recoveries_verified: int = 0
    error: str | None = None


def ensure_commerce_binding(
    db: Session,
    *,
    session_id: str,
    base_url: str | None = None,
    merchant_id: str | None = None,
    commerce_session_id: str | None = None,
    commerce_world_epoch: int | None = None,
    is_enabled: bool = True,
) -> CommerceSourceBinding:
    """Create or update a session's `CommerceSourceBinding`."""
    settings = get_settings()
    sup_state = ensure_supervisor_state(db, session_id)
    world_epoch = sup_state.world_epoch
    mid = (merchant_id or settings.commerce_merchant_id).strip()
    csid = (commerce_session_id or settings.commerce_session_id).strip()
    url = (base_url or settings.commerce_service_base_url).strip().rstrip("/")
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
        if url:
            existing.base_url = url
        existing.world_epoch = world_epoch
        existing.commerce_world_epoch = c_epoch
        existing.is_enabled = is_enabled
        if not is_enabled:
            existing.connectivity_status = "PAUSED"
        elif existing.connectivity_status == "PAUSED":
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
        base_url=url,
        is_enabled=is_enabled,
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


def _hash_remote_event(item: dict[str, Any]) -> str:
    canonical = {
        "event_id": item.get("event_id"),
        "event_type": item.get("event_type"),
        "order_id": item.get("order_id"),
        "payment_id": item.get("payment_id"),
        "payload": item.get("payload") or {},
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def collect_commerce_events(
    db: Session,
    *,
    binding: CommerceSourceBinding,
    connector: CommerceReadConnector,
    batch_size: int = 50,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Pull Commerce events using bounded overlap + durable `NormalizedEvent` deduplication.

    Advances `last_event_cursor` and `last_event_occurred_at` ONLY after commit.
    Handles invalid/stale cursor gracefully by resetting to timestamp overlap or full pull.
    Returns `(pulled_count, newly_persisted_count, pulled_event_dicts)`.
    """
    since_dt: datetime | None = None
    if binding.last_event_occurred_at is not None:
        since_dt = binding.last_event_occurred_at - timedelta(seconds=OVERLAP_WINDOW_SECONDS)

    try:
        page, _ = connector.get_events(
            after_id=None if since_dt is not None else binding.last_event_cursor,
            since=since_dt,
            limit=batch_size,
        )
    except CommerceConnectorError as exc:
        if exc.status_code == 400 and binding.last_event_cursor is not None:
            # Stale/reset cursor after commerce world reset -> recover cleanly from start
            binding.last_event_cursor = None
            binding.last_event_occurred_at = None
            db.flush()
            page, _ = connector.get_events(limit=batch_size)
        else:
            raise

    pulled_dicts: list[dict[str, Any]] = []
    new_count = 0
    latest_cursor = binding.last_event_cursor
    latest_occurred = binding.last_event_occurred_at

    for ev in page.items:
        ev_dict = ev.model_dump()
        pulled_dicts.append(ev_dict)
        ev_occurred = datetime.fromisoformat(ev.occurred_at.replace("Z", "+00:00"))
        if ev_occurred.tzinfo is None:
            ev_occurred = ev_occurred.replace(tzinfo=UTC)

        existing = db.scalar(
            select(NormalizedEvent).where(
                NormalizedEvent.session_id == binding.session_id,
                NormalizedEvent.world_epoch == binding.world_epoch,
                NormalizedEvent.merchant_id == binding.merchant_id,
                NormalizedEvent.event_id == ev.event_id,
            )
        )
        if existing is None:
            row = NormalizedEvent(
                id=f"NEVT-{uuid.uuid4().hex[:12].upper()}",
                event_id=ev.event_id,
                session_id=binding.session_id,
                world_epoch=binding.world_epoch,
                merchant_id=binding.merchant_id,
                verified_source="MINI_STORE_PULL_CONNECTOR",
                claimed_source=ev.source,
                source_mode=ev.source_mode or "MOCK",
                schema_version="1.0",
                event_type=ev.event_type,
                order_id=ev.order_id,
                payment_id=ev.payment_id,
                correlation_id=ev.correlation_id,
                payload_hash=_hash_remote_event(ev_dict),
                payload_json={"event_payload": ev.payload, "commerce_row_id": ev.id},
                provenance_json={
                    "collection_mode": "PULL_RECONCILE",
                    "source_binding_id": binding.id,
                    "source_mode": ev.source_mode or "MOCK",
                },
                triggered_investigation=False,
                occurred_at=ev_occurred,
                received_at=datetime.now(UTC),
            )
            try:
                with db.begin_nested():
                    db.add(row)
                    db.flush()
                new_count += 1
            except IntegrityError:
                pass

        latest_cursor = ev.id
        if latest_occurred is None or ev_occurred > latest_occurred:
            latest_occurred = ev_occurred

    # Advance checkpoint ONLY after durable persistence succeeds
    if page.items:
        binding.last_event_cursor = latest_cursor
        binding.last_event_occurred_at = latest_occurred
        binding.events_observed_count += new_count
    binding.updated_at = datetime.now(UTC)
    db.commit()
    return len(page.items), new_count, pulled_dicts


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

    # Respect DemoSession.max_cases quota — retain anomaly in OBSERVED (never discard!)
    current_cases = int(
        db.scalar(select(func.count(Case.id)).where(Case.session_id == sess.id)) or 0
    )
    if current_cases >= sess.max_cases:
        return None, None

    # Respect monitor_max_concurrent_investigations cap
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
    case = Case(
        id=case_id,
        session_id=sess.id,
        type=candidate.case_type,
        goal=goal,
        state=CaseState.NEW.value,
        owner="autonomous-monitor",
        source_system="COMMERCE_HTTP",
        source_binding_id=binding.id,
        idempotency_key=f"MON:{binding.world_epoch}:{candidate.fingerprint}"[:128],
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
        },
    )
    return case.id, job.id


def record_or_update_anomaly(
    db: Session,
    *,
    binding: CommerceSourceBinding,
    candidate: DetectedAnomaly,
    auto_create_case: bool = True,
) -> tuple[AnomalyObservation, bool]:
    """Durably upsert an `AnomalyObservation` by `(session_id, world_epoch, fingerprint)`.

    Returns `(observation, created_case_flag)`.
    """
    now = datetime.now(UTC)
    existing = db.scalar(
        select(AnomalyObservation).where(
            AnomalyObservation.session_id == binding.session_id,
            AnomalyObservation.world_epoch == binding.world_epoch,
            AnomalyObservation.fingerprint == candidate.fingerprint,
        )
    )
    if existing is not None:
        existing.observation_count += 1
        existing.last_observed_at = now
        existing.evidence_refs_json = candidate.evidence_refs
        existing.rationale = candidate.rationale
        existing.updated_at = now
        created_case = False
        if (
            auto_create_case
            and existing.linked_case_id is None
            and existing.lifecycle_state == "OBSERVED"
        ):
            cid, jid = _create_commerce_case_and_job(
                db, binding=binding, obs=existing, candidate=candidate
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
        session_id=binding.session_id,
        world_epoch=binding.world_epoch,
        source_binding_id=binding.id,
        merchant_id=binding.merchant_id,
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
        # Concurrent worker inserted the same fingerprint simultaneously -> update existing row
        existing2 = db.scalar(
            select(AnomalyObservation).where(
                AnomalyObservation.session_id == binding.session_id,
                AnomalyObservation.world_epoch == binding.world_epoch,
                AnomalyObservation.fingerprint == candidate.fingerprint,
            )
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
        cid, jid = _create_commerce_case_and_job(db, binding=binding, obs=obs, candidate=candidate)
        if cid is not None:
            obs.linked_case_id = cid
            obs.linked_job_id = jid
            obs.lifecycle_state = "INVESTIGATING"
            created_case = True

    db.commit()
    db.refresh(obs)
    return obs, created_case


def verify_anomaly_recovery(
    db: Session,
    *,
    obs: AnomalyObservation,
    connector: CommerceReadConnector,
) -> tuple[bool, str]:
    """Independently verify recovery by re-reading authoritative MiniStore HTTP state.

    NEVER marks recovered on timeout, HTTP error, or unverified claims!
    """
    now = datetime.now(UTC)
    if obs.lifecycle_state == "RECOVERED":
        return True, "Already verified recovered"

    try:
        code = obs.detector_code.upper()
        verified = False
        recovery_source = "UNKNOWN"
        evidence: dict[str, Any] = {"checked_at": now.isoformat(), "detector_code": code}

        if code == "D01" and obs.affected_order_id and obs.affected_payment_id:
            ord_env = connector.get_order(obs.affected_order_id)
            pay_env = connector.get_payment(obs.affected_payment_id)
            ord_data = ord_env["data"]
            pay_data = pay_env["data"]
            evidence["order_status"] = ord_data.get("status")
            evidence["payment_is_synced"] = pay_data.get("is_synced_to_order")
            if str(ord_data.get("status") or "").upper() in (
                "CONFIRMED",
                "PROCESSING",
                "SHIPPED",
                "DELIVERED",
            ) and bool(pay_data.get("is_synced_to_order", False)):
                verified = True
                recovery_source = "COMMERCE_WORKER"

        elif code == "D02" and obs.affected_order_id and obs.affected_payment_id:
            ord_data = connector.get_order(obs.affected_order_id)["data"]
            pay_data = connector.get_payment(obs.affected_payment_id)["data"]
            evidence["order_status"] = ord_data.get("status")
            evidence["payment_status"] = pay_data.get("status")
            evidence["reconciliation_status"] = ord_data.get("reconciliation_status")
            if (
                str(pay_data.get("status") or "").upper() == "REFUNDED"
                or str(ord_data.get("reconciliation_status") or "").upper() == "RESOLVED"
            ):
                verified = True
                recovery_source = "MERCHANT"

        elif code == "D03" and obs.affected_order_id:
            ord_data = connector.get_order(obs.affected_order_id)["data"]
            st = str(ord_data.get("status") or "").upper()
            evidence["order_status"] = st
            if st in ("SHIPPED", "DELIVERED", "CANCELLED"):
                verified = True
                recovery_source = "MERCHANT"

        elif code == "D04" and obs.affected_payment_id:
            pay_data = connector.get_payment(obs.affected_payment_id)["data"]
            amt = int(pay_data.get("amount_vnd") or 0)
            committed = (
                int(pay_data.get("refunded_amount_vnd") or 0)
                + int(pay_data.get("pending_refund_amount_vnd") or 0)
                + int(pay_data.get("unknown_refund_amount_vnd") or 0)
            )
            unknown_amt = int(pay_data.get("unknown_refund_amount_vnd") or 0)
            evidence["committed_refund_vnd"] = committed
            evidence["unknown_refund_vnd"] = unknown_amt
            if (
                committed <= amt
                and unknown_amt == 0
                and not bool(pay_data.get("reconciliation_required"))
            ):
                verified = True
                recovery_source = "MERCHANT"

        elif code == "D05":
            inv_list, _ = connector.get_inventory()
            variant_id = (obs.affected_entities_json or {}).get("variant_id")
            if variant_id:
                matching = [i for i in inv_list if i.get("variant_id") == variant_id]
                if matching:
                    on_hand = int(matching[0].get("on_hand_qty") or 0)
                    reserved = int(matching[0].get("reserved_qty") or 0)
                    evidence["on_hand_qty"] = on_hand
                    evidence["reserved_qty"] = reserved
                    if on_hand >= 0 and reserved >= 0 and reserved <= on_hand:
                        verified = True
                        recovery_source = "MERCHANT"
            else:
                broken = [
                    i
                    for i in inv_list
                    if int(i.get("on_hand_qty") or 0) < 0
                    or int(i.get("reserved_qty") or 0) > int(i.get("on_hand_qty") or 0)
                ]
                evidence["broken_variant_count"] = len(broken)
                if not broken:
                    verified = True
                    recovery_source = "MERCHANT"

        elif code == "D06":
            ev_id = (obs.affected_entities_json or {}).get("event_id")
            if ev_id:
                exists = db.scalar(
                    select(NormalizedEvent.id).where(
                        NormalizedEvent.session_id == obs.session_id,
                        NormalizedEvent.world_epoch == obs.world_epoch,
                        NormalizedEvent.event_id == str(ev_id),
                    )
                )
                evidence["event_present_in_inbox"] = exists is not None
                if exists is not None:
                    verified = True
                    recovery_source = "COMMERCE_WORKER"

        elif code == "D07":
            if obs.affected_payment_id:
                pay_data = connector.get_payment(obs.affected_payment_id)["data"]
                evidence["payment_status"] = pay_data.get("status")
                evidence["reconciliation_required"] = pay_data.get("reconciliation_required")
                if (
                    not bool(pay_data.get("reconciliation_required"))
                    and str(pay_data.get("status") or "").upper() != "UNKNOWN"
                ):
                    verified = True
                    recovery_source = "MERCHANT"
            elif obs.affected_order_id:
                ord_data = connector.get_order(obs.affected_order_id)["data"]
                evidence["reconciliation_status"] = ord_data.get("reconciliation_status")
                if str(ord_data.get("reconciliation_status") or "").upper() in (
                    "CLEAR",
                    "RESOLVED",
                ):
                    verified = True
                    recovery_source = "MERCHANT"

        elif code == "D08":
            settings = get_settings()
            metrics, _ = connector.get_metrics()
            evidence["outbox_lag_count"] = metrics.outbox_lag_count
            evidence["unsynced_payments"] = metrics.unsynced_payments
            if (
                metrics.outbox_lag_count < settings.monitor_outbox_lag_threshold
                and metrics.unsynced_payments < settings.monitor_unsynced_backlog_threshold
            ):
                verified = True
                recovery_source = "COMMERCE_WORKER"

        obs.verification_json = evidence
        obs.updated_at = now
        if verified:
            obs.lifecycle_state = "RECOVERED"
            obs.recovery_source = recovery_source
            obs.verified_recovered_at = now
            record_activity(
                db,
                session_id=obs.session_id,
                world_epoch=obs.world_epoch,
                kind="MONITOR_RECOVERY_VERIFIED",
                title_vi=f"[{obs.detector_code}] Recovery verified ({recovery_source})",
                detail_vi=(
                    f"Anomaly {obs.id} ({obs.anomaly_type}) verified recovered "
                    "against MiniStore HTTP state."
                ),
                case_id=obs.linked_case_id,
                metadata_json=evidence,
            )
            db.commit()
            return True, f"Verified recovered via {recovery_source}"

        db.commit()
        return False, "Postcondition not yet satisfied in authoritative MiniStore state"
    except CommerceConnectorError as exc:
        # NEVER mark recovered when Commerce is unreachable or times out!
        obs.verification_json = {
            "checked_at": now.isoformat(),
            "error": str(exc),
            "verified": False,
        }
        obs.updated_at = now
        db.commit()
        return False, f"Verification blocked (Commerce unavailable): {exc}"


def run_binding_scan(
    db: Session,
    *,
    binding: CommerceSourceBinding,
    connector: CommerceReadConnector | None = None,
    transport: httpx.BaseTransport | None = None,
    service_key: str | None = None,
    auto_create_cases: bool = True,
) -> ScanSummary:
    """Run one complete autonomous monitoring scan for a single `CommerceSourceBinding`."""
    settings = get_settings()
    summary = ScanSummary(binding_id=binding.id, connectivity_status=binding.connectivity_status)
    now = datetime.now(UTC)

    if not binding.is_enabled:
        summary.connectivity_status = "PAUSED"
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
        # 1. Pull events with bounded overlap & durable checkpointing
        pulled, new_ev, remote_events = collect_commerce_events(
            db, binding=binding, connector=conn, batch_size=settings.monitor_batch_size
        )
        summary.events_pulled = pulled
        summary.events_new = new_ev

        # 2. Fetch authoritative Commerce state via authenticated HTTP read endpoints
        orders, obs_at = conn.list_orders(limit=200)
        payments, _ = conn.list_payments(limit=200)
        refunds, _ = conn.list_refunds(limit=200)
        inventory, _ = conn.get_inventory()
        metrics, _ = conn.get_metrics()

        ingested_ids = set(
            db.scalars(
                select(NormalizedEvent.event_id).where(
                    NormalizedEvent.session_id == binding.session_id,
                    NormalizedEvent.world_epoch == binding.world_epoch,
                    NormalizedEvent.merchant_id == binding.merchant_id,
                )
            ).all()
        )

        snapshot = CommerceSnapshot(
            merchant_id=binding.merchant_id,
            commerce_session_id=binding.commerce_session_id,
            observed_at=obs_at,
            orders=orders,
            payments=payments,
            refunds=refunds,
            inventory=inventory,
            metrics=metrics,
            ingested_event_ids=ingested_ids,
            remote_events=remote_events,
        )

        # 3. Run all deterministic detectors D01..D08
        candidates = run_all_detectors(
            snapshot,
            payment_sync_grace_seconds=settings.monitor_payment_sync_grace_seconds,
            order_stall_seconds=settings.monitor_order_stall_seconds,
            outbox_lag_threshold=settings.monitor_outbox_lag_threshold,
            unsynced_backlog_threshold=settings.monitor_unsynced_backlog_threshold,
        )
        summary.anomalies_detected = len(candidates)

        for cand in candidates:
            _, created_case = record_or_update_anomaly(
                db, binding=binding, candidate=cand, auto_create_case=auto_create_cases
            )
            if created_case:
                summary.cases_created += 1

        # 4. Independently verify recovery for any open anomalies on this binding
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
            recovered, _ = verify_anomaly_recovery(db, obs=obs, connector=conn)
            if recovered:
                summary.recoveries_verified += 1

        binding.connectivity_status = "CONNECTED"
        binding.last_successful_scan_at = datetime.now(UTC)
        binding.last_heartbeat_at = datetime.now(UTC)
        binding.last_error = None
        binding.updated_at = datetime.now(UTC)
        db.commit()
        summary.connectivity_status = "CONNECTED"
        return summary

    except CommerceConnectorError as exc:
        binding.connectivity_status = "DISCONNECTED"
        binding.last_error = str(exc)[:500]
        binding.last_heartbeat_at = datetime.now(UTC)
        binding.updated_at = datetime.now(UTC)
        db.commit()
        summary.connectivity_status = "DISCONNECTED"
        summary.error = str(exc)
        return summary
