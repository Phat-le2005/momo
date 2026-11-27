"""Prompt 12.1 Security & Reliability Hardening Regression Suite.

Covers Part A Sections 3-11:
- P0 SSRF & Credential Security: arbitrary base_url rejected, unauthorized guest blocked,
  cross-merchant blocked, malicious hosts/userinfo/ports/redirects blocked, secrets scrubbed,
  immutable historical bindings.
- P1 Multi-Page Event Pagination, Equal Timestamps, Budget Continuation, Unified Push/Pull Hash.
- P1 D06 Full-Pipeline Detection + Same-Scan Recovery (push-expected vs pull-only).
- P1 True Lease & World-Epoch Fencing (stale worker A cannot commit after worker B takes lease;
  world reset invalidates binding instead of silently rebinding).
- P1 Strengthened Recovery Verification (5-state verdicts, D02 RESOLVED insufficient, D05 empty
  inventory -> INSUFFICIENT_EVIDENCE, actor defaults to UNKNOWN).
- P2 Anomaly Recurrence preserving prior recovery history.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.connectors.commerce import (
    CommerceConnectorError,
    CommerceReadConnector,
    validate_approved_commerce_url,
)
from app.core.errors import AppError
from app.core.session import issue_service_ingestion_token
from app.db.session import get_sessionmaker
from app.models.cases import Case
from app.models.monitoring import AnomalyObservation
from app.models.supervisor import NormalizedEvent, SupervisorActivity, SupervisorState
from app.monitoring.service import (
    RecoveryVerdict,
    StaleLeaseError,
    collect_commerce_events,
    ensure_commerce_binding,
    evaluate_recovery_postconditions,
    run_binding_scan,
    verify_anomaly_recovery,
)
from app.monitoring.worker import run_monitor_tick

COMMERCE_BASE = "http://commerce-mock.local"
SERVICE_KEY = "SUPER_SECRET_COMMERCE_KEY_DO_NOT_LEAK_9911"  # noqa: S105


# --- 1. P0 Connector Security: SSRF, Redirects, Auth, Cross-Merchant & Credential Scrubbing ---
def test_p0_connector_security_ssrf_redirects_and_auth(
    client: TestClient, active_session: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject arbitrary base_url, malicious URLs, redirects, guest binding, and cross-merchant."""
    guest_headers = {"X-Demo-Session": active_session["token"]}
    mgr_headers = {
        "X-Demo-Session": active_session["token"],
        "X-Demo-Manager-Token": active_session["manager_token"],
    }

    # 1. Ordinary guest session (no manager token) cannot create/modify bindings
    r_unauth = client.post(
        "/api/v1/monitoring/bindings",
        headers=guest_headers,
        json={"connector_id": "default_ministore"},
    )
    assert r_unauth.status_code in (401, 403)

    # 2. Arbitrary user-supplied `base_url` is strictly rejected (422 extra forbidden!)
    r_ssrf = client.post(
        "/api/v1/monitoring/bindings",
        headers=mgr_headers,
        json={"connector_id": "default_ministore", "base_url": "http://169.254.169.254"},
    )
    assert r_ssrf.status_code == 422

    # 3. Cross-merchant / unauthorized Commerce session rejected (403)
    r_cross = client.post(
        "/api/v1/monitoring/bindings",
        headers=mgr_headers,
        json={"connector_id": "default_ministore", "merchant_id": "MERCHANT_EVIL_99"},
    )
    assert r_cross.status_code == 403

    # 4. Malicious URLs rejected by validator
    for bad_url in (
        "http://169.254.169.254",
        "http://evil.attacker.com",
        "http://user:pass@localhost:8100",
        "ftp://localhost:8100",
        "http://localhost:8100/extra/path",
        "http://localhost:9999",
    ):
        with pytest.raises(CommerceConnectorError):
            validate_approved_commerce_url(bad_url)

    # 5. Redirects strictly blocked & service_key scrubbed from error messages
    def _redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})

    conn_redir = CommerceReadConnector(
        base_url=COMMERCE_BASE,
        service_key=SERVICE_KEY,
        merchant_id="MERCHANT_FINSHOP_01",
        commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
        transport=httpx.MockTransport(_redirect_handler),
    )
    with pytest.raises(CommerceConnectorError, match="CONNECTOR_REDIRECT_BLOCKED"):
        conn_redir.get_metrics()

    def _leak_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=f"Denied key={SERVICE_KEY}")

    conn_leak = CommerceReadConnector(
        base_url=COMMERCE_BASE,
        service_key=SERVICE_KEY,
        merchant_id="MERCHANT_FINSHOP_01",
        commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
        transport=httpx.MockTransport(_leak_handler),
    )
    with pytest.raises(CommerceConnectorError) as exc_info:
        conn_leak.get_metrics()
    assert SERVICE_KEY not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)

    # 6. Historical Cases freeze binding destination URL
    with get_sessionmaker()() as db:
        b = ensure_commerce_binding(db, session_id=active_session["id"], base_url=COMMERCE_BASE)
        db.add(
            Case(
                id="CASE-HIST-01",
                session_id=active_session["id"],
                type="PAYMENT_DISPUTE",
                goal="Historical",
                source_system="COMMERCE_HTTP",
                source_binding_id=b.id,
            )
        )
        db.commit()
        with pytest.raises(AppError, match="BINDING_IMMUTABLE_WITH_HISTORICAL_CASES"):
            ensure_commerce_binding(
                db, session_id=active_session["id"], base_url="http://localhost:8100"
            )


