"""Prompt 14.3 Fix 5 Tests: Safe Unknown-Outcome Reconciliation & Conflicting Replan Blocking."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from app.connectors.commerce_write import (
    execute_and_verify_commerce_recovery,
    reconcile_unknown_recovery_command,
)
from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.open_world import RecoveryCommand


@pytest.fixture
def db(clean_database: None) -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def test_transient_404_preserves_unknown_outcome_blocks_replan_and_reconciles_on_200(
    db: Session, active_session: dict[str, str]
) -> None:
    """Prove:
    1. Isolated HTTP 404 NEVER marks FAILED — preserves UNKNOWN_OUTCOME.
    2. Missing/mismatched identity fields in receiver status lookup are rejected.
    3. Conflicting replanning on the same order is blocked while UNKNOWN_OUTCOME is active.
    4. Subsequent 200 EXECUTED with full matching identity reconciles cleanly to VERIFIED.
    """
    sid = active_session["id"]
    now = datetime.now(UTC)

    binding = CommerceSourceBinding(
        id=f"BIND-UNK-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        base_url="http://127.0.0.1:8100",
        merchant_id="MERCHANT_FINSHOP_01",
        commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
        commerce_world_epoch=1,
        is_enabled=True,
    )
    db.add(binding)
    db.flush()

    obs = AnomalyObservation(
        id=f"ANOM-UNK-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        world_epoch=1,
        source_binding_id=binding.id,
        merchant_id="MERCHANT_FINSHOP_01",
        detector_code="D01",
        anomaly_type="PAYMENT_SUCCEEDED_ORDER_NOT_CONFIRMED",
        severity="HIGH",
        fingerprint=f"fp-unk-{uuid.uuid4().hex[:8]}",
        affected_order_id="ORD-UNK-01",
        affected_payment_id="PAY-UNK-01",
        rationale="Payment succeeded while order still PENDING_PAYMENT",
        lifecycle_state="INVESTIGATING",
    )
    db.add(obs)
    db.flush()

    cmd = RecoveryCommand(
        id=f"RCMD-UNK-{uuid.uuid4().hex[:8].upper()}",
        session_id=sid,
        idempotency_key=f"IDEMP-UNK-{uuid.uuid4().hex[:8]}",
        request_hash="sha256-exact-hash-01",
        command="REQUEST_ORDER_SYNC",
        anomaly_id=obs.id,
        source_binding_id=binding.id,
        merchant_id="MERCHANT_FINSHOP_01",
        commerce_session_id="CSESS_FINSHOP_DEFAULT_01",
        world_epoch=1,
        target_order_id="ORD-UNK-01",
        target_payment_id="PAY-UNK-01",
        expected_version=1,
        policy_decision="ALLOW_AUTONOMOUS",
        state="UNKNOWN_OUTCOME",
        created_at=now,
        updated_at=now,
    )
    db.add(cmd)
    db.commit()

    mode: dict[str, Any] = {"status": "404"}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/v1/service/recovery/commands/"):
            if mode["status"] == "404":
                return httpx.Response(404, json={"detail": "Not found yet"})
            if mode["status"] == "MISSING_HASH":
                return httpx.Response(
                    200,
                    json={
                        "status": "EXECUTED",
                        "command": "REQUEST_ORDER_SYNC",
                        "idempotency_key": cmd.idempotency_key,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "status": "EXECUTED",
                    "command": "REQUEST_ORDER_SYNC",
                    "idempotency_key": cmd.idempotency_key,
                    "request_hash": "sha256-exact-hash-01",
                    "world_epoch": 1,
                    "result": {"status": "SYNCED", "order_status": "CONFIRMED"},
                },
            )
        if request.url.path.endswith("/timeline"):
            return httpx.Response(
                200,
                json=[{"payload": {"sync_source": "FINRESOLVE_RECOVERY_COMMAND"}}],
            )
        if request.url.path.startswith("/api/v1/service/orders/"):
            return httpx.Response(
                200,
                json={
                    "id": "ORD-UNK-01",
                    "status": "CONFIRMED",
                    "reconciliation_status": "CLEAR",
                    "version": 2,
                    "total_vnd": 150000,
                },
            )
        if request.url.path.startswith("/api/v1/service/payments/"):
            return httpx.Response(
                200,
                json={
                    "id": "PAY-UNK-01",
                    "order_id": "ORD-UNK-01",
                    "status": "SUCCEEDED",
                    "is_synced_to_order": True,
                    "reconciliation_required": False,
                    "amount_vnd": 150000,
                    "confirmed_at": (now - timedelta(minutes=2)).isoformat(),
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(_handler)

    # 1. Transient 404 MUST keep UNKNOWN_OUTCOME (NOT FAILED!)
    res_404 = reconcile_unknown_recovery_command(
        db, cmd_row=cmd, binding=binding, obs=obs, transport=transport
    )
    assert res_404.executed is False
    assert res_404.verdict == "UNKNOWN_OUTCOME"
    assert cmd.state == "UNKNOWN_OUTCOME"

    # 2. Conflicting replanning on same order with a NEW idempotency_key MUST be blocked (409)!
    with pytest.raises(AppError) as exc_blk:
        execute_and_verify_commerce_recovery(
            db,
            binding=binding,
            obs=obs,
            command="REQUEST_ORDER_SYNC",
            idempotency_key=f"IDEMP-NEW-{uuid.uuid4().hex[:8]}",
            expected_version=1,
            transport=transport,
        )
    assert exc_blk.value.status_code == 409
    assert "ACTIVE_OR_UNCERTAIN_COMMAND_IN_FLIGHT" in exc_blk.value.message

    # 3. Receiver 200 EXECUTED with MISSING request_hash MUST be rejected (stays UNKNOWN_OUTCOME)!
    mode["status"] = "MISSING_HASH"
    res_bad = reconcile_unknown_recovery_command(
        db, cmd_row=cmd, binding=binding, obs=obs, transport=transport
    )
    assert res_bad.executed is False
    assert res_bad.verdict == "UNKNOWN_OUTCOME"
    assert cmd.state == "UNKNOWN_OUTCOME"

    # 4. Receiver 200 EXECUTED with full matching identity -> reconciles cleanly to VERIFIED!
    mode["status"] = "EXECUTED"
    res_ok = reconcile_unknown_recovery_command(
        db, cmd_row=cmd, binding=binding, obs=obs, transport=transport
    )
    assert res_ok.executed is True
    assert res_ok.verified_recovery is True
    assert cmd.state == "VERIFIED"
    assert obs.lifecycle_state == "RECOVERED"
