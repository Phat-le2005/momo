"""Prompt 14.8 Shared Sandbox Ownership, Cross-Workspace Fencing & Reset Tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.connectors.commerce import CommerceReadConnector
from app.connectors.commerce_write import execute_and_verify_commerce_recovery
from app.core.config import get_settings
from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.models.actions import ActionProposal
from app.models.cases import Case, Evidence
from app.models.enums import ActionProposalStatus, CaseState, RiskLevel, SessionStatus
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.open_world import RecoveryCommand
from app.models.session import DemoSession
from app.models.supervisor import SupervisorJob
from app.supervisor.service import _run_commerce_post_investigation_recovery


def test_fix1_shared_sandbox_single_controller_read_only_preview_and_release(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only one active workspace may control the shared Commerce sandbox at a time;
    other guests receive READ_ONLY_PREVIEW and 403 on scan/reset; release is blocked
    while unresolved uncertain commands exist.
    """
    monkeypatch.setenv("COMMERCE_SERVICE_BASE_URL", "http://127.0.0.1:8100")
    get_settings.cache_clear()

    try:
        # 1. Guest A claims controller role
        ba = client.post("/api/v1/experience/bootstrap", json={}).json()
        assert ba["session"]["workspace_role"] == "CONTROLLER"
        assert ba["sandbox"]["workspace_role"] == "CONTROLLER"
        assert ba["sandbox"]["binding_id"] is not None
        ha = {"X-Demo-Session": ba["session"]["token"]}
        sid_a = ba["session"]["session_id"]

        # 2. Guest A reloads -> restores CONTROLLER ownership idempotently
        ba_reload = client.post("/api/v1/experience/bootstrap", headers=ha, json={}).json()
        assert ba_reload["restored"] is True
        assert ba_reload["session"]["workspace_role"] == "CONTROLLER"

        # 3. Guest B arrives -> receives READ_ONLY_PREVIEW (no binding, no private data)
        bb = client.post("/api/v1/experience/bootstrap", json={}).json()
        assert bb["session"]["workspace_role"] == "READ_ONLY_PREVIEW"
        assert bb["sandbox"]["binding_id"] is None
        hb = {"X-Demo-Session": bb["session"]["token"]}

        ov_b = client.get("/api/v1/experience/overview", headers=hb).json()
        assert ov_b["workspace_role"] == "READ_ONLY_PREVIEW"
        assert ov_b["anomalies"] == []
        assert ov_b["recent_business_events"] == []

        # 4. Unauthorized Guest B cannot scan or reset the shared sandbox
        assert client.post("/api/v1/experience/scan", headers=hb).status_code == 403
        assert client.post("/api/v1/experience/reset", headers=hb).status_code == 403

        # 5. Seed unresolved UNKNOWN_OUTCOME guard -> Guest A CANNOT release ownership yet (409)
        sm = get_sessionmaker()
        now = datetime.now(UTC)
        with sm() as db:
            cmd = RecoveryCommand(
                id=f"RCMD-{uuid.uuid4().hex[:8].upper()}",
                session_id=sid_a,
                idempotency_key=f"IDEMP-HOLD-{uuid.uuid4().hex[:6]}",
                request_hash="b" * 64,
                command="REQUEST_ORDER_SYNC",
                source_binding_id=ba["sandbox"]["binding_id"],
                merchant_id="MERCHANT_FINSHOP_01",
                commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
                world_epoch=1,
                target_order_id="ORD-HOLD-148",
                policy_decision="ALLOW",
                state="UNKNOWN_OUTCOME",
                execution_certainty="COMMAND_EXECUTION_UNKNOWN",
                created_at=now,
                updated_at=now,
            )
            db.add(cmd)
            db.commit()
            cmd_id = cmd.id

        assert client.post("/api/v1/experience/release", headers=ha).status_code == 409

        # 6. Resolve guard -> Guest A releases -> Guest B can now claim CONTROLLER
        with sm() as db:
            row = db.get(RecoveryCommand, cmd_id)
            assert row is not None
            row.state = "VERIFIED"
            row.execution_certainty = "COMMAND_EXECUTION_VERIFIED"
            db.commit()

        r_rel = client.post("/api/v1/experience/release", headers=ha)
        assert r_rel.status_code == 200
        assert r_rel.json()["status"] == "OWNERSHIP_RELEASED"

        bb_takeover = client.post("/api/v1/experience/bootstrap", headers=hb, json={}).json()
        assert bb_takeover["session"]["workspace_role"] == "CONTROLLER"
        assert bb_takeover["sandbox"]["binding_id"] is not None
    finally:
        get_settings.cache_clear()


