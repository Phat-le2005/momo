"""Prompt 14.1 Phase 1 Security Tests: Actual PolicyEngine Enforcement & Approval Validation."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.commerce import CommerceReadConnector
from app.connectors.commerce_write import execute_and_verify_commerce_recovery
from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.models.actions import ActionProposal, Approval
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.open_world import RecoveryCommand


@pytest.fixture
def db(clean_database: None) -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def _make_session_binding_obs(
    db: Session,
    *,
    session_id: str,
    order_id: str = "ORD-SEC-01",
    payment_id: str = "PAY-SEC-01",
    world_epoch: int = 1,
) -> tuple[str, CommerceSourceBinding, AnomalyObservation]:
    binding = CommerceSourceBinding(
        id=f"BIND-{uuid.uuid4().hex[:8].upper()}",
        session_id=session_id,
        world_epoch=world_epoch,
        base_url="http://127.0.0.1:8100",
        merchant_id="MERCHANT_FINSHOP_01",
        commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
        commerce_world_epoch=world_epoch,
        is_enabled=True,
    )
    db.add(binding)
    db.flush()

    obs = AnomalyObservation(
        id=f"ANOM-{uuid.uuid4().hex[:8].upper()}",
        session_id=session_id,
        world_epoch=world_epoch,
        source_binding_id=binding.id,
        merchant_id="MERCHANT_FINSHOP_01",
        detector_code="D01",
        anomaly_type="PAYMENT_SUCCEEDED_ORDER_NOT_CONFIRMED",
        severity="HIGH",
        fingerprint=f"fp-{uuid.uuid4().hex[:10]}",
        affected_order_id=order_id,
        affected_payment_id=payment_id,
        rationale="Authoritative payment succeeded while order still PENDING_PAYMENT",
        lifecycle_state="OBSERVED",
    )
    db.add(obs)
    db.commit()
    return session_id, binding, obs


def _mock_transport(
    *,
    order_state: dict[str, Any],
    payment_state: dict[str, Any],
    exec_calls: list[dict[str, Any]],
    mutate_on_exec: bool = True,
) -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/timeline"):
            return httpx.Response(200, json=[])
        if request.method == "GET" and path.startswith("/api/v1/service/orders/"):
            return httpx.Response(200, json=dict(order_state))
        if request.method == "GET" and path.startswith("/api/v1/service/payments/"):
            return httpx.Response(200, json=dict(payment_state))
        if request.method == "POST" and path == "/api/v1/service/recovery/execute":
            exec_calls.append(
                {
                    "headers": dict(request.headers),
                    "body": request.content.decode("utf-8"),
                }
            )
            if mutate_on_exec:
                order_state["status"] = "CONFIRMED"
                order_state["version"] = int(order_state.get("version", 1)) + 1
                payment_state["is_synced_to_order"] = True
            from tests.helpers_recovery import mock_receiver_response

            return httpx.Response(
                200,
                json=mock_receiver_response(
                    request,
                    status="SYNCED",
                    extra={"order_status": order_state["status"]},
                ),
            )
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(_handler)


def test_policy_engine_enforces_authoritative_evidence_and_blocks_bypasses(
    db: Session, active_session: dict[str, str]
) -> None:
    """PolicyEngine denies unauthorized mutations without mutating Commerce state."""
    _, binding, obs = _make_session_binding_obs(db, session_id=active_session["id"])
    exec_calls: list[dict[str, Any]] = []

    # 1. External VNPAY provider MUST be denied by PolicyEngine before any HTTP write
    vnpay_transport = _mock_transport(
        order_state={"id": "ORD-SEC-01", "status": "PENDING_PAYMENT", "version": 1},
        payment_state={
            "id": "PAY-SEC-01",
            "order_id": "ORD-SEC-01",
            "status": "SUCCEEDED",
            "provider": "VNPAY_SANDBOX",
            "is_synced_to_order": False,
        },
        exec_calls=exec_calls,
    )
    reader = CommerceReadConnector(
        base_url=binding.base_url,
        service_key="test-read-key",
        merchant_id=binding.merchant_id,
        commerce_session_id=binding.commerce_session_id,
        transport=vnpay_transport,
    )
    with pytest.raises(AppError) as exc_vnpay:
        execute_and_verify_commerce_recovery(
            db,
            binding=binding,
            obs=obs,
            command="REQUEST_ORDER_SYNC",
            idempotency_key=f"IDEMP-VNPAY-{uuid.uuid4().hex[:6]}",
            expected_version=1,
            write_key="test-write-key",
            transport=vnpay_transport,
            read_connector=reader,
        )
    assert exc_vnpay.value.status_code == 403
    assert "EXTERNAL_PROVIDER_WRITE_FORBIDDEN" in exc_vnpay.value.message
    assert len(exec_calls) == 0

    # 2. Cancelled order resurrection MUST be denied by PolicyEngine
    cancel_transport = _mock_transport(
        order_state={"id": "ORD-SEC-01", "status": "CANCELLED", "version": 2},
        payment_state={
            "id": "PAY-SEC-01",
            "order_id": "ORD-SEC-01",
            "status": "SUCCEEDED",
            "provider": "LOCAL_MOCK",
            "is_synced_to_order": False,
        },
        exec_calls=exec_calls,
    )
    cancel_reader = CommerceReadConnector(
        base_url=binding.base_url,
        service_key="test-read-key",
        merchant_id=binding.merchant_id,
        commerce_session_id=binding.commerce_session_id,
        transport=cancel_transport,
    )
    with pytest.raises(AppError) as exc_cancel:
        execute_and_verify_commerce_recovery(
            db,
            binding=binding,
            obs=obs,
            command="REQUEST_ORDER_SYNC",
            idempotency_key=f"IDEMP-CANCEL-{uuid.uuid4().hex[:6]}",
            expected_version=2,
            write_key="test-write-key",
            transport=cancel_transport,
            read_connector=cancel_reader,
        )
    assert exc_cancel.value.status_code == 403
    assert "CANCELLED_ORDER_RESURRECTION_FORBIDDEN" in exc_cancel.value.message
    assert len(exec_calls) == 0

    # 3. Valid LOCAL_MOCK evidence -> PolicyEngine authorizes and sends X-Authorization-Digest
    ok_transport = _mock_transport(
        order_state={"id": "ORD-SEC-01", "status": "PENDING_PAYMENT", "version": 1},
        payment_state={
            "id": "PAY-SEC-01",
            "order_id": "ORD-SEC-01",
            "status": "SUCCEEDED",
            "provider": "LOCAL_MOCK",
            "is_synced_to_order": False,
        },
        exec_calls=exec_calls,
    )
    ok_reader = CommerceReadConnector(
        base_url=binding.base_url,
        service_key="test-read-key",
        merchant_id=binding.merchant_id,
        commerce_session_id=binding.commerce_session_id,
        transport=ok_transport,
    )
    res = execute_and_verify_commerce_recovery(
        db,
        binding=binding,
        obs=obs,
        command="REQUEST_ORDER_SYNC",
        idempotency_key=f"IDEMP-OK-{uuid.uuid4().hex[:6]}",
        expected_version=1,
        write_key="test-write-key",
        transport=ok_transport,
        read_connector=ok_reader,
    )
    assert res.executed is True
    assert len(exec_calls) == 1
    assert "." in exec_calls[0]["headers"].get("x-command-authorization", "")
    assert len(exec_calls[0]["headers"].get("x-authorization-digest", "")) == 64

    cmd_row = db.scalar(select(RecoveryCommand).where(RecoveryCommand.id == res.command_id))
    assert cmd_row is not None
    assert cmd_row.policy_decision != "ALLOW_LOW_RISK_LOCAL_MOCK"
    assert cmd_row.policy_decision.startswith("ALLOW_POLICY_ENGINE")


def test_policy_engine_rejects_missing_or_expired_human_approval(
    db: Session, active_session: dict[str, str]
) -> None:
    """When human approval is required, missing or expired Approval records are rejected."""
    sid, binding, obs = _make_session_binding_obs(
        db, session_id=active_session["id"], order_id="ORD-APPR-01", payment_id="PAY-APPR-01"
    )
    exec_calls: list[dict[str, Any]] = []
    transport = _mock_transport(
        order_state={"id": "ORD-APPR-01", "status": "PENDING_PAYMENT", "version": 1},
        payment_state={
            "id": "PAY-APPR-01",
            "order_id": "ORD-APPR-01",
            "status": "SUCCEEDED",
            "provider": "LOCAL_MOCK",
            "is_synced_to_order": False,
        },
        exec_calls=exec_calls,
    )
    reader = CommerceReadConnector(
        base_url=binding.base_url,
        service_key="test-read-key",
        merchant_id=binding.merchant_id,
        commerce_session_id=binding.commerce_session_id,
        transport=transport,
    )

    # Missing approval
    with pytest.raises(AppError) as exc_missing:
        execute_and_verify_commerce_recovery(
            db,
            binding=binding,
            obs=obs,
            command="REQUEST_ORDER_SYNC",
            idempotency_key=f"IDEMP-NOAPPR-{uuid.uuid4().hex[:6]}",
            expected_version=1,
            require_human_approval=True,
            write_key="test-write-key",
            transport=transport,
            read_connector=reader,
        )
    assert exc_missing.value.status_code == 403
    assert "HUMAN_APPROVAL_REQUIRED" in exc_missing.value.message

    # Expired approval
    prop = ActionProposal(
        id=f"PROP-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        action_type="ORDER_SYNC",
        params_json={"order_id": "ORD-APPR-01"},
        expected_version=1,
        status="APPROVED",
    )
    db.add(prop)
    db.flush()
    expired_appr = Approval(
        id=f"APPR-{uuid.uuid4().hex[:8].upper()}",
        proposal_id=prop.id,
        approver_role="OPS_MANAGER",
        approver_id="mgr_01",
        policy_version="FinResolve-Policy-v1.2",
        decision="APPROVED",
        digest="a" * 64,
        expiry=datetime.now(UTC) - timedelta(minutes=5),
    )
    db.add(expired_appr)
    db.commit()

    with pytest.raises(AppError) as exc_exp:
        execute_and_verify_commerce_recovery(
            db,
            binding=binding,
            obs=obs,
            command="REQUEST_ORDER_SYNC",
            idempotency_key=f"IDEMP-EXPAPPR-{uuid.uuid4().hex[:6]}",
            expected_version=1,
            require_human_approval=True,
            approval_id=expired_appr.id,
            write_key="test-write-key",
            transport=transport,
            read_connector=reader,
        )
    assert exc_exp.value.status_code == 403
    assert "APPROVAL_EXPIRED" in exc_exp.value.message

    # Unexpired approval WITHOUT request_hash binding MUST be rejected (403)
    unexpired_no_hash = Approval(
        id=f"APPR-{uuid.uuid4().hex[:8].upper()}",
        proposal_id=prop.id,
        approver_role="OPS_MANAGER",
        approver_id="mgr_01",
        policy_version="FinResolve-Policy-v1.2",
        decision="APPROVED",
        digest="b" * 64,
        expiry=datetime.now(UTC) + timedelta(minutes=30),
    )
    db.add(unexpired_no_hash)
    db.commit()

    with pytest.raises(AppError) as exc_nohash:
        execute_and_verify_commerce_recovery(
            db,
            binding=binding,
            obs=obs,
            command="REQUEST_ORDER_SYNC",
            idempotency_key=f"IDEMP-NOHASH-{uuid.uuid4().hex[:6]}",
            expected_version=1,
            require_human_approval=True,
            approval_id=unexpired_no_hash.id,
            write_key="test-write-key",
            transport=transport,
            read_connector=reader,
        )
    assert exc_nohash.value.status_code == 403
    assert "APPROVAL_MISSING_REQUEST_HASH" in exc_nohash.value.message
    assert len(exec_calls) == 0


def test_unknown_outcome_reconciliation_via_receiver_status_lookup(
    db: Session, active_session: dict[str, str]
) -> None:
    """Timeout -> UNKNOWN_OUTCOME (no blind retry) -> reconciled via receiver status lookup."""
    from app.connectors.commerce_write import reconcile_unknown_recovery_command

    _, binding, obs = _make_session_binding_obs(
        db, session_id=active_session["id"], order_id="ORD-TOUT-01", payment_id="PAY-TOUT-01"
    )
    order_state: dict[str, Any] = {
        "id": "ORD-TOUT-01",
        "status": "PENDING_PAYMENT",
        "version": 1,
    }
    payment_state: dict[str, Any] = {
        "id": "PAY-TOUT-01",
        "order_id": "ORD-TOUT-01",
        "status": "SUCCEEDED",
        "provider": "LOCAL_MOCK",
        "is_synced_to_order": False,
    }
    idemp_key = f"IDEMP-TOUT-{uuid.uuid4().hex[:8]}"

    captured: dict[str, Any] = {"request_hash": None}

    def _timeout_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/timeline"):
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path.startswith("/api/v1/service/orders/"):
            return httpx.Response(200, json=dict(order_state))
        if request.method == "GET" and request.url.path.startswith("/api/v1/service/payments/"):
            return httpx.Response(200, json=dict(payment_state))
        if request.method == "POST" and request.url.path == "/api/v1/service/recovery/execute":
            # Simulate receiver actually mutating state, then response lost (timeout)
            order_state["status"] = "CONFIRMED"
            order_state["version"] = 2
            payment_state["is_synced_to_order"] = True
            raise httpx.ReadTimeout("simulated lost response after receiver commit")
        if request.method == "GET" and request.url.path.endswith(f"/commands/{idemp_key}"):
            return httpx.Response(
                200,
                json={
                    "idempotency_key": idemp_key,
                    "command": "REQUEST_ORDER_SYNC",
                    "request_hash": captured["request_hash"],
                    "world_epoch": binding.commerce_world_epoch,
                    "status": "EXECUTED",
                    "http_status": 200,
                    "result": {"command": "REQUEST_ORDER_SYNC", "status": "SYNCED"},
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_timeout_handler)
    reader = CommerceReadConnector(
        base_url=binding.base_url,
        service_key="test-read-key",
        merchant_id=binding.merchant_id,
        commerce_session_id=binding.commerce_session_id,
        transport=transport,
    )

    # 1. Execution times out -> UNKNOWN_OUTCOME recorded
    res1 = execute_and_verify_commerce_recovery(
        db,
        binding=binding,
        obs=obs,
        command="REQUEST_ORDER_SYNC",
        idempotency_key=idemp_key,
        expected_version=1,
        write_key="test-write-key",
        transport=transport,
        read_connector=reader,
    )
    assert res1.verdict == "UNKNOWN_OUTCOME"
    cmd_row = db.scalar(select(RecoveryCommand).where(RecoveryCommand.id == res1.command_id))
    assert cmd_row is not None
    assert cmd_row.state == "UNKNOWN_OUTCOME"
    captured["request_hash"] = cmd_row.request_hash

    # 2. Blind retry with same idempotency_key MUST be blocked (409)
    with pytest.raises(AppError) as exc_retry:
        execute_and_verify_commerce_recovery(
            db,
            binding=binding,
            obs=obs,
            command="REQUEST_ORDER_SYNC",
            idempotency_key=idemp_key,
            expected_version=1,
            write_key="test-write-key",
            transport=transport,
            read_connector=reader,
        )
    assert exc_retry.value.status_code == 409
    assert "UNKNOWN_OUTCOME_NO_BLIND_RETRY" in exc_retry.value.message

    # 3. Reconcile via receiver status lookup -> verifies actual postconditions -> VERIFIED
    rec_res = reconcile_unknown_recovery_command(
        db,
        cmd_row=cmd_row,
        binding=binding,
        obs=obs,
        write_key="test-write-key",
        transport=transport,
        read_connector=reader,
    )
    assert rec_res.executed is True
    assert rec_res.verified_recovery is True
    db.refresh(cmd_row)
    assert cmd_row.state == "VERIFIED"


def test_insecure_default_secrets_rejected_and_readonly_mode_allowed() -> None:
    """Fix 1 (Prompt 14.3): Reject rotated/missing signing secrets; allow explicit read-only."""
    import secrets

    from pydantic import ValidationError

    from app.core.config import Settings

    strong_approval = secrets.token_hex(32)
    strong_sign = secrets.token_hex(32)
    strong_write = secrets.token_hex(32)

    # 1. Publicly known rotated signing secret MUST be rejected even in local env
    with pytest.raises(ValidationError):
        Settings(
            app_env="local",
            demo_approval_secret=strong_approval,
            command_auth_signing_secret="finresolve-local-only-command-auth-hmac-secret-2026-change-me",
        )

    # 2. Deployed env with autonomous_recovery_enabled=True and missing signing secret -> rejected
    with pytest.raises(ValidationError):
        Settings(
            app_env="docker",
            demo_approval_secret=strong_approval,
            autonomous_recovery_enabled=True,
            command_auth_signing_secret="",
            commerce_write_key="",
        )

    # 3. Deployed env with signing secret == write key -> rejected (must be separate!)
    with pytest.raises(ValidationError):
        Settings(
            app_env="docker",
            demo_approval_secret=strong_approval,
            autonomous_recovery_enabled=True,
            command_auth_signing_secret=strong_sign,
            commerce_write_key=strong_sign,
        )

    # 4. Deployed env with autonomous_recovery_enabled=False -> read-only boots without signing key
    ro = Settings(
        app_env="docker",
        demo_approval_secret=strong_approval,
        autonomous_recovery_enabled=False,
        command_auth_signing_secret="",
        commerce_write_key="",
    )
    assert ro.autonomous_recovery_enabled is False

    # 5. Deployed env with distinct strong >= 256-bit secrets succeeds
    ok = Settings(
        app_env="docker",
        demo_approval_secret=strong_approval,
        autonomous_recovery_enabled=True,
        command_auth_signing_secret=strong_sign,
        commerce_write_key=strong_write,
    )
    assert len(ok.command_auth_signing_secret.get_secret_value()) == 64
