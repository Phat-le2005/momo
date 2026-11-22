"""IncidentCorrelator and Prompt 05 Scenario Seeder (12 correlated cases + 2 unrelated lookalikes).

Hard Domain Invariants:
1. Causal Evidence Correlation Only (QA I01):
   - Groups candidate Cases ONLY when authoritative `Evidence(kind="OBSERVED_FACT")` shares
     causal operational metadata: `worker_id`, `batch_id`, `error_code`, and bounded event
     timestamp window (`<= 20 minutes`).
   - Emits explicit correlation trace:
     "Detected 12 potentially linked cases by dropped-sync worker ID and time range".
2. False Merge Prevention (QA I02):
   - Never merges Cases solely because customer complaint text (`CLAIM`) is similar or identical.
   - Lookalike Cases with identical Vietnamese complaint text but different causal OBSERVED_FACT
     metadata (e.g. failed payment OTP or different worker/batch/error_code) are explicitly
     excluded and reported in `rejected_lookalikes`.
3. Zero Oracle Leak (QA I01):
   - Strips and forbids any simulator hidden-truth / oracle keys (`_oracle_*`, `hidden_truth`,
     `ground_truth`, `oracle_label`, `fault_injection_truth`) from correlation inputs, persisted
     records, and API outputs.
4. No Auto-Resolution of Child Cases (QA I03):
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


def sanitize_no_oracle_leak(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip any hidden simulator oracle keys so correlation and APIs never leak ground truth."""
    cleaned: dict[str, Any] = {}
    for k, v in payload.items():
        k_lower = str(k).lower()
        if k_lower.startswith("_oracle") or k_lower in FORBIDDEN_ORACLE_KEYS:
            continue
        if isinstance(v, dict):
            cleaned[k] = sanitize_no_oracle_leak(v)
        else:
            cleaned[k] = v
    return cleaned


@dataclass
class CausalObservation:
    case: Case
    claim_text: str
    evidence_refs: list[str]
    worker_id: str | None
    batch_id: str | None
    error_code: str | None
    provider: str | None
    order_id: str | None
    payment_id: str | None
    payment_status: str | None
    event_timestamp: datetime | None


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