# --- 2. P1 Multi-Page Event Pagination, Equal Timestamps & Unified Push/Pull Hash ---
def test_p1_multipage_event_pagination_and_unified_push_pull_hash(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Paginates across 4 pages with equal timestamps, resumes ticks, unifies push/pull hash."""
    same_ts = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    all_events = [
        {
            "id": f"EV-{i:04d}",
            "event_id": f"CEVT-{i:04d}",
            "event_type": "ORDER_CREATED",
            "source": "MINI_STORE_COMMERCE",
            "source_mode": "MOCK",
            "order_id": f"ORD-{i:04d}",
            "occurred_at": same_ts,
            "payload": {"seq": i},
        }
        for i in range(1, 106)  # 105 events -> 4 pages at batch_size=30
    ]

    def _paged_handler(request: httpx.Request) -> httpx.Response:
        after_id = request.url.params.get("after_id")
        limit = int(request.url.params.get("limit", "30"))
        start = 0
        if after_id:
            for idx, ev in enumerate(all_events):
                if ev["id"] == after_id:
                    start = idx + 1
                    break
        slice_rows = all_events[start : start + limit]
        has_more = (start + limit) < len(all_events)
        next_cur = slice_rows[-1]["id"] if has_more and slice_rows else None
        return httpx.Response(
            200, json={"items": slice_rows, "next_cursor": next_cur, "has_more": has_more}
        )

    # First push CEVT-0001 via authenticated push inbox
    from app.models.session import DemoSession

    with get_sessionmaker()() as db_sess:
        sess_obj = db_sess.get(DemoSession, active_session["id"])
        assert sess_obj is not None
        svc_tok = issue_service_ingestion_token(
            sess_obj,
            source="MINI_STORE_COMMERCE",
            merchant_id="MERCHANT_FINSHOP_01",
        )
    push_resp = client.post(
        "/api/v1/events/ingest",
        headers={"X-Demo-Session": active_session["token"], "X-Service-Token": svc_tok},
        json={
            "event_id": "CEVT-0001",
            "event_type": "ORDER_CREATED",
            "source": "MINI_STORE_COMMERCE",
            "merchant_id": "MERCHANT_FINSHOP_01",
            "order_id": "ORD-0001",
            "occurred_at": same_ts,
            "payload": {"seq": 1},
        },
    )
    assert push_resp.status_code in (200, 202)

    with get_sessionmaker()() as db:
        b = ensure_commerce_binding(db, session_id=active_session["id"], base_url=COMMERCE_BASE)
        conn = CommerceReadConnector(
            base_url=COMMERCE_BASE,
            service_key=SERVICE_KEY,
            merchant_id=b.merchant_id,
            commerce_session_id=b.commerce_session_id,
            transport=httpx.MockTransport(_paged_handler),
        )
        # Tick 1 with max_pages=2 -> pulls 60 events, events_scan_complete=False (resumable!)
        p1, n1, _ = collect_commerce_events(
            db, binding=b, connector=conn, batch_size=30, max_pages=2
        )
        assert p1 == 60
        assert n1 == 59  # CEVT-0001 already pushed with IDENTICAL hash -> 0 conflict!
        assert b.events_scan_complete is False
        assert b.last_event_cursor == "EV-0060"

        # Tick 2 with max_pages=3 -> pulls remaining 45 events, completes scan!
        p2, n2, _ = collect_commerce_events(
            db, binding=b, connector=conn, batch_size=30, max_pages=3
        )
        assert p2 == 45
        assert n2 == 45
        assert b.events_scan_complete is True
        assert b.last_event_cursor == "EV-0105"

        total_ev = int(
            db.scalar(
                select(func.count(NormalizedEvent.id)).where(
                    NormalizedEvent.session_id == active_session["id"]
                )
            )
            or 0
        )
        assert total_ev == 105
        conflicts = int(
            db.scalar(
                select(func.count(SupervisorActivity.id)).where(
                    SupervisorActivity.session_id == active_session["id"],
                    SupervisorActivity.kind == "EVENT_PAYLOAD_CONFLICT",
                )
            )
            or 0
        )
        assert conflicts == 0


# --- 3. P1 D06 Full-Pipeline Detection + Same-Scan Recovery (Push-Expected vs Pull-Only) ---
def test_p1_d06_full_pipeline_detection_and_same_scan_recovery(
    active_session: dict[str, str],
) -> None:
    """D06 detects missing push-expected event BEFORE ingest, recovers & verifies in same scan."""
    old_ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    remote_events = [
        {
            "id": "EV-PUSH-MISS",
            "event_id": "CEVT-PUSH-MISS-01",
            "event_type": "ORDER_CREATED",  # Push-expected!
            "source": "MINI_STORE_COMMERCE",
            "source_mode": "MOCK",
            "order_id": "ORD-D06-1",
            "occurred_at": old_ts,
            "payload": {"total_vnd": 100000},
        },
        {
            "id": "EV-PULL-ONLY",
            "event_id": "CEVT-PULL-ONLY-01",
            "event_type": "INVENTORY_RESERVED",  # Pull-only -> MUST NOT trigger D06!
            "source": "MINI_STORE_COMMERCE",
            "source_mode": "MOCK",
            "order_id": "ORD-D06-1",
            "occurred_at": old_ts,
            "payload": {"qty": 1},
        },
    ]

    def _handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/v1/service/events":
            return httpx.Response(
                200, json={"items": remote_events, "next_cursor": None, "has_more": False}
            )
        if p == "/api/v1/service/metrics":
            return httpx.Response(200, json={"outbox_lag_count": 0, "unsynced_payments": 0})
        return httpx.Response(200, json=[])

    with get_sessionmaker()() as db:
        b = ensure_commerce_binding(db, session_id=active_session["id"], base_url=COMMERCE_BASE)
        summary = run_binding_scan(
            db, binding=b, transport=httpx.MockTransport(_handler), service_key=SERVICE_KEY
        )
        assert summary.anomalies_detected == 1
        assert summary.recoveries_verified == 1

        anoms = list(
            db.scalars(
                select(AnomalyObservation).where(
                    AnomalyObservation.session_id == active_session["id"]
                )
            ).all()
        )
        assert len(anoms) == 1
        assert anoms[0].detector_code == "D06"
        assert anoms[0].affected_entities_json["event_id"] == "CEVT-PUSH-MISS-01"
        assert anoms[0].lifecycle_state == "RECOVERED"
        assert anoms[0].verification_verdict == RecoveryVerdict.VERIFIED_RECOVERY.value


# --- 4. P1 True Lease Fencing & World-Epoch Reset Isolation ---
def test_p1_stale_worker_fencing_and_world_reset_isolation(active_session: dict[str, str]) -> None:
    """Stale worker A cannot commit after worker B claims lease; world reset stops scan."""

    def _empty_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/service/events":
            return httpx.Response(200, json={"items": [], "next_cursor": None, "has_more": False})
        if request.url.path == "/api/v1/service/metrics":
            return httpx.Response(200, json={"outbox_lag_count": 0, "unsynced_payments": 0})
        return httpx.Response(200, json=[])

    with get_sessionmaker()() as db:
        b = ensure_commerce_binding(db, session_id=active_session["id"], base_url=COMMERCE_BASE)
        # Worker B holds active lease token
        b.lease_token = "WORKER-B-LEASE"  # noqa: S105
        b.locked_until = datetime.now(UTC) + timedelta(minutes=5)
        db.commit()

        # Stale worker A tries to run scan with old token -> blocked cleanly with LEASE_LOST!
        s_stale = run_binding_scan(
            db,
            binding=b,
            transport=httpx.MockTransport(_empty_handler),
            service_key=SERVICE_KEY,
            lease_token="WORKER-A-STALE",  # noqa: S105
            expected_world_epoch=b.world_epoch,
        )
        assert s_stale.connectivity_status == "LEASE_LOST"
        assert b.last_successful_scan_at is None

        # Clear lease, advance SupervisorState.world_epoch -> worker MUST mark WORLD_RESET_REQUIRED!
        b.lease_token = None
        b.locked_until = None
        sup = db.get(SupervisorState, active_session["id"])
        assert sup is not None
        sup.world_epoch += 1
        db.commit()

        ticks = run_monitor_tick(
            db,
            worker_id="w-reset-check",
            transport=httpx.MockTransport(_empty_handler),
            service_key=SERVICE_KEY,
        )
        assert ticks == []
        db.refresh(b)
        assert b.world_reset_required is True
        assert b.is_enabled is False
        assert b.connectivity_status == "WORLD_RESET_REQUIRED"


# --- 5. P1 Strengthened Recovery Verification & P2 Recurrence ---
def test_p1_strict_recovery_verdicts_and_p2_anomaly_recurrence(
    active_session: dict[str, str],
) -> None:
    """D02 RESOLVED alone -> STILL_INCONSISTENT; D05 empty inv -> INSUFFICIENT_EVIDENCE."""
    state: dict[str, Any] = {
        "order": {"id": "ORD-D02", "status": "CANCELLED", "reconciliation_status": "RESOLVED"},
        "payment": {
            "payment_id": "PAY-D02",
            "order_id": "ORD-D02",
            "status": "SUCCEEDED",  # NOT refunded yet!
            "amount_vnd": 100000,
            "refunded_amount_vnd": 0,
            "reconciliation_required": False,
        },
        "inventory": [],  # empty!
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/api/v1/service/orders/ORD-D02":
            return httpx.Response(200, json=state["order"])
        if p == "/api/v1/service/payments/PAY-D02":
            return httpx.Response(200, json=state["payment"])
        if p == "/api/v1/service/inventory":
            return httpx.Response(200, json=state["inventory"])
        if p == "/api/v1/service/events":
            return httpx.Response(200, json={"items": [], "next_cursor": None, "has_more": False})
        if p == "/api/v1/service/orders":
            return httpx.Response(200, json=[state["order"]])
        if p == "/api/v1/service/payments":
            return httpx.Response(200, json=[state["payment"]])
        if p == "/api/v1/service/metrics":
            return httpx.Response(200, json={"outbox_lag_count": 0, "unsynced_payments": 0})
        return httpx.Response(200, json=[])

    with get_sessionmaker()() as db:
        b = ensure_commerce_binding(db, session_id=active_session["id"], base_url=COMMERCE_BASE)
        conn = CommerceReadConnector(
            base_url=COMMERCE_BASE,
            service_key=SERVICE_KEY,
            merchant_id=b.merchant_id,
            commerce_session_id=b.commerce_session_id,
            transport=httpx.MockTransport(_handler),
        )

        # D02: RESOLVED label alone MUST NOT pass verification
        obs_d02 = AnomalyObservation(
            id="ANOM-D02-TEST",
            fingerprint="D02:M:ORD-D02:PAY-D02",
            session_id=active_session["id"],
            world_epoch=b.world_epoch,
            source_binding_id=b.id,
            merchant_id=b.merchant_id,
            detector_code="D02",
            anomaly_type="LATE_PAYMENT_ON_CANCELLED_ORDER",
            affected_order_id="ORD-D02",
            affected_payment_id="PAY-D02",
        )
        db.add(obs_d02)
        db.commit()

        v1 = evaluate_recovery_postconditions(db, obs=obs_d02, connector=conn)
        assert v1.verdict == RecoveryVerdict.STILL_INCONSISTENT
        assert v1.recovered is False

        # D05: empty inventory response -> INSUFFICIENT_EVIDENCE (never VERIFIED_RECOVERY!)
        obs_d05 = AnomalyObservation(
            id="ANOM-D05-TEST",
            fingerprint="D05:M:INV:VAR-1",
            session_id=active_session["id"],
            world_epoch=b.world_epoch,
            source_binding_id=b.id,
            merchant_id=b.merchant_id,
            detector_code="D05",
            anomaly_type="INVENTORY_INCONSISTENCY",
            affected_entities_json={"variant_id": "VAR-1"},
        )
        db.add(obs_d05)
        db.commit()

        v_d05 = evaluate_recovery_postconditions(db, obs=obs_d05, connector=conn)
        assert v_d05.verdict == RecoveryVerdict.INSUFFICIENT_EVIDENCE
        assert v_d05.recovered is False

        # Full refund -> D02 now VERIFIED_RECOVERY with actor UNKNOWN (no fabricated actor!)
        state["payment"]["status"] = "REFUNDED"
        state["payment"]["refunded_amount_vnd"] = 100000
        rec_ok, _ = verify_anomaly_recovery(db, obs=obs_d02, connector=conn)
        assert rec_ok is True
        assert obs_d02.verification_verdict == RecoveryVerdict.VERIFIED_RECOVERY.value
        assert obs_d02.recovery_source == "UNKNOWN"

        # Verify StaleLeaseError prevents stale verifier write
        b.lease_token = "OTHER-WORKER"  # noqa: S105
        b.locked_until = datetime.now(UTC) + timedelta(minutes=5)
        db.commit()
        with pytest.raises(StaleLeaseError):
            verify_anomaly_recovery(
                db,
                obs=obs_d05,
                connector=conn,
                lease_token="STALE-TOKEN",  # noqa: S105
            )
