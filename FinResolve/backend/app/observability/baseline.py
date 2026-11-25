"""Dynamic Behavioral Baseline Engine (Prompt 14 Section 11).

Maintains persistent merchant/service/metric baselines with:
- Explicit warm-up (`sample_count >= min_samples`).
- EWMA + Rolling Median + Median Absolute Deviation (MAD) robust deviation scoring.
- Outlier-resistant updates (extreme anomalies do NOT contaminate the baseline!).
- Explicit freeze/quarantine (`is_frozen=True`) during major incidents.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.open_world import BehavioralBaseline

MAX_RECENT_WINDOW = 50
EWMA_ALPHA = 0.25


@dataclass(frozen=True)
class BaselineEvaluation:
    metric_name: str
    value: float
    warm_up_complete: bool
    is_frozen: bool
    baseline_median: float
    baseline_mad: float
    baseline_ewma: float
    robust_z_score: float
    is_anomalous: bool
    confidence: float


def _compute_median_mad(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    med = float(statistics.median(values))
    deviations = [abs(v - med) for v in values]
    mad = float(statistics.median(deviations))
    return round(med, 4), round(mad, 4)


def update_and_evaluate_baseline(
    db: Session,
    *,
    session_id: str,
    merchant_id: str,
    service_name: str,
    metric_name: str,
    value: float,
    regime: str = "NORMAL",
    min_samples: int = 5,
    z_threshold: float = 3.5,
    min_abs_delta: float = 1.0,
) -> BaselineEvaluation:
    """Evaluate a metric against its persistent baseline and update if non-contaminating."""
    now = datetime.now(UTC)
    row = db.scalar(
        select(BehavioralBaseline)
        .where(
            BehavioralBaseline.session_id == session_id,
            BehavioralBaseline.merchant_id == merchant_id,
            BehavioralBaseline.service_name == service_name,
            BehavioralBaseline.metric_name == metric_name,
            BehavioralBaseline.regime == regime,
        )
        .with_for_update()
    )
    if row is None:
        row = BehavioralBaseline(
            id=f"BASE-{uuid.uuid4().hex[:10].upper()}",
            session_id=session_id,
            merchant_id=merchant_id,
            service_name=service_name,
            metric_name=metric_name,
            regime=regime,
            sample_count=0,
            min_samples=min_samples,
            ewma=float(value),
            median=float(value),
            mad=0.0,
            recent_values_json=[],
            confidence=0.0,
            is_frozen=False,
            last_updated_at=now,
        )
        db.add(row)
        db.flush()

    warm_up_complete = row.sample_count >= row.min_samples
    abs_delta = abs(float(value) - row.median)

    # Robust z-score using MAD (scaled by 1.4826 for normal consistency, with floor)
    scale = max(row.mad * 1.4826, max(abs(row.median) * 0.1, 0.5))
    robust_z = round(abs_delta / scale, 3) if warm_up_complete else 0.0
    is_anomalous = warm_up_complete and robust_z >= z_threshold and abs_delta >= min_abs_delta

    # Contamination protection: NEVER update baseline when frozen or value is an outlier!
    if not row.is_frozen and not is_anomalous:
        recent = list(row.recent_values_json or [])
        recent.append(round(float(value), 4))
        if len(recent) > MAX_RECENT_WINDOW:
            recent = recent[-MAX_RECENT_WINDOW:]
        med, mad = _compute_median_mad(recent)
        row.recent_values_json = recent
        row.sample_count += 1
        row.ewma = (
            float(value)
            if row.sample_count == 1
            else round(EWMA_ALPHA * float(value) + (1.0 - EWMA_ALPHA) * row.ewma, 4)
        )
        row.median = med
        row.mad = mad
        row.confidence = round(min(1.0, row.sample_count / max(1.0, float(row.min_samples * 2))), 3)
        row.last_updated_at = now

    db.flush()
    return BaselineEvaluation(
        metric_name=metric_name,
        value=round(float(value), 4),
        warm_up_complete=warm_up_complete,
        is_frozen=row.is_frozen,
        baseline_median=row.median,
        baseline_mad=row.mad,
        baseline_ewma=row.ewma,
        robust_z_score=robust_z,
        is_anomalous=is_anomalous,
        confidence=row.confidence,
    )


def set_merchant_baselines_frozen(
    db: Session,
    *,
    session_id: str,
    merchant_id: str,
    frozen: bool,
) -> int:
    """Freeze or unfreeze baseline learning during major active incidents."""
    rows = list(
        db.scalars(
            select(BehavioralBaseline).where(
                BehavioralBaseline.session_id == session_id,
                BehavioralBaseline.merchant_id == merchant_id,
            )
        ).all()
    )
    for r in rows:
        r.is_frozen = frozen
    db.flush()
    return len(rows)