def test_fix1_cross_workspace_canonical_resource_guard_blocks_conflicting_mutation() -> None:
    """Two different FinResolve workspaces targeting the same canonical Commerce order
    cannot issue conflicting mutations while an uncertain guard is active.
    """
    sm = get_sessionmaker()
    now = datetime.now(UTC)
    with sm() as db:
        sess_a = DemoSession(
            id=f"sess_a_{uuid.uuid4().hex[:8]}",
            token=f"dsk_{uuid.uuid4().hex}",
            status=SessionStatus.ACTIVE.value,
            max_cases=20,
            case_count=0,
            expires_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        sess_b = DemoSession(
            id=f"sess_b_{uuid.uuid4().hex[:8]}",
            token=f"dsk_{uuid.uuid4().hex}",
            status=SessionStatus.ACTIVE.value,
            max_cases=20,
            case_count=0,
            expires_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        db.add_all([sess_a, sess_b])
        db.flush()

        bind_a = CommerceSourceBinding(
            id=f"CSB-A-{uuid.uuid4().hex[:6].upper()}",
            session_id=sess_a.id,
            world_epoch=1,
            connector_id="default_ministore",
            base_url="http://127.0.0.1:8100",
            merchant_id="MERCHANT_FINSHOP_01",
            commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
            commerce_world_epoch=1,
            is_enabled=True,
            connectivity_status="CONNECTED",
            created_at=now,
            updated_at=now,
        )
        bind_b = CommerceSourceBinding(
            id=f"CSB-B-{uuid.uuid4().hex[:6].upper()}",
            session_id=sess_b.id,
            world_epoch=1,
            connector_id="default_ministore",
            base_url="http://127.0.0.1:8100",
            merchant_id="MERCHANT_FINSHOP_01",
            commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
            commerce_world_epoch=1,
            is_enabled=True,
            connectivity_status="CONNECTED",
            created_at=now,
            updated_at=now,
        )
        db.add_all([bind_a, bind_b])
        db.flush()

        # Workspace A holds an unresolved UNKNOWN_OUTCOME command on ORD-SHARED-99
        guard_a = RecoveryCommand(
            id=f"RCMD-A-{uuid.uuid4().hex[:6].upper()}",
            session_id=sess_a.id,
            idempotency_key=f"IDEMP-A-{uuid.uuid4().hex[:6]}",
            request_hash="c" * 64,
            command="REQUEST_ORDER_SYNC",
            source_binding_id=bind_a.id,
            merchant_id="MERCHANT_FINSHOP_01",
            commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
            world_epoch=1,
            target_order_id="ORD-SHARED-99",
            target_payment_id="PAY-SHARED-99",
            policy_decision="ALLOW",
            state="UNKNOWN_OUTCOME",
            execution_certainty="COMMAND_EXECUTION_UNKNOWN",
            created_at=now,
            updated_at=now,
        )
        obs_b = AnomalyObservation(
            id=f"ANOM-B-{uuid.uuid4().hex[:6].upper()}",
            session_id=sess_b.id,
            world_epoch=1,
            source_binding_id=bind_b.id,
            merchant_id="MERCHANT_FINSHOP_01",
            detector_code="D01",
            anomaly_type="PAYMENT_SYNC_MISMATCH",
            severity="HIGH",
            fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
            lifecycle_state="INVESTIGATING",
            affected_order_id="ORD-SHARED-99",
            affected_payment_id="PAY-SHARED-99",
            rationale="Cross-workspace conflict test",
            evidence_refs_json=[],
            affected_entities_json={},
            first_observed_at=now,
            last_observed_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add_all([guard_a, obs_b])
        db.commit()

        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={}))
        reader = CommerceReadConnector(
            base_url=bind_b.base_url,
            service_key="rk",
            merchant_id=bind_b.merchant_id,
            commerce_session_id=bind_b.commerce_session_id,
            world_epoch=1,
            transport=transport,
        )

        # Workspace B MUST be blocked with 409 by Workspace A's canonical business resource guard!
        with pytest.raises(AppError) as exc_info:
            execute_and_verify_commerce_recovery(
                db,
                binding=bind_b,
                obs=obs_b,
                command="REQUEST_ORDER_SYNC",
                idempotency_key=f"IDEMP-B-{uuid.uuid4().hex[:6]}",
                expected_version=1,
                order_snapshot={"id": "ORD-SHARED-99", "status": "PENDING_PAYMENT", "version": 1},
                payment_snapshot={
                    "id": "PAY-SHARED-99",
                    "order_id": "ORD-SHARED-99",
                    "status": "SUCCEEDED",
                    "provider": "LOCAL_MOCK",
                    "is_synced_to_order": False,
                },
                write_key="wk",
                transport=transport,
                read_connector=reader,
            )
        assert exc_info.value.status_code == 409
        assert "ACTIVE_OR_UNCERTAIN_COMMAND_IN_FLIGHT" in exc_info.value.message


def test_fix2_reset_cancels_approved_proposals_and_fences_stale_worker(
    client: TestClient,
) -> None:
    """Reset cancels both PROPOSED and APPROVED (undispatched) proposals, distinguishes
    dashboard_world_epoch from commerce_world_epoch, and fences stale workers before dispatch.
    """
    boot = client.post("/api/v1/experience/bootstrap", json={}).json()
    h = {"X-Demo-Session": boot["session"]["token"]}
    sid = boot["session"]["session_id"]

    sm = get_sessionmaker()
    now = datetime.now(UTC)
    with sm() as db:
        bind = CommerceSourceBinding(
            id=f"CSB-F-{uuid.uuid4().hex[:6].upper()}",
            session_id=sid,
            world_epoch=1,
            connector_id="default_ministore",
            base_url="http://127.0.0.1:8100",
            merchant_id="MERCHANT_FINSHOP_01",
            commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
            commerce_world_epoch=1,
            is_enabled=True,
            connectivity_status="CONNECTED",
            created_at=now,
            updated_at=now,
        )
        db.add(bind)
        db.flush()
        case = Case(
            id=f"CASE-F-{uuid.uuid4().hex[:6].upper()}",
            session_id=sid,
            type="PAYMENT_SYNC_ANOMALY",
            goal="Fence test",
            state=CaseState.WAITING_APPROVAL.value,
            version=1,
            owner="AUTONOMOUS_SUPERVISOR",
            source_system="COMMERCE_HTTP",
            source_binding_id=bind.id,
            created_at=now,
            updated_at=now,
        )
        db.add(case)
        db.flush()
        obs = AnomalyObservation(
            id=f"ANOM-F-{uuid.uuid4().hex[:6].upper()}",
            session_id=sid,
            world_epoch=1,
            source_binding_id=bind.id,
            merchant_id="MERCHANT_FINSHOP_01",
            detector_code="D01",
            anomaly_type="PAYMENT_SYNC_MISMATCH",
            severity="HIGH",
            fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
            lifecycle_state="INVESTIGATING",
            affected_order_id="ORD-FENCE-1",
            affected_payment_id="PAY-FENCE-1",
            linked_case_id=case.id,
            rationale="Fence test",
            evidence_refs_json=[],
            affected_entities_json={},
            first_observed_at=now,
            last_observed_at=now,
            created_at=now,
            updated_at=now,
        )
        approved_prop = ActionProposal(
            id=f"PROP-APPR-{uuid.uuid4().hex[:6].upper()}",
            case_id=case.id,
            session_id=sid,
            action_type="ORDER_SYNC",
            params_json={"order_id": "ORD-FENCE-1", "expected_order_version": 1},
            evidence_refs=[],
            expected_version=1,
            risk_level=RiskLevel.LOW.value,
            status=ActionProposalStatus.APPROVED.value,
            created_at=now,
        )
        stale_job = SupervisorJob(
            id=f"JOB-STALE-{uuid.uuid4().hex[:6].upper()}",
            session_id=sid,
            world_epoch=1,
            job_type="INVESTIGATE_CASE",
            dedupe_key=f"dk-{uuid.uuid4().hex[:8]}",
            target_case_id=case.id,
            status="RUNNING",
            worker_id="stale-worker",
            claim_token="TOK-STALE-1",
            locked_until=now + timedelta(minutes=2),
            created_at=now,
            updated_at=now,
        )
        ev = Evidence(
            id=f"EVD-F-{uuid.uuid4().hex[:6].upper()}",
            case_id=case.id,
            session_id=sid,
            kind="OBSERVED_FACT",
            source="COMMERCE_HTTP",
            source_ref="ORD-FENCE-1",
            payload_json={"order": {"id": "ORD-FENCE-1", "version": 1}},
            observed_at=now,
            confidence=1.0,
        )
        db.add_all([obs, approved_prop, stale_job, ev])
        db.commit()
        case_id = case.id
        obs_id = obs.id
        prop_id = approved_prop.id
        job_id = stale_job.id

    # Reset into dashboard_world_epoch=2 while commerce_world_epoch stays 1
    r_reset = client.post("/api/v1/experience/reset", headers=h)
    assert r_reset.status_code == 200
    body = r_reset.json()
    assert body["dashboard_world_epoch"] == 2
    assert body["commerce_world_epoch"] == 1
    assert body["expired_proposals_count"] == 1

    with sm() as db:
        prop_after = db.get(ActionProposal, prop_id)
        assert prop_after is not None
        assert prop_after.status == ActionProposalStatus.CANCELLED.value

        # Stale worker from World 1 attempting post-investigation recovery MUST be fenced!
        fence_msg = _run_commerce_post_investigation_recovery(
            db,
            case_id=case_id,
            session_id=sid,
            run_id="RUN-STALE-1",
            job_id=job_id,
            claim_token="TOK-STALE-1",
            run_started_at=now,
            is_fallback_mode=False,
            anomaly_id=obs_id,
        )
        assert fence_msg == "STALE_WORLD_EPOCH_FENCED"