class IncidentCorrelator:
    """Correlates session Cases by shared causal OBSERVED_FACT evidence without oracle leakage."""

    def __init__(self, db: Session, session_id: str) -> None:
        self._db = db
        self._session_id = session_id

    def extract_causal_observation(self, case: Case) -> CausalObservation:
        claim_text = ""
        ev_refs: list[str] = []
        worker_id: str | None = None
        batch_id: str | None = None
        error_code: str | None = None
        provider: str | None = None
        order_id: str | None = None
        payment_id: str | None = None
        payment_status: str | None = None
        event_ts: datetime | None = None

        for ev in case.evidence:
            if ev.kind == EvidenceKind.CLAIM.value:
                txt = ev.payload_json.get("text")
                if isinstance(txt, str) and txt.strip():
                    claim_text = txt.strip()
                continue

            if ev.kind != EvidenceKind.OBSERVED_FACT.value:
                continue

            safe_payload = sanitize_no_oracle_leak(dict(ev.payload_json or {}))
            has_causal_key = False

            w_val = safe_payload.get("worker_id") or safe_payload.get("sync_worker_id")
            if isinstance(w_val, str) and w_val.strip():
                worker_id = w_val.strip()
                has_causal_key = True

            b_val = safe_payload.get("batch_id") or safe_payload.get("sync_batch_id")
            if isinstance(b_val, str) and b_val.strip():
                batch_id = b_val.strip()
                has_causal_key = True

            e_val = safe_payload.get("error_code") or safe_payload.get("sync_error_code")
            if isinstance(e_val, str) and e_val.strip():
                error_code = e_val.strip()
                has_causal_key = True

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

            ts_raw = safe_payload.get("event_timestamp")
            if isinstance(ts_raw, str) and ts_raw.strip():
                try:
                    parsed = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    event_ts = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
                    has_causal_key = True
                except ValueError:
                    pass

            if has_causal_key or ev.source in (
                "ORDER_EVENT_LOG_DB",
                "PROVIDER_LEDGER_MOCK",
                "SIM_ORDERS_DB",
            ):
                ev_refs.append(ev.id)

        return CausalObservation(
            case=case,
            claim_text=claim_text,
            evidence_refs=ev_refs,
            worker_id=worker_id,
            batch_id=batch_id,
            error_code=error_code,
            provider=provider,
            order_id=order_id,
            payment_id=payment_id,
            payment_status=payment_status,
            event_timestamp=event_ts,
        )

    def correlate_session_cases(self) -> CorrelationOutcome:
        """Group session Cases by shared causal OBSERVED_FACT metadata (`worker_id`, `batch_id`,
        `error_code`, and bounded event time window). Never group by complaint text alone."""
        cases = list(
            self._db.scalars(
                select(Case)
                .options(selectinload(Case.evidence))
                .where(Case.session_id == self._session_id)
                .order_by(Case.created_at.asc(), Case.id.asc())
            ).all()
        )

        observations = [self.extract_causal_observation(c) for c in cases]

        # Bucket candidate observations by (worker_id, batch_id, error_code) ONLY when all 3
        # causal OBSERVED_FACT attributes are present.
        causal_buckets: dict[tuple[str, str, str], list[CausalObservation]] = {}
        unbucketed: list[CausalObservation] = []

        for obs in observations:
            if obs.worker_id and obs.batch_id and obs.error_code:
                key = (obs.worker_id, obs.batch_id, obs.error_code)
                causal_buckets.setdefault(key, []).append(obs)
            else:
                unbucketed.append(obs)

        created_or_updated_incidents: list[Incident] = []
        correlated_case_ids: set[str] = set()
        primary_incident_key: tuple[str, str, str] | None = None

        for (worker_id, batch_id, error_code), bucket in causal_buckets.items():
            # Filter bucket members by bounded time window (<= 20 minutes between min and max ts)
            timestamps = [b.event_timestamp for b in bucket if b.event_timestamp is not None]
            if timestamps:
                min_ts = min(timestamps)
                window_members = [
                    b
                    for b in bucket
                    if b.event_timestamp is not None
                    and abs((b.event_timestamp - min_ts).total_seconds())
                    <= MAX_INCIDENT_TIME_WINDOW_SECONDS
                ]
            else:
                window_members = bucket

            if len(window_members) < MIN_CORRELATED_CASES_FOR_INCIDENT:
                unbucketed.extend(bucket)
                continue

            if primary_incident_key is None:
                primary_incident_key = (worker_id, batch_id, error_code)

            corr_key = f"worker:{worker_id}|batch:{batch_id}|err:{error_code}"
            member_ts = [m.event_timestamp for m in window_members if m.event_timestamp is not None]
            ts_start = min(member_ts).isoformat() if member_ts else "N/A"
            ts_end = max(member_ts).isoformat() if member_ts else "N/A"

            all_ev_refs: list[str] = []
            for m in window_members:
                for ref in m.evidence_refs:
                    if ref not in all_ev_refs:
                        all_ev_refs.append(ref)

            count = len(window_members)
            trace_msg = (
                f"Detected {count} potentially linked cases by dropped-sync worker ID "
                f"and time range (worker_id={worker_id}, batch_id={batch_id}, "
                f"error_code={error_code}, window={ts_start}..{ts_end})"
            )
            hypothesis_vi = (
                f"Giả thuyết sự cố (Hypothesis - cần kiểm chứng độc lập từng Case): "
                f"Nhóm {count} đơn hàng thanh toán MoMo thành công nhưng bị nghẽn đồng bộ "
                f"trạng thái đơn hàng do lỗi '{error_code}' tại worker '{worker_id}' "
                f"(lô '{batch_id}' trong khung giờ {ts_start} .. {ts_end}). "
                "Không tự động đóng/giải quyết các Case con khi chưa kiểm chứng hậu điều kiện."
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
            shared_factors = sanitize_no_oracle_leak(
                {
                    "worker_id": worker_id,
                    "batch_id": batch_id,
                    "error_code": error_code,
                    "provider": window_members[0].provider or "MOCK_MOMO",
                    "time_window_start": ts_start,
                    "time_window_end": ts_end,
                    "linked_cases_count": count,
                    "correlation_basis": "CAUSAL_OBSERVED_FACT_METADATA",
                    "pure_text_similarity_used": False,
                }
            )

            if existing_inc is None:
                incident = Incident(
                    id=f"INC-{uuid.uuid4().hex[:10].upper()}",
                    session_id=self._session_id,
                    title=(
                        f"Sự cố nghẽn đồng bộ đơn hàng ({worker_id} / {batch_id} / {count} Cases)"
                    ),
                    hypothesis=hypothesis_vi,
                    status=IncidentStatus.INVESTIGATING.value,
                    correlation_key=corr_key,
                    correlation_trace=trace_msg,
                    shared_factors_json=shared_factors,
                    evidence_refs=all_ev_refs,
                    confidence=0.92,
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
                incident.updated_at = now
                self._db.flush()

            existing_case_map = {ic.case_id: ic for ic in (incident.incident_cases or [])}
            for m in window_members:
                correlated_case_ids.add(m.case.id)
                if m.case.id not in existing_case_map:
                    ic = IncidentCase(
                        id=f"IC-{uuid.uuid4().hex[:10].upper()}",
                        incident_id=incident.id,
                        case_id=m.case.id,
                        session_id=self._session_id,
                        link_reason=(
                            f"Shared causal OBSERVED_FACT: worker_id={worker_id}, "
                            f"batch_id={batch_id}, error_code={error_code}"
                        ),
                        shared_evidence_refs=m.evidence_refs,
                        causal_factors_json=sanitize_no_oracle_leak(
                            {
                                "worker_id": worker_id,
                                "batch_id": batch_id,
                                "error_code": error_code,
                                "order_id": m.order_id,
                                "payment_id": m.payment_id,
                                "event_timestamp": (
                                    m.event_timestamp.isoformat() if m.event_timestamp else None
                                ),
                            }
                        ),
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
                                "shared_evidence_refs": m.evidence_refs,
                                "auto_resolved": False,
                            },
                            created_at=now,
                        )
                    )

            self._db.flush()
            created_or_updated_incidents.append(incident)

        # Identify lookalike Cases that share similar customer complaint text ("thanh toán",
        # "chờ thanh toán", "250.000") but were rejected because causal OBSERVED_FACT differs!
        rejected_lookalikes: list[RejectedLookalikeItem] = []
        for obs in unbucketed:
            if obs.case.id in correlated_case_ids:
                continue
            has_lookalike_complaint = any(
                kw in obs.claim_text.lower()
                for kw in ("chờ thanh toán", "đã thanh toán", "trừ tiền", "momo")
            )
            if not has_lookalike_complaint:
                continue

            if primary_incident_key is not None:
                p_worker, p_batch, p_err = primary_incident_key
                diff_parts: list[str] = []
                if obs.worker_id != p_worker:
                    diff_parts.append(f"worker_id='{obs.worker_id}' != '{p_worker}'")
                if obs.batch_id != p_batch:
                    diff_parts.append(f"batch_id='{obs.batch_id}' != '{p_batch}'")
                if obs.error_code != p_err:
                    diff_parts.append(f"error_code='{obs.error_code}' != '{p_err}'")
                if obs.payment_status and obs.payment_status != PaymentStatus.SUCCESS.value:
                    diff_parts.append(f"payment_status='{obs.payment_status}' != 'SUCCESS'")
                diff_summary = "; ".join(diff_parts) or "no shared causal worker/batch metadata"
            else:
                diff_summary = (
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
    - 12 correlated Cases sharing causal OBSERVED_FACT metadata:
      `worker_id="SYNC_WORKER_W07"`, `batch_id="BATCH_20261122_0900"`,
      `error_code="ERR_DROPPED_WEBHOOK_SYNC"`, event timestamps within 09:00..09:06 UTC.
    - 2 unrelated lookalike Cases with nearly identical Vietnamese customer complaint text
      ("Tôi đã thanh toán 250.000đ qua MoMo nhưng đơn hàng vẫn báo chờ thanh toán...")
      but completely different root causes in OBSERVED_FACT:
      1. Lookalike 1: Payment FAILED (OTP expired, `result_code=1006`,
         `worker_id="CHECKOUT_GATEWAY_W02"`, `error_code="ERR_USER_OTP_EXPIRED"`).
      2. Lookalike 2: Different worker/batch & signature error
         (`worker_id="PARTNER_CALLBACK_W19"`, `batch_id="BATCH_20261121_2215"`,
         `error_code="ERR_MERCHANT_SIGNATURE_MISMATCH"`).
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

    # Ensure session max_cases accommodates the 14 scenario cases in MOCK demo mode
    if demo_session.case_count + 14 > demo_session.max_cases:
        demo_session.max_cases = max(demo_session.max_cases, demo_session.case_count + 20)

    now = datetime.now(UTC)
    base_ts = datetime(2026, 11, 22, 9, 0, 0, tzinfo=UTC)
    suffix = uuid.uuid4().hex[:6].upper()

    shared_worker = "SYNC_WORKER_W07"
    shared_batch = "BATCH_20261122_0900"
    shared_error = "ERR_DROPPED_WEBHOOK_SYNC"

    correlated_case_ids: list[str] = []

    # 1. Create 12 correlated Cases + Orders + Verified SUCCESS Payments
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

    # 2. Create 2 unrelated lookalike Cases with almost identical customer complaint text
    #    but completely different causal OBSERVED_FACT metadata!
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

        # Deliberately identical Vietnamese complaint text phrasing as the 12 correlated cases!
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

    # 3. Run IncidentCorrelator and TaskforceManager in-house
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
