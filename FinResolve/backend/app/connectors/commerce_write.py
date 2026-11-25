"""Policy-Gated Commerce Write Connector & Durable Recovery Command Protocol (Prompt 13 + 14).

Security & Safety Invariants:
- Uses dedicated `X-Commerce-Write-Key` separate from read credentials.
- Persists every recovery command as a durable `RecoveryCommand` record with canonical
  `request_hash` and `(session_id, idempotency_key)` uniqueness.
- Rejects conflicting idempotency-key reuse (`409 CONFLICT`), preserves `UNKNOWN_OUTCOME`
  across restarts (never blindly retries uncertain operations!), and NEVER trusts HTTP 200
  alone — ALWAYS independently re-reads authoritative MiniStore state via `CommerceReadConnector`!
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.commerce import (
    CommerceConnectorError,
    CommerceReadConnector,
    validate_approved_commerce_url,
)
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.models.cases import CaseEvent
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.open_world import RecoveryCommand
from app.monitoring.service import (
    RecoveryVerdict,
    build_connector_for_binding,
    verify_anomaly_recovery,
)
from app.policy.engine import CommercePolicyDecision, PolicyEngine

LOW_RISK_AUTO_COMMANDS: frozenset[str] = frozenset(
    {
        "REQUEST_ORDER_SYNC",
        "REQUEST_EVENT_REDELIVERY",
        "REQUEST_PAYMENT_RECONCILIATION",
        "REQUEST_MANUAL_REVIEW",
    }
)


@dataclass
class RecoveryExecutionResult:
    command_id: str | None
    command: str
    executed: bool
    http_status: int | None
    verified_recovery: bool
    verdict: str
    recovery_source: str | None
    detail: str
    response_payload: dict[str, Any]
    idempotent_replay: bool = False


def _compute_command_hash(
    *,
    command: str,
    binding_id: str,
    order_id: str | None,
    payment_id: str | None,
    event_id: str | None,
    expected_version: int | None,
) -> str:
    canonical = {
        "command": command.strip().upper(),
        "binding_id": binding_id,
        "order_id": order_id,
        "payment_id": payment_id,
        "event_id": event_id,
        "expected_version": expected_version,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class CommerceWriteConnector:
    """Dedicated server-side write client for MiniStore `/api/v1/service/recovery/execute`."""

    def __init__(
        self,
        *,
        base_url: str,
        write_key: str,
        merchant_id: str,
        commerce_session_id: str,
        world_epoch: int = 1,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = validate_approved_commerce_url(base_url)
        self._write_key = write_key
        self.merchant_id = merchant_id
        self.commerce_session_id = commerce_session_id
        self.world_epoch = world_epoch
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    def _scrub(self, text: str) -> str:
        if self._write_key and self._write_key in text:
            return text.replace(self._write_key, "[REDACTED]")
        return text

    def execute_command(
        self,
        *,
        command: str,
        idempotency_key: str,
        authorization_digest: str,
        order_id: str | None = None,
        payment_id: str | None = None,
        event_id: str | None = None,
        expected_version: int | None = None,
        reason: str = "FinResolve policy-gated recovery",
    ) -> tuple[int, dict[str, Any]]:
        norm_cmd = command.strip().upper()
        if norm_cmd not in LOW_RISK_AUTO_COMMANDS:
            raise CommerceConnectorError(
                f"FORBIDDEN_WRITE_COMMAND: '{norm_cmd}' is not permitted",
                status_code=403,
                retryable=False,
            )
        if not authorization_digest or len(authorization_digest.strip()) < 32:
            raise CommerceConnectorError(
                "MISSING_POLICY_AUTHORIZATION_DIGEST: PolicyEngine authorization required",
                status_code=403,
                retryable=False,
            )
        url = f"{self.base_url}/api/v1/service/recovery/execute"
        headers = {
            "X-Commerce-Write-Key": self._write_key,
            "X-Merchant-Id": self.merchant_id,
            "X-Commerce-Session-Id": self.commerce_session_id,
            "X-World-Epoch": str(self.world_epoch),
            "X-Authorization-Digest": authorization_digest.strip(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body: dict[str, Any] = {
            "command": norm_cmd,
            "idempotency_key": idempotency_key,
            "reason": reason,
        }
        if order_id:
            body["order_id"] = order_id
        if payment_id:
            body["payment_id"] = payment_id
        if event_id:
            body["event_id"] = event_id
        if expected_version is not None:
            body["expected_version"] = expected_version

        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                resp = client.post(url, headers=headers, json=body)
            if 300 <= resp.status_code < 400:
                raise CommerceConnectorError(
                    "CONNECTOR_REDIRECT_BLOCKED: redirects forbidden on recovery writes",
                    status_code=resp.status_code,
                    retryable=False,
                )
            data = resp.json() if resp.content else {}
            return resp.status_code, data if isinstance(data, dict) else {"raw": data}
        except CommerceConnectorError:
            raise
        except httpx.TimeoutException as exc:
            raise CommerceConnectorError(
                self._scrub(f"Recovery command {norm_cmd} timed out (UNKNOWN_OUTCOME): {exc}"),
                retryable=False,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise CommerceConnectorError(
                self._scrub(f"Recovery command {norm_cmd} error: {str(exc)[:200]}"),
                retryable=False,
            ) from exc

    def get_command_status(self, idempotency_key: str) -> tuple[int, dict[str, Any]]:
        """Authoritative receiver-side command status lookup for unknown-outcome reconciliation."""
        url = f"{self.base_url}/api/v1/service/recovery/commands/{idempotency_key}"
        headers = {
            "X-Commerce-Write-Key": self._write_key,
            "X-Merchant-Id": self.merchant_id,
            "X-Commerce-Session-Id": self.commerce_session_id,
            "X-World-Epoch": str(self.world_epoch),
            "Accept": "application/json",
        }
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                resp = client.get(url, headers=headers)
            data = resp.json() if resp.content else {}
            return resp.status_code, data if isinstance(data, dict) else {"raw": data}
        except Exception as exc:  # noqa: BLE001
            raise CommerceConnectorError(
                self._scrub(f"Status lookup failed for {idempotency_key}: {str(exc)[:200]}"),
                retryable=True,
            ) from exc


def execute_and_verify_commerce_recovery(
    db: Session,
    *,
    binding: CommerceSourceBinding,
    obs: AnomalyObservation,
    command: str,
    idempotency_key: str,
    expected_version: int | None = None,
    approval_id: str | None = None,
    require_human_approval: bool = False,
    order_snapshot: dict[str, Any] | None = None,
    payment_snapshot: dict[str, Any] | None = None,
    write_key: str | None = None,
    read_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
    read_connector: CommerceReadConnector | None = None,
) -> RecoveryExecutionResult:
    """Durable Recovery Command Protocol: PROPOSED -> POLICY -> EXECUTING -> VERIFYING -> VERIFIED.

    - ALWAYS evaluates deterministic `PolicyEngine.evaluate_commerce_recovery(...)` against fresh
      authoritative Commerce state immediately before execution; NEVER uses hardcoded allow labels.
    - Enforces idempotency_key uniqueness and rejects conflicting payload reuse (`409 CONFLICT`).
    - Never blindly retries `UNKNOWN_OUTCOME` commands!
    - Independently re-reads authoritative MiniStore state after execution.
    """
    norm_cmd = command.strip().upper()
    target_event_id = (obs.affected_entities_json or {}).get("event_id")
    reader = read_connector or build_connector_for_binding(
        binding, service_key=read_key, transport=transport
    )

    # Refresh authoritative evidence immediately before PolicyEngine evaluation
    fresh_order = order_snapshot
    fresh_payment = payment_snapshot
    if fresh_order is None and obs.affected_order_id:
        try:
            fresh_order = reader.get_order(obs.affected_order_id)["data"]
        except CommerceConnectorError:
            fresh_order = None
    if fresh_payment is None and obs.affected_payment_id:
        try:
            fresh_payment = reader.get_payment(obs.affected_payment_id)["data"]
        except CommerceConnectorError:
            fresh_payment = None

    effective_expected_version = expected_version
    if (
        effective_expected_version is None
        and fresh_order is not None
        and isinstance(fresh_order.get("version"), int)
    ):
        effective_expected_version = int(fresh_order["version"])

    req_hash = _compute_command_hash(
        command=norm_cmd,
        binding_id=binding.id,
        order_id=obs.affected_order_id,
        payment_id=obs.affected_payment_id,
        event_id=str(target_event_id) if target_event_id else None,
        expected_version=effective_expected_version,
    )

    # 1. Check durable idempotency record under lock
    existing = db.scalar(
        select(RecoveryCommand)
        .where(
            RecoveryCommand.session_id == binding.session_id,
            RecoveryCommand.idempotency_key == idempotency_key,
        )
        .with_for_update()
    )
    if existing is not None:
        if existing.request_hash != req_hash:
            raise AppError(
                ErrorCode.CONFLICT,
                "RECOVERY_IDEMPOTENCY_CONFLICT: idempotency_key reused with different payload",
                status_code=409,
            )
        if existing.state == "UNKNOWN_OUTCOME":
            raise AppError(
                ErrorCode.CONFLICT,
                "UNKNOWN_OUTCOME_NO_BLIND_RETRY: previous command outcome is UNKNOWN",
                status_code=409,
            )
        return RecoveryExecutionResult(
            command_id=existing.id,
            command=existing.command,
            executed=existing.state in ("VERIFYING", "VERIFIED"),
            http_status=existing.http_status,
            verified_recovery=existing.state == "VERIFIED",
            verdict=str((existing.verification_json or {}).get("verdict") or existing.state),
            recovery_source=obs.recovery_source,
            detail=f"Idempotent replay of durable command {existing.id} (state={existing.state})",
            response_payload=dict(existing.response_json or {}),
            idempotent_replay=True,
        )

    # 2. Mandatory deterministic PolicyEngine evaluation (NO bypass, NO hardcoded allow!)
    policy_engine = PolicyEngine(db, binding.session_id)
    decision: CommercePolicyDecision = policy_engine.evaluate_commerce_recovery(
        binding=binding,
        obs=obs,
        command=norm_cmd,
        request_hash=req_hash,
        expected_version=effective_expected_version,
        order_snapshot=fresh_order,
        payment_snapshot=fresh_payment,
        target_event_id=str(target_event_id) if target_event_id else None,
        approval_id=approval_id,
        require_human_approval=require_human_approval,
    )

    now = datetime.now(UTC)
    if not decision.allowed or not decision.authorization_digest:
        rejected_cmd = RecoveryCommand(
            id=f"RCMD-{uuid.uuid4().hex[:10].upper()}",
            session_id=binding.session_id,
            idempotency_key=idempotency_key,
            request_hash=req_hash,
            command=norm_cmd,
            case_id=obs.linked_case_id,
            anomaly_id=obs.id,
            source_binding_id=binding.id,
            merchant_id=binding.merchant_id,
            commerce_session_id=binding.commerce_session_id,
            world_epoch=binding.commerce_world_epoch,
            target_order_id=obs.affected_order_id,
            target_payment_id=obs.affected_payment_id,
            target_event_id=str(target_event_id) if target_event_id else None,
            expected_version=effective_expected_version,
            policy_decision=decision.decision_code[:32],
            approval_id=approval_id,
            state="REJECTED",
            response_json={
                "violation_code": decision.violation_code,
                "violation_message": decision.violation_message,
                "policy_version": decision.policy_version,
                "checked_preconditions": decision.checked_preconditions,
            },
            created_at=now,
            updated_at=now,
        )
        db.add(rejected_cmd)
        if obs.linked_case_id:
            db.add(
                CaseEvent(
                    id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
                    case_id=obs.linked_case_id,
                    session_id=obs.session_id,
                    type="COMMERCE_RECOVERY_POLICY_DENIED",
                    payload_json={
                        "command_id": rejected_cmd.id,
                        "command": norm_cmd,
                        "decision_code": decision.decision_code,
                        "violation_code": decision.violation_code,
                        "violation_message": decision.violation_message,
                        "policy_version": decision.policy_version,
                    },
                    created_at=now,
                )
            )
        db.commit()
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"POLICY_ENGINE_DENIED ({decision.violation_code}): {decision.violation_message}",
            status_code=403,
            details={
                "command_id": rejected_cmd.id,
                "decision_code": decision.decision_code,
                "violation_code": decision.violation_code,
            },
        )

    cmd_row = RecoveryCommand(
        id=f"RCMD-{uuid.uuid4().hex[:10].upper()}",
        session_id=binding.session_id,
        idempotency_key=idempotency_key,
        request_hash=req_hash,
        command=norm_cmd,
        case_id=obs.linked_case_id,
        anomaly_id=obs.id,
        source_binding_id=binding.id,
        merchant_id=binding.merchant_id,
        commerce_session_id=binding.commerce_session_id,
        world_epoch=binding.commerce_world_epoch,
        target_order_id=obs.affected_order_id,
        target_payment_id=obs.affected_payment_id,
        target_event_id=str(target_event_id) if target_event_id else None,
        expected_version=effective_expected_version,
        policy_decision=decision.decision_code[:32],
        approval_id=decision.approval_id,
        state="EXECUTING",
        created_at=now,
        updated_at=now,
    )
    db.add(cmd_row)
    db.commit()

    settings = get_settings()
    w_key = write_key if write_key is not None else settings.commerce_write_key.get_secret_value()
    writer = CommerceWriteConnector(
        base_url=binding.base_url,
        write_key=w_key,
        merchant_id=binding.merchant_id,
        commerce_session_id=binding.commerce_session_id,
        world_epoch=binding.commerce_world_epoch,
        timeout_seconds=settings.commerce_connector_timeout_seconds,
        transport=transport,
    )

    try:
        http_status, resp_data = writer.execute_command(
            command=norm_cmd,
            idempotency_key=idempotency_key,
            authorization_digest=decision.authorization_digest,
            order_id=obs.affected_order_id,
            payment_id=obs.affected_payment_id,
            event_id=str(target_event_id) if target_event_id else None,
            expected_version=effective_expected_version,
        )
    except CommerceConnectorError as exc:
        is_timeout = "timed out" in str(exc).lower()
        cmd_row.state = "UNKNOWN_OUTCOME" if is_timeout else "FAILED"
        cmd_row.http_status = exc.status_code
        cmd_row.response_json = {"error": str(exc)}
        cmd_row.updated_at = datetime.now(UTC)
        verdict = (
            RecoveryVerdict.UNKNOWN_OUTCOME.value
            if is_timeout
            else RecoveryVerdict.SOURCE_UNAVAILABLE.value
        )
        obs.verification_verdict = verdict
        db.commit()
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=norm_cmd,
            executed=False,
            http_status=exc.status_code,
            verified_recovery=False,
            verdict=verdict,
            recovery_source=None,
            detail=str(exc),
            response_payload={},
        )

    cmd_row.http_status = http_status
    cmd_row.response_json = resp_data
    cmd_row.state = "VERIFYING" if http_status == 200 else "FAILED"
    cmd_row.updated_at = datetime.now(UTC)

    if obs.linked_case_id:
        db.add(
            CaseEvent(
                id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
                case_id=obs.linked_case_id,
                session_id=obs.session_id,
                type="COMMERCE_RECOVERY_COMMAND_EXECUTED",
                payload_json={
                    "command_id": cmd_row.id,
                    "command": norm_cmd,
                    "http_status": http_status,
                    "idempotency_key": idempotency_key,
                    "response": resp_data,
                },
                created_at=datetime.now(UTC),
            )
        )
    db.commit()

    if http_status != 200:
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=norm_cmd,
            executed=False,
            http_status=http_status,
            verified_recovery=False,
            verdict=RecoveryVerdict.STILL_INCONSISTENT.value,
            recovery_source=None,
            detail=f"Commerce recovery command returned HTTP {http_status}",
            response_payload=resp_data,
        )

    # Independent post-action verification (NEVER trust HTTP 200 alone!)
    recovered, detail = verify_anomaly_recovery(db, obs=obs, connector=reader)
    cmd_row.state = "VERIFIED" if recovered else "MANUAL_REVIEW"
    cmd_row.verification_json = {
        "recovered": recovered,
        "verdict": obs.verification_verdict,
        "recovery_source": obs.recovery_source,
        "detail": detail,
    }
    cmd_row.updated_at = datetime.now(UTC)
    db.commit()

    return RecoveryExecutionResult(
        command_id=cmd_row.id,
        command=norm_cmd,
        executed=True,
        http_status=http_status,
        verified_recovery=recovered,
        verdict=obs.verification_verdict or RecoveryVerdict.STILL_INCONSISTENT.value,
        recovery_source=obs.recovery_source,
        detail=detail,
        response_payload=resp_data,
    )


def reconcile_unknown_recovery_command(
    db: Session,
    *,
    cmd_row: RecoveryCommand,
    binding: CommerceSourceBinding,
    obs: AnomalyObservation,
    write_key: str | None = None,
    read_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
    read_connector: CommerceReadConnector | None = None,
) -> RecoveryExecutionResult:
    """Reconcile a RecoveryCommand stuck in EXECUTING or UNKNOWN_OUTCOME via authoritative lookup.

    NEVER blindly retries the mutation! Queries MiniStore `GET /commands/{idempotency_key}` first,
    then runs independent business postcondition verification if MiniStore confirms execution.
    """
    settings = get_settings()
    w_key = write_key if write_key is not None else settings.commerce_write_key.get_secret_value()
    writer = CommerceWriteConnector(
        base_url=binding.base_url,
        write_key=w_key,
        merchant_id=binding.merchant_id,
        commerce_session_id=binding.commerce_session_id,
        world_epoch=binding.commerce_world_epoch,
        timeout_seconds=settings.commerce_connector_timeout_seconds,
        transport=transport,
    )
    reader = read_connector or build_connector_for_binding(
        binding, service_key=read_key, transport=transport
    )

    try:
        status_code, status_data = writer.get_command_status(cmd_row.idempotency_key)
    except CommerceConnectorError as exc:
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=cmd_row.command,
            executed=False,
            http_status=exc.status_code,
            verified_recovery=False,
            verdict=RecoveryVerdict.UNKNOWN_OUTCOME.value,
            recovery_source=None,
            detail=f"Authoritative status lookup unavailable: {exc}",
            response_payload={},
        )

    if status_code == 404:
        cmd_row.state = "FAILED"
        cmd_row.http_status = 404
        cmd_row.response_json = {"reconciliation": "NOT_RECEIVED_BY_COMMERCE"}
        cmd_row.updated_at = datetime.now(UTC)
        db.commit()
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=cmd_row.command,
            executed=False,
            http_status=404,
            verified_recovery=False,
            verdict=RecoveryVerdict.STILL_INCONSISTENT.value,
            recovery_source=None,
            detail="Receiver confirmed command was never executed (404); safe to re-plan",
            response_payload=status_data,
        )

    remote_status = str(status_data.get("status") or "").upper()
    if status_code == 200 and remote_status == "EXECUTED":
        cmd_row.http_status = 200
        cmd_row.response_json = dict(status_data.get("result") or status_data)
        cmd_row.state = "VERIFYING"
        cmd_row.updated_at = datetime.now(UTC)
        db.commit()

        recovered, detail = verify_anomaly_recovery(db, obs=obs, connector=reader)
        cmd_row.state = "VERIFIED" if recovered else "MANUAL_REVIEW"
        cmd_row.verification_json = {
            "recovered": recovered,
            "verdict": obs.verification_verdict,
            "recovery_source": obs.recovery_source,
            "detail": detail,
            "reconciled_from_unknown": True,
        }
        cmd_row.updated_at = datetime.now(UTC)
        db.commit()
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=cmd_row.command,
            executed=True,
            http_status=200,
            verified_recovery=recovered,
            verdict=obs.verification_verdict or RecoveryVerdict.STILL_INCONSISTENT.value,
            recovery_source=obs.recovery_source,
            detail=f"Reconciled via receiver status lookup: {detail}",
            response_payload=cmd_row.response_json,
        )

    return RecoveryExecutionResult(
        command_id=cmd_row.id,
        command=cmd_row.command,
        executed=False,
        http_status=status_code,
        verified_recovery=False,
        verdict=RecoveryVerdict.UNKNOWN_OUTCOME.value,
        recovery_source=None,
        detail=f"Receiver command still in state '{remote_status}' (HTTP {status_code})",
        response_payload=status_data,
    )
