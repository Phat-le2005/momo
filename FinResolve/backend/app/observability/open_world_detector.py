"""Open-World Hybrid 4-Layer Anomaly Detection & Unknown Incident Handler (Prompt 14 Part C).

Layers:
- Layer 1: Deterministic invariants (D01..D08 in `app.monitoring.detectors`).
- Layer 2: Adaptive statistical deviation against persistent `BehavioralBaseline`
  (latency spike, error-rate surge, traffic spike, backlog surge, refund frequency).
- Layer 3: Lightweight multivariate novelty detection (cross-signal divergence, e.g.,
  traffic surging while throughput/payment confirmation degrades, or refund activity
  spiking while order volume is flat).
- Layer 4: Contextual AI trigger for ambiguous/unexplained novel patterns.

Mandatory Open-World Capability:
- Emits generic `UNKNOWN_OPERATIONAL_ANOMALY` or `UNKNOWN_BUSINESS_ANOMALY` candidates
  WITHOUT requiring a predefined detector code or challenge family, and WITHOUT inventing
  a root cause at detection time!
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.monitoring.detectors import DetectedAnomaly
from app.observability.baseline import BaselineEvaluation, update_and_evaluate_baseline


@dataclass(frozen=True)
class TelemetrySignalVector:
    """Structured operational & business signal snapshot for Layer 2 & 3 detection."""

    rps: float = 0.0
    p95_latency_ms: float = 0.0
    error_rate: float = 0.0
    outbox_lag: float = 0.0
    unsynced_payments: float = 0.0
    order_throughput: float = 0.0
    refund_count: float = 0.0


def run_open_world_hybrid_detection(
    db: Session,
    *,
    session_id: str,
    merchant_id: str,
    service_name: str = "ministore-commerce",
    signals: TelemetrySignalVector,
    regime: str = "NORMAL",
    min_samples: int = 5,
) -> tuple[list[DetectedAnomaly], dict[str, BaselineEvaluation]]:
    """Run Layer 2 (adaptive statistical) + Layer 3 (multivariate novelty) open-world detection.

    Returns `(detected_unknown_anomalies, baseline_evaluations)`.
    """
    metric_specs: list[tuple[str, float, float, float]] = [
        ("rps", signals.rps, 3.5, 0.5),
        ("p95_latency_ms", signals.p95_latency_ms, 3.5, 25.0),
        ("error_rate", signals.error_rate, 3.0, 0.05),
        ("outbox_lag", signals.outbox_lag, 3.5, 3.0),
        ("unsynced_payments", signals.unsynced_payments, 3.5, 2.0),
        ("order_throughput", signals.order_throughput, 3.5, 2.0),
        ("refund_count", signals.refund_count, 3.5, 2.0),
    ]

    evals: dict[str, BaselineEvaluation] = {}
    for name, val, z_thr, min_delta in metric_specs:
        evals[name] = update_and_evaluate_baseline(
            db,
            session_id=session_id,
            merchant_id=merchant_id,
            service_name=service_name,
            metric_name=name,
            value=val,
            regime=regime,
            min_samples=min_samples,
            z_threshold=z_thr,
            min_abs_delta=min_delta,
        )

    anomalies: list[DetectedAnomaly] = []

    # Layer 3 — Multivariate Novelty: Cross-signal divergence (e.g., traffic up + latency/error up,
    # or refund activity surging while order throughput stays flat/low)
    rps_ev = evals["rps"]
    lat_ev = evals["p95_latency_ms"]
    err_ev = evals["error_rate"]
    ref_ev = evals["refund_count"]
    ord_ev = evals["order_throughput"]

    multivariate_triggers: list[str] = []
    if rps_ev.warm_up_complete and lat_ev.warm_up_complete:
        if rps_ev.value > rps_ev.baseline_median * 1.5 and (
            lat_ev.is_anomalous or err_ev.is_anomalous
        ):
            multivariate_triggers.append(
                f"traffic_vs_degradation(rps={rps_ev.value} vs {rps_ev.baseline_median}, "
                f"p95={lat_ev.value}ms, err_rate={err_ev.value})"
            )
    if ref_ev.warm_up_complete and ord_ev.warm_up_complete:
        if ref_ev.is_anomalous and not ord_ev.is_anomalous:
            multivariate_triggers.append(
                f"refund_spike_flat_orders(refunds={ref_ev.value}/{ref_ev.baseline_median}, "
                f"orders={ord_ev.value}/{ord_ev.baseline_median})"
            )

    if multivariate_triggers:
        anomalies.append(
            DetectedAnomaly(
                detector_code="OPEN_MV",
                anomaly_type="UNKNOWN_BUSINESS_ANOMALY",
                fingerprint=f"OPEN_MV:{merchant_id}:{service_name}:MULTIVARIATE",
                severity="HIGH",
                affected_order_id=None,
                affected_payment_id=None,
                affected_entities={
                    "merchant_id": merchant_id,
                    "service_name": service_name,
                    "detection_layer": "LAYER_3_MULTIVARIATE_NOVELTY",
                    "triggers": multivariate_triggers,
                },
                evidence_refs=[
                    {
                        "source": "OPEN_WORLD_HYBRID_DETECTOR",
                        "detection_layer": "LAYER_3_MULTIVARIATE_NOVELTY",
                        "triggers": multivariate_triggers,
                        "baseline_comparison": {
                            k: {
                                "observed": v.value,
                                "baseline_median": v.baseline_median,
                                "robust_z": v.robust_z_score,
                            }
                            for k, v in evals.items()
                        },
                    }
                ],
                rationale=(
                    "Novel multivariate divergence (root cause unknown): "
                    + "; ".join(multivariate_triggers)
                    + "."
                ),
                case_type="UNKNOWN_BUSINESS_ANOMALY",
                recommended_action_hint="INVESTIGATE_UNKNOWN_ANOMALY",
            )
        )

    # Layer 2 — Adaptive Statistical Deviations (when no multivariate already covers it,
    # or for standalone latency/error/backlog spikes)
    stat_anomalous = [
        ev
        for name, ev in evals.items()
        if ev.is_anomalous and name in ("p95_latency_ms", "error_rate", "outbox_lag")
    ]
    if stat_anomalous and not multivariate_triggers:
        changed = ", ".join(
            f"{e.metric_name}={e.value} (baseline={e.baseline_median}, z={e.robust_z_score})"
            for e in stat_anomalous
        )
        anomalies.append(
            DetectedAnomaly(
                detector_code="OPEN_STAT",
                anomaly_type="UNKNOWN_OPERATIONAL_ANOMALY",
                fingerprint=f"OPEN_STAT:{merchant_id}:{service_name}:{stat_anomalous[0].metric_name}",
                severity="HIGH"
                if any(e.metric_name == "error_rate" for e in stat_anomalous)
                else "MEDIUM",
                affected_order_id=None,
                affected_payment_id=None,
                affected_entities={
                    "merchant_id": merchant_id,
                    "service_name": service_name,
                    "detection_layer": "LAYER_2_ADAPTIVE_STATISTICAL",
                    "deviating_metrics": [e.metric_name for e in stat_anomalous],
                },
                evidence_refs=[
                    {
                        "source": "OPEN_WORLD_HYBRID_DETECTOR",
                        "detection_layer": "LAYER_2_ADAPTIVE_STATISTICAL",
                        "metrics": [
                            {
                                "metric": e.metric_name,
                                "observed": e.value,
                                "baseline_median": e.baseline_median,
                                "baseline_mad": e.baseline_mad,
                                "robust_z_score": e.robust_z_score,
                            }
                            for e in stat_anomalous
                        ],
                    }
                ],
                rationale=(
                    f"Statistical deviation from learned behavioral baseline (root cause unknown): "
                    f"{changed}."
                ),
                case_type="UNKNOWN_OPERATIONAL_ANOMALY",
                recommended_action_hint="INVESTIGATE_UNKNOWN_ANOMALY",
            )
        )

    return anomalies, evals
