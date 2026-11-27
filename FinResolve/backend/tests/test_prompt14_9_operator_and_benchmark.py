"""Prompt 14.9 Operator Authorization, Canonical Resource Fencing & Benchmark Math Tests."""

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
from app.models.enums import SessionStatus
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.open_world import RecoveryCommand
from app.models.session import DemoSession
from scripts.run_benchmark import compute_classification_metrics


def test_prompt14_9_operator_token_blocks_anonymous_controller_claim(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When EXPERIENCE_OPERATOR_TOKEN is configured, anonymous visitors MUST receive
    READ_ONLY_PREVIEW (never CONTROLLER), while authorized operators claim & restore CONTROLLER.
    """
    monkeypatch.setenv("COMMERCE_SERVICE_BASE_URL", "http://127.0.0.1:8100")
    monkeypatch.setenv("EXPERIENCE_OPERATOR_TOKEN", "op-secret-token-for-test-only-32chars")
    get_settings.cache_clear()

    try:
        # 1. Anonymous visitor arrives first -> MUST NOT become CONTROLLER!
        anon = client.post("/api/v1/experience/bootstrap", json={}).json()
        assert anon["session"]["workspace_role"] == "READ_ONLY_PREVIEW"
        assert anon["sandbox"]["binding_id"] is None

        # 2. Wrong operator token -> still READ_ONLY_PREVIEW
        wrong = client.post(
            "/api/v1/experience/bootstrap",
            headers={"X-Operator-Token": "wrong-token"},
            json={},
        ).json()
        assert wrong["session"]["workspace_role"] == "READ_ONLY_PREVIEW"

        # 3. Authorized operator token -> becomes CONTROLLER
        op = client.post(
            "/api/v1/experience/bootstrap",
            headers={"X-Operator-Token": "op-secret-token-for-test-only-32chars"},
            json={},
        ).json()
        assert op["session"]["workspace_role"] == "CONTROLLER"
        assert op["sandbox"]["binding_id"] is not None
        assert "op-secret-token-for-test-only-32chars" not in str(op)

        # 4. Authorized operator reloads with X-Demo-Session -> restores CONTROLLER
        op_reload = client.post(
            "/api/v1/experience/bootstrap",
            headers={"X-Demo-Session": op["session"]["token"]},
            json={},
        ).json()
        assert op_reload["restored"] is True
        assert op_reload["session"]["workspace_role"] == "CONTROLLER"
    finally:
        get_settings.cache_clear()


def test_prompt14_9_canonical_resource_fencing_across_payment_and_revoked_binding() -> None:
    """Conflicting command referencing the same order via payment_snapshot (without
    obs.affected_order_id) MUST be blocked by canonical business resource guard, and a
    revoked binding MUST be rejected at the dispatch boundary with 403.
    """
    sm = get_sessionmaker()
    now = datetime.now(UTC)
    with sm() as db:
        sess = DemoSession(
            id=f"sess_c149_{uuid.uuid4().hex[:8]}",
            token=f"dsk_{uuid.uuid4().hex}",
            status=SessionStatus.ACTIVE.value,
            max_cases=20,
            case_count=0,
            expires_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        db.add(sess)
        db.flush()
        binding = CommerceSourceBinding(
            id=f"CSB-149-{uuid.uuid4().hex[:6].upper()}",
            session_id=sess.id,
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
        db.add(binding)
        db.flush()

        # Existing UNKNOWN_OUTCOME guard on ORD-CANON-149
        guard = RecoveryCommand(
            id=f"RCMD-149-{uuid.uuid4().hex[:6].upper()}",
            session_id=sess.id,
            idempotency_key=f"IDEMP-C149-{uuid.uuid4().hex[:6]}",
            request_hash="d" * 64,
            command="REQUEST_ORDER_SYNC",
            source_binding_id=binding.id,
            merchant_id="MERCHANT_FINSHOP_01",
            commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
            world_epoch=1,
            target_order_id="ORD-CANON-149",
            target_payment_id="PAY-CANON-A",
            policy_decision="ALLOW",
            state="UNKNOWN_OUTCOME",
            execution_certainty="COMMAND_EXECUTION_UNKNOWN",
            created_at=now,
            updated_at=now,
        )
        # New anomaly references ONLY a payment pointing to ORD-CANON-149 (affected_order_id=None)
        obs = AnomalyObservation(
            id=f"ANOM-149-{uuid.uuid4().hex[:6].upper()}",
            session_id=sess.id,
            world_epoch=1,
            source_binding_id=binding.id,
            merchant_id="MERCHANT_FINSHOP_01",
            detector_code="D01",
            anomaly_type="PAYMENT_SYNC_MISMATCH",
            severity="HIGH",
            fingerprint=f"fp-{uuid.uuid4().hex[:8]}",
            lifecycle_state="INVESTIGATING",
            affected_order_id=None,
            affected_payment_id="PAY-CANON-B",
            rationale="Indirect order reference via payment",
            evidence_refs_json=[],
            affected_entities_json={},
            first_observed_at=now,
            last_observed_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add_all([guard, obs])
        db.commit()

        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={}))
        reader = CommerceReadConnector(
            base_url=binding.base_url,
            service_key="rk",
            merchant_id=binding.merchant_id,
            commerce_session_id=binding.commerce_session_id,
            world_epoch=1,
            transport=transport,
        )

        # 1. Indirect reference to ORD-CANON-149 via payment_snapshot -> blocked (409)!
        with pytest.raises(AppError) as exc_conflict:
            execute_and_verify_commerce_recovery(
                db,
                binding=binding,
                obs=obs,
                command="REQUEST_ORDER_SYNC",
                idempotency_key=f"IDEMP-C149-2-{uuid.uuid4().hex[:6]}",
                expected_version=1,
                order_snapshot={"id": "ORD-CANON-149", "status": "PENDING_PAYMENT", "version": 1},
                payment_snapshot={
                    "id": "PAY-CANON-B",
                    "order_id": "ORD-CANON-149",
                    "status": "SUCCEEDED",
                    "provider": "LOCAL_MOCK",
                    "is_synced_to_order": False,
                },
                write_key="wk",
                transport=transport,
                read_connector=reader,
            )
        assert exc_conflict.value.status_code == 409

        # 2. Revoked binding at dispatch boundary -> blocked (403)!
        binding.is_enabled = False
        db.commit()
        with pytest.raises(AppError) as exc_revoked:
            execute_and_verify_commerce_recovery(
                db,
                binding=binding,
                obs=obs,
                command="REQUEST_ORDER_SYNC",
                idempotency_key=f"IDEMP-C149-3-{uuid.uuid4().hex[:6]}",
                expected_version=1,
                order_snapshot={"id": "ORD-OTHER-999", "status": "PENDING_PAYMENT", "version": 1},
                payment_snapshot={
                    "id": "PAY-OTHER-999",
                    "order_id": "ORD-OTHER-999",
                    "status": "SUCCEEDED",
                    "provider": "LOCAL_MOCK",
                    "is_synced_to_order": False,
                },
                write_key="wk",
                transport=transport,
                read_connector=reader,
            )
        assert exc_revoked.value.status_code == 403
        assert "CONTROLLER_OWNERSHIP_REVOKED" in exc_revoked.value.message


def test_prompt14_9_benchmark_statistics_valid_bounds_and_zero_denominators() -> None:
    """Verify FPR is bounded in [0.0, 1.0] and zero denominators return None."""
    m = compute_classification_metrics(tp=1, fp=0, tn=3, fn=0)
    assert m["precision"] == 1.0
    assert m["recall"] == 1.0
    assert m["false_positive_rate"] == 0.0
    assert m["f1_score"] == 1.0

    m_zero = compute_classification_metrics(tp=0, fp=0, tn=0, fn=0)
    assert m_zero["precision"] is None
    assert m_zero["recall"] is None
    assert m_zero["false_positive_rate"] is None
    assert m_zero["f1_score"] is None
