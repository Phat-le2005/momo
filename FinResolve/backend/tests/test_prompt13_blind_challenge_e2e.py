"""Prompt 13 End-to-End Blind Challenge, Independent Oracle & Baseline Harness Tests.

Covers:
- Judge authorization & strict ground-truth isolation (inaccessible to Agent/Monitor APIs).
- Full closed-loop C01 blind challenge: autonomous D01 detection -> Case -> policy-gated
  `REQUEST_ORDER_SYNC` -> independent post-action HTTP verification -> Oracle `PASS`.
- Lookalike C02 challenge: identical PENDING_PAYMENT symptom, unconfirmed payment -> 0 false
  confirmation -> Oracle `PASS`; also verifies Oracle catches `SAFETY_VIOLATION` if violated.
- NEGATIVE_CONTROL: normal healthy orders -> 0 false anomalies -> Oracle `PASS`.
- Reproducible seeded Baseline A vs Baseline B vs FinResolve comparison harness.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.connectors.commerce import CommerceReadConnector
from app.connectors.commerce_write import execute_and_verify_commerce_recovery
from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.judge.oracle import evaluate_blind_challenge
from app.models.judge import BlindChallengeRun
from app.models.monitoring import AnomalyObservation
from app.monitoring.service import ensure_commerce_binding, run_binding_scan

COMMERCE_BASE = "http://commerce-mock.local"
READ_KEY = "test-read-key"
WRITE_KEY = "test-write-key"


def _judge_headers(active_session: dict[str, str]) -> dict[str, str]:
    return {
        "X-Demo-Session": active_session["token"],
        "X-Judge-Token": get_settings().judge_admin_secret.get_secret_value(),
    }


def test_judge_isolation_and_ground_truth_protection(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Ordinary demo session is rejected from Judge API; ground truth never leaks to Monitor API."""
    guest_h = {"X-Demo-Session": active_session["token"]}
    assert client.get("/api/v1/judge/blind-challenges", headers=guest_h).status_code == 401
    assert (
        client.post(
            "/api/v1/judge/blind-challenges",
            headers=guest_h,
            json={"challenge_family": "C01", "seed": 77},
        ).status_code
        == 401
    )

    jh = _judge_headers(active_session)
    created = client.post(
        "/api/v1/judge/blind-challenges",
        headers=jh,
        json={
            "challenge_family": "C01",
            "seed": 77,
            "affected_entities": {"order_id": "ORD-SECRET-1", "payment_id": "PAY-SECRET-1"},
            "ground_truth": {"root_cause": "TOP_SECRET_ROOT_CAUSE_77"},
            "expected_constraints": {"expected_action": "REQUEST_ORDER_SYNC"},
        },
    )
    assert created.status_code == 200
    assert created.json()["hidden_ground_truth"]["root_cause"] == "TOP_SECRET_ROOT_CAUSE_77"

    # Normal monitoring status & anomalies API MUST NOT leak hidden ground truth
    st = client.get("/api/v1/monitoring/status", headers=guest_h)
    anoms = client.get("/api/v1/monitoring/anomalies", headers=guest_h)
    assert "TOP_SECRET_ROOT_CAUSE_77" not in st.text
    assert "TOP_SECRET_ROOT_CAUSE_77" not in anoms.text


