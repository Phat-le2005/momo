"""IncidentCorrelator and Prompt 05 Scenario Seeder (12 correlated cases + 2 unrelated lookalikes).

Hard Domain Invariants (Prompt 05 & Prompt 05.1 Hardening):
1. Same-Event Causal Evidence Correlation Only (QA I01):
   - Groups candidate Cases ONLY when a single authoritative `Evidence(kind="OBSERVED_FACT")`
     event carries the shared causal triad (`worker_id`, `batch_id`, `error_code`) within a
     contiguous bounded event time window (`<= 20 minutes`).
   - Never stitches `worker_id` from one event with `batch_id` or `error_code` from a different
     event.
   - Emits explicit correlation trace:
     "Detected 12 potentially linked cases by dropped-sync worker ID and time range".
2. False Merge Prevention (QA I02):
   - Never merges Cases solely because customer complaint text (`CLAIM`) is similar or identical.
   - Cases outside the 20-minute time cluster or with different causal OBSERVED_FACT metadata
     are excluded and reported in `rejected_lookalikes`.
3. Recursive Zero Oracle Leak (QA I01):
   - Recursively strips any simulator hidden-truth / oracle keys (`_oracle_*`, `hidden_truth`,
     `ground_truth`, `oracle_label`, `fault_injection_truth`, etc.) across both dictionaries
     AND lists/tuples.
4. Explainable Evidence Score:
   - Replaces fixed confidence numbers with a deterministic, explainable evidence score
     breakdown (`evidence_score_breakdown`) reflecting causal triad completeness, time-window
     cohesion, cluster size, provenance completeness, and payment status consistency.
5. No Auto-Resolution of Child Cases (QA I03):
   - Correlation creates `Incident` and `IncidentCase(is_auto_resolved=False)` records and
     logs `CaseEvent(type="INCIDENT_CORRELATED")`, but NEVER mutates `Case.state` to `RESOLVED`
     or `VERIFYING` and NEVER creates a `Verification` record.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import AppError, ErrorCode
from app.models.cases import Case, CaseEvent, Evidence
from app.models.enums import (
    CaseState,
    EvidenceKind,
    FulfillmentStatus,
    IncidentStatus,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
)
from app.models.incidents import Incident, IncidentCase
from app.models.session import DemoSession
from app.models.simulator import SimOrder, SimPayment

FORBIDDEN_ORACLE_KEYS: frozenset[str] = frozenset(
    {
        "oracle",
        "oracle_label",
        "oracle_cause",
        "oracle_truth",
        "hidden_truth",
        "ground_truth",
        "fault_injection_label",
        "fault_injection_truth",
        "internal_oracle_group",
    }
)

MAX_INCIDENT_TIME_WINDOW_SECONDS = 20 * 60  # 20 minutes
MIN_CORRELATED_CASES_FOR_INCIDENT = 2
MAX_INCIDENT_SCENARIO_SEEDS_PER_SESSION = 2


def _is_forbidden_oracle_key(key: str) -> bool:
    k_lower = str(key).strip().lower()
    return (
        k_lower.startswith("_oracle")
        or k_lower in FORBIDDEN_ORACLE_KEYS
        or "oracle_secret" in k_lower
        or "oracle_root" in k_lower
    )


def sanitize_no_oracle_leak(payload: Any) -> Any:
    """Recursively strip hidden simulator oracle keys across dictionaries, lists, and tuples."""
    if isinstance(payload, dict):
        cleaned: dict[str, Any] = {}
        for k, v in payload.items():
            if _is_forbidden_oracle_key(str(k)):
                continue
            cleaned[str(k)] = sanitize_no_oracle_leak(v)
        return cleaned
    if isinstance(payload, list):
        return [sanitize_no_oracle_leak(item) for item in payload]
    if isinstance(payload, tuple):
        return [sanitize_no_oracle_leak(item) for item in payload]
    return payload


@dataclass
class CausalObservation:
    case: Case
    claim_text: str
    evidence_refs: list[str]
    causal_event_evidence_id: str | None
    worker_id: str | None
    batch_id: str | None
    error_code: str | None
    provider: str | None
    order_id: str | None
    payment_id: str | None
    payment_status: str | None
    event_timestamp: datetime | None
    has_split_event_causal_fields: bool = False


@dataclass
class RejectedLookalikeItem:
    case_id: str
    order_id: str | None
    claim_text: str
    observed_worker_id: str | None
    observed_batch_id: str | None
    observed_error_code: str | None
    rejection_reason: str


@dataclass
class CorrelationOutcome:
    incidents: list[Incident] = field(default_factory=list)
    rejected_lookalikes: list[RejectedLookalikeItem] = field(default_factory=list)


def _parse_iso_timestamp(ts_raw: Any) -> datetime | None:
    if not isinstance(ts_raw, str) or not ts_raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(ts_raw.strip().replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def compute_explainable_evidence_score(
    members: list[CausalObservation],
) -> tuple[float, dict[str, Any]]:
    """Compute an explainable evidence score in [0.0, 1.0] from observed causal factors."""
    count = len(members)
    all_have_same_event_triad = all(
        bool(m.worker_id and m.batch_id and m.error_code and m.causal_event_evidence_id)
        for m in members
    )
    triad_score = 0.35 if all_have_same_event_triad else 0.15

    timestamps = [m.event_timestamp for m in members if m.event_timestamp is not None]
    missing_ts_count = count - len(timestamps)
    if len(timestamps) >= 2 and missing_ts_count == 0:
        span_seconds = max(0.0, (max(timestamps) - min(timestamps)).total_seconds())
        if span_seconds <= 600:
            time_score = 0.25
        elif span_seconds <= MAX_INCIDENT_TIME_WINDOW_SECONDS:
            time_score = 0.18
        else:
            time_score = 0.05
    else:
        span_seconds = None
        time_score = 0.05

    if count >= 10:
        size_score = 0.15
    elif count >= 4:
        size_score = 0.10
    else:
        size_score = 0.05

    observed_statuses = sorted(
        {m.payment_status if m.payment_status else "UNKNOWN" for m in members}
    )
    if observed_statuses == [PaymentStatus.SUCCESS.value]:
        payment_consistency_score = 0.15
        payment_consistency_label = "UNIFORM_OBSERVED_SUCCESS"
    elif len(observed_statuses) == 1 and observed_statuses[0] != "UNKNOWN":
        payment_consistency_score = 0.08
        payment_consistency_label = f"UNIFORM_{observed_statuses[0]}"
    else:
        # Mixed or unknown payment statuses reduce confidence and require per-case scrutiny
        payment_consistency_score = -0.08
        payment_consistency_label = f"MIXED_OR_UNKNOWN_PAYMENT_STATUSES:{observed_statuses}"

    complete_provenance_count = sum(
        1
        for m in members
        if m.order_id and m.payment_id and m.event_timestamp is not None and m.evidence_refs
    )
    provenance_ratio = complete_provenance_count / max(1, count)
    provenance_score = round(0.10 * provenance_ratio, 3)

    raw_total = triad_score + time_score + size_score + payment_consistency_score + provenance_score
    final_score = round(min(0.99, max(0.10, raw_total)), 2)

    breakdown: dict[str, Any] = {
        "final_score": final_score,
        "scoring_method": "EXPLAINABLE_CAUSAL_EVIDENCE_RUBRIC_V1",
        "components": {
            "same_event_causal_triad_score": triad_score,
            "time_window_cohesion_score": time_score,
            "cluster_size_support_score": size_score,
            "payment_status_consistency_score": payment_consistency_score,
            "provenance_completeness_score": provenance_score,
        },
        "metrics": {
            "member_cases_count": count,
            "all_have_same_event_triad": all_have_same_event_triad,
            "time_span_seconds": span_seconds,
            "missing_timestamp_count": missing_ts_count,
            "observed_payment_statuses": observed_statuses,
            "payment_consistency_label": payment_consistency_label,
            "complete_provenance_cases": complete_provenance_count,
        },
    }
    return final_score, breakdown


class IncidentCorrelator:
    """Correlates session Cases by shared causal OBSERVED_FACT evidence without oracle leakage."""

    def __init__(self, db: Session, session_id: str) -> None:
        self._db = db
        self._session_id = session_id

    def extract_causal_observation(self, case: Case) -> CausalObservation:
        """Extract causal observation for a Case.
        Requires `worker_id`, `batch_id`, and `error_code` to come from the SAME `OBSERVED_FACT`
        evidence record so unrelated events on a Case are never stitched together."""
        claim_text = ""
        ev_refs: list[str] = []

        causal_ev_id: str | None = None
        worker_id: str | None = None
        batch_id: str | None = None
        error_code: str | None = None
        provider: str | None = None
        order_id: str | None = None
        payment_id: str | None = None
        payment_status: str | None = None
        event_ts: datetime | None = None

        partial_workers: list[str] = []
        partial_batches: list[str] = []
        partial_errors: list[str] = []

        for ev in case.evidence:
            if ev.kind == EvidenceKind.CLAIM.value:
                txt = ev.payload_json.get("text")
                if isinstance(txt, str) and txt.strip():
                    claim_text = txt.strip()
                continue

            if ev.kind != EvidenceKind.OBSERVED_FACT.value:
                continue

            safe_payload: dict[str, Any] = sanitize_no_oracle_leak(dict(ev.payload_json or {}))

            ev_w_raw = safe_payload.get("worker_id") or safe_payload.get("sync_worker_id")
            ev_b_raw = safe_payload.get("batch_id") or safe_payload.get("sync_batch_id")
            ev_e_raw = safe_payload.get("error_code") or safe_payload.get("sync_error_code")

            ev_w = ev_w_raw.strip() if isinstance(ev_w_raw, str) and ev_w_raw.strip() else None
            ev_b = ev_b_raw.strip() if isinstance(ev_b_raw, str) and ev_b_raw.strip() else None
            ev_e = ev_e_raw.strip() if isinstance(ev_e_raw, str) and ev_e_raw.strip() else None
            ev_ts = _parse_iso_timestamp(safe_payload.get("event_timestamp"))

            if ev_w:
                partial_workers.append(ev_w)
            if ev_b:
                partial_batches.append(ev_b)
            if ev_e:
                partial_errors.append(ev_e)

            # Same-event requirement: worker_id, batch_id, and error_code must all exist on
            # the SAME OBSERVED_FACT record to qualify as a valid causal correlation event.
            # When a Case receives a newer corrective OBSERVED_FACT event (ordered ascending by
            # observed_at), the latest same-event triad supersedes earlier ones.
            if ev_w and ev_b and ev_e:
                causal_ev_id = ev.id
                worker_id = ev_w
                batch_id = ev_b
                error_code = ev_e
                event_ts = ev_ts
                if ev.id not in ev_refs:
                    ev_refs.append(ev.id)

                p_val = safe_payload.get("provider")
                if isinstance(p_val, str) and p_val.strip():
                    provider = p_val.strip()
                o_val = safe_payload.get("order_id")
                if isinstance(o_val, str) and o_val.strip():
                    order_id = o_val.strip()
                pay_val = safe_payload.get("payment_id")
                if isinstance(pay_val, str) and pay_val.strip():
                    payment_id = pay_val.strip()
                ps_val = safe_payload.get("payment_status") or safe_payload.get("status")
                if isinstance(ps_val, str) and ps_val.strip() and "payment_id" in safe_payload:
                    payment_status = ps_val.strip()
            else:
                # Secondary OBSERVED_FACT records on the same Case may provide order/payment
                # context only if they reference the same order/payment, never stitching triad keys.
                o_val = safe_payload.get("order_id")
                if order_id is None and isinstance(o_val, str) and o_val.strip():
                    order_id = o_val.strip()
                pay_val = safe_payload.get("payment_id")
                if payment_id is None and isinstance(pay_val, str) and pay_val.strip():
                    payment_id = pay_val.strip()
                ps_val = safe_payload.get("payment_status") or safe_payload.get("status")
                if (
                    payment_status is None
                    and isinstance(ps_val, str)
                    and ps_val.strip()
                    and "payment_id" in safe_payload
                ):
                    payment_status = ps_val.strip()
                if ev.source in ("ORDER_EVENT_LOG_DB", "PROVIDER_LEDGER_MOCK", "SIM_ORDERS_DB"):
                    if ev.id not in ev_refs:
                        ev_refs.append(ev.id)

        has_split = bool(
            causal_ev_id is None and (partial_workers or partial_batches or partial_errors)
        )
        return CausalObservation(
            case=case,
            claim_text=claim_text,
            evidence_refs=ev_refs,
            causal_event_evidence_id=causal_ev_id,
            worker_id=worker_id or (partial_workers[0] if partial_workers else None),
            batch_id=batch_id or (partial_batches[0] if partial_batches else None),
            error_code=error_code or (partial_errors[0] if partial_errors else None),
            provider=provider,
            order_id=order_id,
            payment_id=payment_id,
            payment_status=payment_status,
            event_timestamp=event_ts,
            has_split_event_causal_fields=has_split,
        )

    @staticmethod
    def _cluster_by_time_window(
        bucket: list[CausalObservation],
    ) -> tuple[list[list[CausalObservation]], list[CausalObservation]]:
        """Partition a `(worker_id, batch_id, error_code)` bucket into contiguous time-window
        clusters where `(cluster_max_ts - cluster_min_ts) <= MAX_INCIDENT_TIME_WINDOW_SECONDS`.
        Observations lacking a valid `event_timestamp` are never blindly grouped into a time
        cluster."""
        with_ts = [b for b in bucket if b.event_timestamp is not None]
        without_ts = [b for b in bucket if b.event_timestamp is None]

        if not with_ts:
            return [], without_ts

        with_ts.sort(
            key=lambda x: (x.event_timestamp or datetime.min.replace(tzinfo=UTC), x.case.id)
        )

        clusters: list[list[CausalObservation]] = []
        current_cluster: list[CausalObservation] = []
        cluster_start_ts: datetime | None = None

        for obs in with_ts:
            assert obs.event_timestamp is not None  # noqa: S101
            if not current_cluster or cluster_start_ts is None:
                current_cluster = [obs]
                cluster_start_ts = obs.event_timestamp
            else:
                span = (obs.event_timestamp - cluster_start_ts).total_seconds()
                if span <= MAX_INCIDENT_TIME_WINDOW_SECONDS:
                    current_cluster.append(obs)
                else:
                    clusters.append(current_cluster)
                    current_cluster = [obs]
                    cluster_start_ts = obs.event_timestamp

        if current_cluster:
            clusters.append(current_cluster)

        valid_clusters: list[list[CausalObservation]] = []
        outliers: list[CausalObservation] = list(without_ts)
        for cl in clusters:
            if len(cl) >= MIN_CORRELATED_CASES_FOR_INCIDENT:
                valid_clusters.append(cl)
            else:
                outliers.extend(cl)

        return valid_clusters, outliers

    def correlate_session_cases(self) -> CorrelationOutcome:
        """Group session Cases by same-event shared causal OBSERVED_FACT metadata (`worker_id`,
        `batch_id`, `error_code`) and bounded time-window clustering."""
        cases = list(
            self._db.scalars(
                select(Case)
                .options(selectinload(Case.evidence))
                .where(Case.session_id == self._session_id)
                .order_by(Case.created_at.asc(), Case.id.asc())
            ).all()
        )

        observations = [self.extract_causal_observation(c) for c in cases]

        causal_buckets: dict[tuple[str, str, str], list[CausalObservation]] = {}
        unbucketed: list[CausalObservation] = []

        for obs in observations:
            if (
                obs.causal_event_evidence_id is not None
                and obs.worker_id
                and obs.batch_id
                and obs.error_code
                and not obs.has_split_event_causal_fields
            ):
                key = (obs.worker_id, obs.batch_id, obs.error_code)
                causal_buckets.setdefault(key, []).append(obs)
            else:
                unbucketed.append(obs)

        created_or_updated_incidents: list[Incident] = []
        correlated_case_ids: set[str] = set()
        primary_incident_key: tuple[str, str, str] | None = None
        primary_window_range: tuple[datetime, datetime] | None = None

        for (worker_id, batch_id, error_code), bucket in causal_buckets.items():
            valid_clusters, time_outliers = self._cluster_by_time_window(bucket)
            unbucketed.extend(time_outliers)

            for cluster_idx, window_members in enumerate(valid_clusters):
                if primary_incident_key is None:
                    primary_incident_key = (worker_id, batch_id, error_code)

                member_ts = [
                    m.event_timestamp for m in window_members if m.event_timestamp is not None
                ]
                min_ts = min(member_ts)
                max_ts = max(member_ts)
                if primary_window_range is None:
                    primary_window_range = (min_ts, max_ts)

                ts_start = min_ts.isoformat()
                ts_end = max_ts.isoformat()

                base_corr_key = f"worker:{worker_id}|batch:{batch_id}|err:{error_code}"
                corr_key = (
                    base_corr_key
                    if cluster_idx == 0
                    else f"{base_corr_key}|win:{min_ts.strftime('%Y%m%d%H%M%S')}"
                )

                all_ev_refs: list[str] = []
                for m in window_members:
                    for ref in m.evidence_refs:
                        if ref not in all_ev_refs:
                            all_ev_refs.append(ref)

                count = len(window_members)
                score, score_breakdown = compute_explainable_evidence_score(window_members)
                observed_pay_statuses = score_breakdown["metrics"]["observed_payment_statuses"]

                trace_msg = (
                    f"Detected {count} potentially linked cases by dropped-sync worker ID "
                    f"and time range (worker_id={worker_id}, batch_id={batch_id}, "
                    f"error_code={error_code}, window={ts_start}..{ts_end}, "
                    f"evidence_score={score:.2f})"
                )
                hypothesis_vi = (
                    f"Giả thuyết sự cố (Hypothesis - điểm bằng chứng {score:.2f}, cần kiểm chứng "
                    f"độc lập từng Case): Nhóm {count} Case cùng chia sẻ sự kiện OBSERVED_FACT "
                    f"tại worker '{worker_id}', lô '{batch_id}', mã lỗi '{error_code}' trong "
                    f"khung giờ {ts_start} .. {ts_end} (trạng thái thanh toán quan sát được: "
                    f"{observed_pay_statuses}). Không tự động xác thực hoặc đóng các Case con."
                )

                existing_inc = self._db.scalar(
                    select(Incident)
                    .options(selectinload(Incident.incident_cases))
                    .where(
                        Incident.session_id == self._session_id,
                        Incident.correlation_key == corr_key,
                    )
                )
                now = datetime.now(UTC)
                shared_factors: dict[str, Any] = sanitize_no_oracle_leak(
                    {
                        "worker_id": worker_id,
                        "batch_id": batch_id,
                        "error_code": error_code,
                        "provider": window_members[0].provider or "MOCK_MOMO",
                        "time_window_start": ts_start,
                        "time_window_end": ts_end,
                        "linked_cases_count": count,
                        "observed_payment_statuses": observed_pay_statuses,
                        "correlation_basis": "SAME_EVENT_CAUSAL_OBSERVED_FACT_METADATA",
                        "pure_text_similarity_used": False,
                        "evidence_score_breakdown": score_breakdown,
                    }
                )

                if existing_inc is None:
                    incident = Incident(
                        id=f"INC-{uuid.uuid4().hex[:10].upper()}",
                        session_id=self._session_id,
                        title=(
                            f"Sự cố nghẽn đồng bộ đơn hàng ({worker_id} / {batch_id} / "
                            f"{count} Cases)"
                        ),
                        hypothesis=hypothesis_vi,
                        status=IncidentStatus.INVESTIGATING.value,
                        correlation_key=corr_key,
                        correlation_trace=trace_msg,
                        shared_factors_json=shared_factors,
                        evidence_refs=all_ev_refs,
                        confidence=score,
                        created_at=now,
                        updated_at=now,
                    )
                    self._db.add(incident)
                    self._db.flush()
                else:
                    incident = existing_inc
                    incident.hypothesis = hypothesis_vi
                    incident.correlation_trace = trace_msg
                    incident.shared_factors_json = shared_factors
                    incident.evidence_refs = all_ev_refs
                    incident.confidence = score
                    incident.updated_at = now
                    self._db.flush()

                existing_case_map = {ic.case_id: ic for ic in (incident.incident_cases or [])}
                current_member_ids = {m.case.id for m in window_members}

                # Reconcile stale IncidentCase memberships on repeated correlation:
                # remove stale links and append an audit trail CaseEvent(INCIDENT_UNLINKED).
                removed_stale_ids: list[str] = []
                for stale_case_id, stale_ic in list(existing_case_map.items()):
                    if stale_case_id not in current_member_ids:
                        removed_stale_ids.append(stale_case_id)
                        self._db.delete(stale_ic)
                        self._db.add(
                            CaseEvent(
                                id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                                case_id=stale_case_id,
                                session_id=self._session_id,
                                type="INCIDENT_UNLINKED",
                                payload_json={
                                    "incident_id": incident.id,
                                    "correlation_key": corr_key,
                                    "reason": "STALE_LINK_REMOVED_ON_RECORRELATION",
                                    "auto_resolved": False,
                                },
                                created_at=now,
                            )
                        )
                if removed_stale_ids:
                    sf_reconciled = dict(incident.shared_factors_json or {})
                    prior_removed = list(sf_reconciled.get("removed_stale_case_ids") or [])
                    sf_reconciled["removed_stale_case_ids"] = sorted(
                        set(prior_removed) | set(removed_stale_ids)
                    )
                    incident.shared_factors_json = sf_reconciled
                    incident.title = (
                        f"Sự cố nghẽn đồng bộ đơn hàng ({worker_id} / {batch_id} / {count} Cases)"
                    )

                for m in window_members:
                    correlated_case_ids.add(m.case.id)
                    causal_json = sanitize_no_oracle_leak(
                        {
                            "causal_event_evidence_id": m.causal_event_evidence_id,
                            "worker_id": worker_id,
                            "batch_id": batch_id,
                            "error_code": error_code,
                            "order_id": m.order_id,
                            "payment_id": m.payment_id,
                            "observed_payment_status": m.payment_status or "UNKNOWN",
                            "event_timestamp": (
                                m.event_timestamp.isoformat() if m.event_timestamp else None
                            ),
                        }
                    )
                    if m.case.id not in existing_case_map:
                        ic = IncidentCase(
                            id=f"IC-{uuid.uuid4().hex[:10].upper()}",
                            incident_id=incident.id,
                            case_id=m.case.id,
                            session_id=self._session_id,
                            link_reason=(
                                f"Same-event causal OBSERVED_FACT ({m.causal_event_evidence_id}): "
                                f"worker_id={worker_id}, batch_id={batch_id}, "
                                f"error_code={error_code}"
                            ),
                            shared_evidence_refs=m.evidence_refs,
                            causal_factors_json=causal_json,
                            is_auto_resolved=False,
                            created_at=now,
                        )
                        self._db.add(ic)
                        self._db.add(
                            CaseEvent(
                                id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                                case_id=m.case.id,
                                session_id=self._session_id,
                                type="INCIDENT_CORRELATED",
                                payload_json={
                                    "incident_id": incident.id,
                                    "correlation_key": corr_key,
                                    "correlation_trace": trace_msg,
                                    "evidence_score": score,
                                    "shared_evidence_refs": m.evidence_refs,
                                    "auto_resolved": False,
                                },
                                created_at=now,
                            )
                        )
                    else:
                        existing_ic = existing_case_map[m.case.id]
                        existing_ic.causal_factors_json = causal_json
                        existing_ic.shared_evidence_refs = m.evidence_refs
                        existing_ic.link_reason = (
                            f"Same-event causal OBSERVED_FACT ({m.causal_event_evidence_id}): "
                            f"worker_id={worker_id}, batch_id={batch_id}, "
                            f"error_code={error_code}"
                        )

                self._db.flush()
                created_or_updated_incidents.append(incident)

        # Reconcile previously existing session Incidents that no longer have a valid cluster
        # (e.g., a 2-case Incident dropped to < 2 Cases after evidence update).
        # Remove stale IncidentCase rows, preserve Incident + CaseEvent audit trail, and NEVER
        # auto-resolve child Cases.
        active_incident_ids = {inc.id for inc in created_or_updated_incidents}
        all_session_incidents = list(
            self._db.scalars(
                select(Incident)
                .options(selectinload(Incident.incident_cases))
                .where(Incident.session_id == self._session_id)
            ).all()
        )
        reconcile_now = datetime.now(UTC)
        for old_inc in all_session_incidents:
            if old_inc.id in active_incident_ids:
                continue
            stale_links = list(old_inc.incident_cases or [])
            if not stale_links:
                continue
            removed_from_old: list[str] = []
            for stale_ic in stale_links:
                removed_from_old.append(stale_ic.case_id)
                self._db.delete(stale_ic)
                self._db.add(
                    CaseEvent(
                        id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                        case_id=stale_ic.case_id,
                        session_id=self._session_id,
                        type="INCIDENT_UNLINKED",
                        payload_json={
                            "incident_id": old_inc.id,
                            "correlation_key": old_inc.correlation_key,
                            "reason": "INCIDENT_CLUSTER_NO_LONGER_VALID_ON_RECORRELATION",
                            "auto_resolved": False,
                        },
                        created_at=reconcile_now,
                    )
                )
            old_sf = dict(old_inc.shared_factors_json or {})
            prior_removed = list(old_sf.get("removed_stale_case_ids") or [])
            old_sf["removed_stale_case_ids"] = sorted(set(prior_removed) | set(removed_from_old))
            old_sf["linked_cases_count"] = 0
            old_inc.shared_factors_json = old_sf
            old_inc.status = IncidentStatus.CLOSED.value
            old_inc.updated_at = reconcile_now
        self._db.flush()

        rejected_lookalikes: list[RejectedLookalikeItem] = []
        for obs in unbucketed:
            if obs.case.id in correlated_case_ids:
                continue
            has_lookalike_complaint = any(
                kw in obs.claim_text.lower()
                for kw in ("chờ thanh toán", "đã thanh toán", "trừ tiền", "momo", "đơn hàng")
            )
            if not has_lookalike_complaint and not obs.has_split_event_causal_fields:
                continue

            diff_parts: list[str] = []
            if obs.has_split_event_causal_fields:
                diff_parts.append(
                    "causal fields (worker_id/batch_id/error_code) were split across separate "
                    "OBSERVED_FACT events instead of a single causal event"
                )
            elif primary_incident_key is not None:
                p_worker, p_batch, p_err = primary_incident_key
                if obs.worker_id != p_worker:
                    diff_parts.append(f"worker_id='{obs.worker_id}' != '{p_worker}'")
                if obs.batch_id != p_batch:
                    diff_parts.append(f"batch_id='{obs.batch_id}' != '{p_batch}'")
                if obs.error_code != p_err:
                    diff_parts.append(f"error_code='{obs.error_code}' != '{p_err}'")
                if (
                    not diff_parts
                    and primary_window_range is not None
                    and obs.event_timestamp is not None
                ):
                    w_start, w_end = primary_window_range
                    diff_parts.append(
                        f"event_timestamp '{obs.event_timestamp.isoformat()}' outside "
                        f"incident window '{w_start.isoformat()}..{w_end.isoformat()}'"
                    )
                if obs.payment_status and obs.payment_status != PaymentStatus.SUCCESS.value:
                    diff_parts.append(f"payment_status='{obs.payment_status}' != 'SUCCESS'")

            diff_summary = "; ".join(diff_parts) or (
                f"worker_id='{obs.worker_id}', batch_id='{obs.batch_id}', "
                f"error_code='{obs.error_code}'"
            )

            rejected_lookalikes.append(
                RejectedLookalikeItem(
                    case_id=obs.case.id,
                    order_id=obs.order_id,
                    claim_text=obs.claim_text,
                    observed_worker_id=obs.worker_id,
                    observed_batch_id=obs.batch_id,
                    observed_error_code=obs.error_code,
                    rejection_reason=(
                        "FALSE_MERGE_PREVENTED: Customer complaint text is similar, but causal "
                        f"OBSERVED_FACT metadata differs ({diff_summary}). Never merge by text."
                    ),
                )
            )

        for inc in created_or_updated_incidents:
            sf = dict(inc.shared_factors_json or {})
            sf["rejected_lookalikes"] = [
                {
                    "case_id": r.case_id,
                    "order_id": r.order_id,
                    "observed_worker_id": r.observed_worker_id,
                    "observed_batch_id": r.observed_batch_id,
                    "observed_error_code": r.observed_error_code,
                    "rejection_reason": r.rejection_reason,
                }
                for r in rejected_lookalikes
            ]
            inc.shared_factors_json = sf

        self._db.flush()
        return CorrelationOutcome(
            incidents=created_or_updated_incidents,
            rejected_lookalikes=rejected_lookalikes,
        )


def seed_incident_taskforce_scenario(
    db: Session,
    session_id: str,
    *,
    force_new: bool = False,
) -> dict[str, Any]:
    """Seed the Prompt 05 demonstration scenario:
    - 12 correlated Cases sharing same-event causal OBSERVED_FACT metadata:
      `worker_id="SYNC_WORKER_W07"`, `batch_id="BATCH_20261122_0900"`,
      `error_code="ERR_DROPPED_WEBHOOK_SYNC"`, event timestamps within 09:00..09:05 UTC.
    - 2 unrelated lookalike Cases with nearly identical Vietnamese customer complaint text
      ("Tôi đã thanh toán 250.000đ qua MoMo nhưng đơn hàng vẫn báo chờ thanh toán...")
      but completely different root causes in OBSERVED_FACT.
    """
    demo_session = db.scalar(
        select(DemoSession).where(DemoSession.id == session_id).with_for_update()
    )
    if demo_session is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"DemoSession '{session_id}' not found",
            status_code=404,
        )

    existing_incidents = list(
        db.scalars(
            select(Incident)
            .options(selectinload(Incident.incident_cases), selectinload(Incident.tasks))
            .where(Incident.session_id == session_id)
            .order_by(Incident.created_at.desc())
        ).all()
    )
    existing_lookalikes = list(
        db.scalars(
            select(Case)
            .where(
                Case.session_id == session_id,
                Case.id.like("CASE-LOOKALIKE-%"),
            )
            .order_by(Case.id.asc())
        ).all()
    )

    if not force_new and existing_incidents and len(existing_lookalikes) >= 2:
        latest_inc = existing_incidents[0]
        return {
            "idempotent_replay": True,
            "incident_id": latest_inc.id,
            "correlation_trace": latest_inc.correlation_trace,
            "correlated_case_ids": [ic.case_id for ic in latest_inc.incident_cases],
            "unrelated_lookalike_case_ids": [c.id for c in existing_lookalikes[:2]],
            "assigned_task_ids": [t.id for t in latest_inc.tasks],
        }

    if len(existing_incidents) >= MAX_INCIDENT_SCENARIO_SEEDS_PER_SESSION:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            f"Incident scenario seed limit exceeded ({MAX_INCIDENT_SCENARIO_SEEDS_PER_SESSION} "
            "per session).",
            status_code=429,
        )

    if demo_session.case_count + 14 > demo_session.max_cases:
        demo_session.max_cases = max(demo_session.max_cases, demo_session.case_count + 20)

    now = datetime.now(UTC)
    base_ts = datetime(2026, 11, 22, 9, 0, 0, tzinfo=UTC)
    suffix = uuid.uuid4().hex[:6].upper()

    shared_worker = "SYNC_WORKER_W07"
    shared_batch = "BATCH_20261122_0900"
    shared_error = "ERR_DROPPED_WEBHOOK_SYNC"

    correlated_case_ids: list[str] = []

    for idx in range(1, 13):
        tag = f"{idx:02d}-{suffix}"
        ord_id = f"ORD-INC-SYNC-{tag}"
        pay_id = f"PAY-INC-SYNC-{tag}"
        case_id = f"CASE-INC-SYNC-{tag}"
        event_ts = base_ts + timedelta(seconds=idx * 25)

        sim_ord = SimOrder(
            id=ord_id,
            session_id=session_id,
            customer_name=f"Khách hàng Sự cố #{idx:02d}",
            customer_phone=f"0978100{idx:03d}",
            amount=250000,
            currency="VND",
            subtotal=250000,
            discount=0,
            tax=0,
            shipping=0,
            grand_total=250000,
            status=OrderStatus.PENDING.value,
            payment_status=OrderPaymentStatus.UNPAID.value,
            fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
            version=1,
            source_mode="MOCK",
            created_at=event_ts,
            updated_at=event_ts,
        )
        sim_pay = SimPayment(
            id=pay_id,
            session_id=session_id,
            order_id=ord_id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref=f"MOMO_INC_TXN_{tag}",
            provider_order_id=f"MOMO_INC_ORD_{tag}",
            request_id=f"REQ_INC_{tag}",
            result_code=0,
            amount=250000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=event_ts,
            observed_at=event_ts,
            verification_source="MOCK_MOMO_ADAPTER_VERIFIED",
            source_mode="MOCK",
            created_at=event_ts,
        )
        case_obj = Case(
            id=case_id,
            session_id=session_id,
            type="PAYMENT_DISPUTE",
            goal=f"Kiểm tra đồng bộ thanh toán đơn hàng {ord_id}",
            state=CaseState.NEW.value,
            version=1,
            owner="guest",
            created_at=now,
            updated_at=now,
        )
        db.add_all([sim_ord, sim_pay, case_obj])
        db.flush()

        ev_claim = Evidence(
            id=f"EVD-INC-CLM-{tag}",
            case_id=case_id,
            session_id=session_id,
            kind=EvidenceKind.CLAIM.value,
            source="CUSTOMER_MESSAGE",
            source_ref="intake_text",
            payload_json={
                "text": (
                    f"Tôi đã thanh toán 250.000đ qua MoMo cho đơn hàng {ord_id} "
                    "nhưng ứng dụng vẫn báo chờ thanh toán, đề nghị kiểm tra gấp!"
                )
            },
            observed_at=event_ts,
        )
        ev_fact = Evidence(
            id=f"EVD-INC-FCT-{tag}",
            case_id=case_id,
            session_id=session_id,
            kind=EvidenceKind.OBSERVED_FACT.value,
            source="ORDER_EVENT_LOG_DB",
            source_ref=f"sim_orders:{ord_id}",
            payload_json={
                "order_id": ord_id,
                "payment_id": pay_id,
                "provider": "MOCK_MOMO",
                "payment_status": "SUCCESS",
                "order_status": "PENDING",
                "worker_id": shared_worker,
                "batch_id": shared_batch,
                "error_code": shared_error,
                "event_timestamp": event_ts.isoformat(),
                "amount": 250000,
                "currency": "VND",
            },
            confidence=1.0,
            observed_at=event_ts,
        )
        db.add_all([ev_claim, ev_fact])
        correlated_case_ids.append(case_id)

    lookalike_specs: list[tuple[int, str, str, str, str, int, datetime]] = [
        (
            1,
            "CHECKOUT_GATEWAY_W02",
            "BATCH_20261122_1145",
            "ERR_USER_OTP_EXPIRED",
            PaymentStatus.FAILED.value,
            1006,
            base_ts + timedelta(hours=2, minutes=45),
        ),
        (
            2,
            "PARTNER_CALLBACK_W19",
            "BATCH_20261121_2215",
            "ERR_MERCHANT_SIGNATURE_MISMATCH",
            PaymentStatus.PENDING.value,
            7002,
            base_ts - timedelta(hours=10, minutes=45),
        ),
    ]

    lookalike_case_ids: list[str] = []
    for l_idx, l_worker, l_batch, l_err, l_pay_status, l_res_code, l_ts in lookalike_specs:
        l_tag = f"0{l_idx}-{suffix}"
        l_ord_id = f"ORD-LOOKALIKE-{l_tag}"
        l_pay_id = f"PAY-LOOKALIKE-{l_tag}"
        l_case_id = f"CASE-LOOKALIKE-{l_tag}"

        l_ord = SimOrder(
            id=l_ord_id,
            session_id=session_id,
            customer_name=f"Khách hàng Lookalike #{l_idx}",
            customer_phone=f"097899900{l_idx}",
            amount=250000,
            currency="VND",
            subtotal=250000,
            discount=0,
            tax=0,
            shipping=0,
            grand_total=250000,
            status=OrderStatus.PENDING.value,
            payment_status=OrderPaymentStatus.UNPAID.value,
            fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
            version=1,
            source_mode="MOCK",
            created_at=l_ts,
            updated_at=l_ts,
        )
        l_pay = SimPayment(
            id=l_pay_id,
            session_id=session_id,
            order_id=l_ord_id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref=f"MOMO_LOOK_TXN_{l_tag}",
            provider_order_id=f"MOMO_LOOK_ORD_{l_tag}",
            request_id=f"REQ_LOOK_{l_tag}",
            result_code=l_res_code,
            amount=250000,
            currency="VND",
            status=l_pay_status,
            verified_at=None,
            observed_at=l_ts,
            verification_source=None,
            source_mode="MOCK",
            created_at=l_ts,
        )
        l_case = Case(
            id=l_case_id,
            session_id=session_id,
            type="PAYMENT_DISPUTE",
            goal=f"Kiểm tra đồng bộ thanh toán đơn hàng {l_ord_id}",
            state=CaseState.NEW.value,
            version=1,
            owner="guest",
            created_at=now,
            updated_at=now,
        )
        db.add_all([l_ord, l_pay, l_case])
        db.flush()

        l_claim = Evidence(
            id=f"EVD-LOOK-CLM-{l_tag}",
            case_id=l_case_id,
            session_id=session_id,
            kind=EvidenceKind.CLAIM.value,
            source="CUSTOMER_MESSAGE",
            source_ref="intake_text",
            payload_json={
                "text": (
                    f"Tôi đã thanh toán 250.000đ qua MoMo cho đơn hàng {l_ord_id} "
                    "nhưng ứng dụng vẫn báo chờ thanh toán, đề nghị kiểm tra gấp!"
                )
            },
            observed_at=l_ts,
        )
        l_fact = Evidence(
            id=f"EVD-LOOK-FCT-{l_tag}",
            case_id=l_case_id,
            session_id=session_id,
            kind=EvidenceKind.OBSERVED_FACT.value,
            source="ORDER_EVENT_LOG_DB",
            source_ref=f"sim_orders:{l_ord_id}",
            payload_json={
                "order_id": l_ord_id,
                "payment_id": l_pay_id,
                "provider": "MOCK_MOMO",
                "payment_status": l_pay_status,
                "order_status": "PENDING",
                "worker_id": l_worker,
                "batch_id": l_batch,
                "error_code": l_err,
                "event_timestamp": l_ts.isoformat(),
                "amount": 250000,
                "currency": "VND",
            },
            confidence=1.0,
            observed_at=l_ts,
        )
        db.add_all([l_claim, l_fact])
        lookalike_case_ids.append(l_case_id)

    demo_session.case_count += 14
    db.flush()

    from app.incidents.taskforce import TaskforceManager

    correlator = IncidentCorrelator(db, session_id)
    outcome = correlator.correlate_session_cases()
    assert len(outcome.incidents) >= 1  # noqa: S101
    primary_inc = outcome.incidents[0]

    tf_manager = TaskforceManager(db, session_id)
    tasks = tf_manager.assign_and_run_taskforce(primary_inc)
    db.commit()

    return {
        "idempotent_replay": False,
        "incident_id": primary_inc.id,
        "correlation_trace": primary_inc.correlation_trace,
        "correlated_case_ids": correlated_case_ids,
        "unrelated_lookalike_case_ids": lookalike_case_ids,
        "assigned_task_ids": [t.id for t in tasks],
    }