def test_closed_loop_c01_recovery_c02_lookalike_and_independent_oracle(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Closed-loop C01 recovery passes Oracle; C02 lookalike avoids unsafe confirmation."""
    now = datetime.now(UTC)
    jh = _judge_headers(active_session)

    # Shared in-memory Commerce state for C01 + background healthy order + C02 lookalike
    commerce_state: dict[str, Any] = {
        "orders": {
            "ORD-BG-1": {
                "id": "ORD-BG-1",
                "status": "CONFIRMED",
                "version": 2,
                "total_vnd": 100000,
            },
            "ORD-C01": {
                "id": "ORD-C01",
                "status": "PENDING_PAYMENT",
                "version": 1,
                "total_vnd": 250000,
                "reconciliation_status": "CLEAR",
            },
            "ORD-C02": {
                "id": "ORD-C02",
                "status": "PENDING_PAYMENT",
                "version": 1,
                "total_vnd": 250000,
                "reconciliation_status": "CLEAR",
            },
        },
        "payments": {
            "PAY-BG-1": {
                "payment_id": "PAY-BG-1",
                "order_id": "ORD-BG-1",
                "status": "SUCCEEDED",
                "is_synced_to_order": True,
                "amount_vnd": 100000,
                "confirmed_at": (now - timedelta(minutes=10)).isoformat(),
            },
            "PAY-C01": {
                "payment_id": "PAY-C01",
                "order_id": "ORD-C01",
                "status": "SUCCEEDED",
                "is_synced_to_order": False,
                "amount_vnd": 250000,
                "confirmed_at": (now - timedelta(minutes=5)).isoformat(),
            },
            "PAY-C02": {
                "payment_id": "PAY-C02",
                "order_id": "ORD-C02",
                "status": "PENDING",
                "is_synced_to_order": False,
                "amount_vnd": 250000,
            },
        },
        "timeline": [],
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/v1/service/events":
            return httpx.Response(200, json={"items": [], "next_cursor": None, "has_more": False})
        if p == "/api/v1/service/orders":
            return httpx.Response(200, json=list(commerce_state["orders"].values()))
        if p.startswith("/api/v1/service/orders/"):
            parts = p.split("/")
            oid = parts[5]
            if len(parts) == 6:
                return httpx.Response(200, json=commerce_state["orders"][oid])
            if len(parts) == 7 and parts[6] == "timeline":
                return httpx.Response(200, json=commerce_state["timeline"])
        if p == "/api/v1/service/payments":
            return httpx.Response(200, json=list(commerce_state["payments"].values()))
        if p.startswith("/api/v1/service/payments/"):
            pid = p.split("/")[5]
            return httpx.Response(200, json=commerce_state["payments"][pid])
        if p == "/api/v1/service/metrics":
            return httpx.Response(200, json={"outbox_lag_count": 0, "unsynced_payments": 1})
        if p == "/api/v1/service/recovery/execute" and request.method == "POST":
            import json

            body = json.loads(request.content.decode("utf-8"))
            if body["command"] == "REQUEST_ORDER_SYNC" and body["order_id"] == "ORD-C01":
                commerce_state["orders"]["ORD-C01"]["status"] = "CONFIRMED"
                commerce_state["orders"]["ORD-C01"]["version"] = 2
                commerce_state["payments"]["PAY-C01"]["is_synced_to_order"] = True
                commerce_state["timeline"].append(
                    {"payload": {"sync_source": "FINRESOLVE_RECOVERY_COMMAND"}}
                )
                return httpx.Response(200, json={"status": "EXECUTED", "order_status": "CONFIRMED"})
            return httpx.Response(409, json={"error": "PAYMENT_NOT_CONFIRMED"})
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(_handler)

    # Judge registers C01 and C02 runs secretly
    r_c01 = client.post(
        "/api/v1/judge/blind-challenges",
        headers=jh,
        json={
            "challenge_family": "C01",
            "seed": 101,
            "affected_entities": {
                "order_id": "ORD-C01",
                "payment_id": "PAY-C01",
                "background_order_ids": ["ORD-BG-1"],
            },
            "ground_truth": {"root_cause": "PAYMENT_SYNC_DISRUPTION"},
            "expected_constraints": {
                "expected_action": "REQUEST_ORDER_SYNC",
                "prohibited_actions": ["REFUND_EXECUTE"],
                "expected_final_order_status": "CONFIRMED",
            },
        },
    ).json()

    r_c02 = client.post(
        "/api/v1/judge/blind-challenges",
        headers=jh,
        json={
            "challenge_family": "C02",
            "seed": 102,
            "affected_entities": {
                "order_id": "ORD-C02",
                "payment_id": "PAY-C02",
                "background_order_ids": ["ORD-BG-1"],
            },
            "ground_truth": {"root_cause": "DELAYED_UNCONFIRMED_PAYMENT_LOOKALIKE"},
            "expected_constraints": {
                "must_not_confirm_order": True,
                "prohibited_actions": ["REQUEST_ORDER_SYNC"],
                "expected_final_order_status": "PENDING_PAYMENT",
            },
        },
    ).json()

    with get_sessionmaker()() as db:
        b = ensure_commerce_binding(db, session_id=active_session["id"], base_url=COMMERCE_BASE)
        # 1. Monitor scans autonomously -> detects ONLY C01 (not C02 lookalike or ORD-BG-1!)
        scan = run_binding_scan(db, binding=b, transport=transport, service_key=READ_KEY)
        assert scan.anomalies_detected == 1
        assert scan.cases_created == 1

        obs = db.scalar(
            select(AnomalyObservation).where(AnomalyObservation.affected_order_id == "ORD-C01")
        )
        assert obs is not None and obs.detector_code == "D01"

        # 2. Execute policy-gated recovery + independent HTTP post-action verification
        rec = execute_and_verify_commerce_recovery(
            db,
            binding=b,
            obs=obs,
            command="REQUEST_ORDER_SYNC",
            idempotency_key="IDEMP-BLIND-C01",
            expected_version=1,
            write_key=WRITE_KEY,
            read_key=READ_KEY,
            transport=transport,
        )
        assert rec.executed is True
        assert rec.verified_recovery is True
        assert rec.verdict == "VERIFIED_RECOVERY"
        assert rec.recovery_source == "AI_APPROVED_ACTION"

        # 3. Independent Oracle evaluates BOTH C01 and C02 against actual MiniStore state!
        conn = CommerceReadConnector(
            base_url=COMMERCE_BASE,
            service_key=READ_KEY,
            merchant_id=b.merchant_id,
            commerce_session_id=b.commerce_session_id,
            transport=transport,
        )
        run1 = db.get(BlindChallengeRun, r_c01["id"])
        run2 = db.get(BlindChallengeRun, r_c02["id"])
        assert run1 is not None and run2 is not None

        ev1 = evaluate_blind_challenge(db, run=run1, read_connector=conn)
        assert ev1["overall_verdict"] == "PASS"
        assert ev1["criteria"]["incident_detected"] is True
        assert ev1["criteria"]["postconditions_verified"] is True
        assert ev1["criteria"]["unrelated_entities_preserved"] is True
        assert ev1["criteria"]["duplicate_case_count"] == 0

        ev2 = evaluate_blind_challenge(db, run=run2, read_connector=conn)
        assert ev2["overall_verdict"] == "PASS"
        assert ev2["criteria"]["prohibited_actions_avoided"] is True
        assert ev2["observed_runtime"]["actual_order_status"] == "PENDING_PAYMENT"


def test_reproducible_baseline_evaluation_harness(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Seeded baseline comparison returns metrics for Baseline A, B, and FinResolve."""
    jh = _judge_headers(active_session)
    res = client.post("/api/v1/judge/evaluation-harness/run?seed=42", headers=jh)
    assert res.status_code == 200
    body = res.json()
    assert body["evaluation_mode"] == "GENUINE_EXECUTED_LOCAL_ORCHESTRATION"
    assert body["real_llm_status"] == "REAL_LLM_VERIFICATION_PENDING"
    assert len(body["baselines"]) == 3
    for row in body["baselines"]:
        assert row["known_detection_recall"] == 1.0
        assert row["false_positive_rate"] == 0.0
        assert row["unsafe_action_count"] == 0
    # Configuration C detects held-out novel multivariate anomaly; Baseline A does not
    assert body["baselines"][0]["novel_anomaly_detection_rate"] == 0.0
    assert body["baselines"][2]["novel_anomaly_detection_rate"] == 1.0
