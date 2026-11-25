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
    merchant_id: str,
    commerce_session_id: str,
    world_epoch: int,
    order_id: str | None,
    payment_id: str | None,
    event_id: str | None,
    expected_version: int | None,
) -> str:
    canonical = {
        "merchant_id": merchant_id,
        "commerce_session_id": commerce_session_id,
        "world_epoch": world_epoch,
        "command": command.strip().upper(),
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
        authorization_token: str,
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
        if not authorization_token or "." not in authorization_token:
            raise CommerceConnectorError(
                "MISSING_SIGNED_AUTHORIZATION_TOKEN: signed PolicyEngine authorization required",
                status_code=403,
                retryable=False,
            )
        url = f"{self.base_url}/api/v1/service/recovery/execute"
        headers = {
            "X-Commerce-Write-Key": self._write_key,
            "X-Merchant-Id": self.merchant_id,
            "X-Commerce-Session-Id": self.commerce_session_id,
            "X-World-Epoch": str(self.world_epoch),
            "X-Command-Authorization": authorization_token.strip(),
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
        merchant_id=binding.merchant_id,
        commerce_session_id=binding.commerce_session_id,
        world_epoch=binding.commerce_world_epoch,
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

    # 1b (Prompt 14.3 Fix 5 + Prompt 14.4 Fix 5): Atomic PostgreSQL advisory lock per target
    # resource prevents concurrent check-then-insert races across competing workers, AND
    # retains the mutation guard across MANUAL_REVIEW while execution_certainty is uncertain!
    from sqlalchemy import and_, or_
    from sqlalchemy import text as sa_text

    target_lock_key = f"rcmd:{binding.id}:{obs.affected_order_id or obs.id}"
    db.execute(sa_text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": target_lock_key})

    conflict_scope: list[Any] = [
        RecoveryCommand.session_id == binding.session_id,
        RecoveryCommand.source_binding_id == binding.id,
        or_(
            RecoveryCommand.state.in_(["EXECUTING", "UNKNOWN_OUTCOME", "VERIFYING"]),
            and_(
                RecoveryCommand.state == "MANUAL_REVIEW",
                RecoveryCommand.execution_certainty.in_(["UNKNOWN", "COMMAND_EXECUTION_UNKNOWN"]),
                RecoveryCommand.uncertainty_resolved_at.is_(None),
            ),
        ),
    ]
    if obs.affected_order_id:
        conflict_scope.append(RecoveryCommand.target_order_id == obs.affected_order_id)
    else:
        conflict_scope.append(RecoveryCommand.anomaly_id == obs.id)
    in_flight = db.scalar(select(RecoveryCommand).where(*conflict_scope).limit(1))
    if in_flight is not None:
        raise AppError(
            ErrorCode.CONFLICT,
            f"ACTIVE_OR_UNCERTAIN_COMMAND_IN_FLIGHT: command '{in_flight.id}' "
            f"(state='{in_flight.state}', certainty='{in_flight.execution_certainty}') "
            "blocks conflicting mutations until reconciled or explicitly resolved",
            status_code=409,
        )

    # 2. Mandatory deterministic PolicyEngine evaluation (NO bypass, NO hardcoded allow!)
    policy_engine = PolicyEngine(db, binding.session_id)
    decision: CommercePolicyDecision = policy_engine.evaluate_commerce_recovery(
        binding=binding,
        obs=obs,
        command=norm_cmd,
        request_hash=req_hash,
        idempotency_key=idempotency_key,
        expected_version=effective_expected_version,
        order_snapshot=fresh_order,
        payment_snapshot=fresh_payment,
        target_event_id=str(target_event_id) if target_event_id else None,
        approval_id=approval_id,
        require_human_approval=require_human_approval,
    )

    now = datetime.now(UTC)
    if (
        not decision.allowed
        or not decision.authorization_token
        or not decision.authorization_digest
    ):
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
            authorization_token=decision.authorization_token,
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
    cmd_row.execution_certainty = "COMMAND_EXECUTION_VERIFIED"
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
    uncertainty_grace_seconds: int = 30,
    max_not_found_probes: int = 3,
) -> RecoveryExecutionResult:
    """Safely reconcile a RecoveryCommand stuck in EXECUTING, UNKNOWN_OUTCOME, or VERIFYING.

    Hard Safety Invariants (Prompt 14.3 Fix 5):
    - NEVER blindly retries the mutation!
    - NEVER treats an isolated HTTP 404 from receiver status lookup as definitive proof the
      command never executed (an in-flight or uncommitted command can return 404 transiently).
    - Preserves UNKNOWN_OUTCOME until grace window + probe budget expire, then checks
      authoritative business state and escalates to MANUAL_REVIEW if still uncertain.
    - Requires ALL mandatory identity fields (`idempotency_key`, `request_hash`, `command`)
      in the receiver status response to match `cmd_row` before accepting EXECUTED.
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

    # Serialize concurrent reconciliation workers via row lock on RecoveryCommand
    locked_cmd = db.scalar(
        select(RecoveryCommand).where(RecoveryCommand.id == cmd_row.id).with_for_update()
    )
    if locked_cmd is not None:
        cmd_row = locked_cmd

    # If already VERIFYING, complete independent postcondition verification directly
    if cmd_row.state == "VERIFYING":
        recovered, detail = verify_anomaly_recovery(db, obs=obs, connector=reader)
        cmd_row.state = "VERIFIED" if recovered else "MANUAL_REVIEW"
        cmd_row.verification_json = {
            "recovered": recovered,
            "verdict": obs.verification_verdict,
            "recovery_source": obs.recovery_source,
            "detail": detail,
            "reconciled_from_verifying": True,
        }
        cmd_row.updated_at = datetime.now(UTC)
        db.commit()
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=cmd_row.command,
            executed=cmd_row.execution_certainty == "COMMAND_EXECUTION_VERIFIED",
            http_status=cmd_row.http_status or 200,
            verified_recovery=recovered,
            verdict=obs.verification_verdict or RecoveryVerdict.STILL_INCONSISTENT.value,
            recovery_source=obs.recovery_source,
            detail=f"Completed verification for stranded VERIFYING command: {detail}",
            response_payload=dict(cmd_row.response_json or {}),
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

    now = datetime.now(UTC)
    if status_code == 404:
        # NEVER treat an isolated HTTP 404 as definitive proof the command never executed!
        prev_resp = dict(cmd_row.response_json or {})
        probes = int(prev_resp.get("not_found_probes") or 0) + 1
        created = cmd_row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age_sec = max(0.0, (now - created).total_seconds())
        prev_resp["not_found_probes"] = probes
        prev_resp["last_404_at"] = now.isoformat()
        cmd_row.http_status = 404
        cmd_row.response_json = prev_resp
        cmd_row.updated_at = now

        if probes < max(1, max_not_found_probes) or age_sec < float(
            max(0, uncertainty_grace_seconds)
        ):
            cmd_row.state = "UNKNOWN_OUTCOME"
            db.commit()
            return RecoveryExecutionResult(
                command_id=cmd_row.id,
                command=cmd_row.command,
                executed=False,
                http_status=404,
                verified_recovery=False,
                verdict=RecoveryVerdict.UNKNOWN_OUTCOME.value,
                recovery_source=None,
                detail=(
                    f"Transient 404 on receiver lookup (probe {probes}/{max_not_found_probes}, "
                    f"age={age_sec:.1f}s); preserving UNKNOWN_OUTCOME"
                ),
                response_payload=status_data,
            )

        # Uncertainty window exhausted: verify authoritative business state before escalating
        # NEVER set executed=True solely because business state recovered after repeated 404s!
        recovered, detail = verify_anomaly_recovery(db, obs=obs, connector=reader)
        if recovered:
            cmd_row.state = "MANUAL_REVIEW"
            cmd_row.execution_certainty = "COMMAND_EXECUTION_UNKNOWN"
            cmd_row.verification_json = {
                "outcome_classification": "BUSINESS_STATE_RECOVERED",
                "command_certainty": "COMMAND_EXECUTION_UNKNOWN",
                "recovered": True,
                "verdict": "BUSINESS_STATE_RECOVERED",
                "recovery_source": obs.recovery_source,
                "detail": detail,
                "reconciled_after_404_probes": probes,
            }
            db.commit()
            return RecoveryExecutionResult(
                command_id=cmd_row.id,
                command=cmd_row.command,
                executed=False,
                http_status=404,
                verified_recovery=True,
                verdict="BUSINESS_STATE_RECOVERED",
                recovery_source=obs.recovery_source,
                detail=(
                    f"Business state observed recovered after 404 probes while command execution "
                    f"remains unconfirmed (COMMAND_EXECUTION_UNKNOWN): {detail}"
                ),
                response_payload=status_data,
            )

        cmd_row.state = "MANUAL_REVIEW"
        cmd_row.execution_certainty = "COMMAND_EXECUTION_UNKNOWN"
        cmd_row.verification_json = {
            "outcome_classification": "SAFELY_ESCALATED",
            "command_certainty": "COMMAND_EXECUTION_UNKNOWN",
            "recovered": False,
            "verdict": RecoveryVerdict.UNKNOWN_OUTCOME.value,
            "detail": (
                f"Receiver returned 404 across {probes} probes over {age_sec:.1f}s; "
                "escalated to MANUAL_REVIEW with uncertainty guard retained"
            ),
        }
        db.commit()
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=cmd_row.command,
            executed=False,
            http_status=404,
            verified_recovery=False,
            verdict=RecoveryVerdict.UNKNOWN_OUTCOME.value,
            recovery_source=None,
            detail="Uncertainty window exhausted after 404 probes; escalated to MANUAL_REVIEW",
            response_payload=status_data,
        )

    remote_status = str(status_data.get("status") or "").upper()
    remote_hash = str(status_data.get("request_hash") or "").strip()
    remote_cmd = str(status_data.get("command") or "").strip().upper()
    remote_idemp = str(status_data.get("idempotency_key") or "").strip()
    remote_epoch = status_data.get("world_epoch")

    # Prompt 14.3 Fix 5: ALL mandatory identity fields must be present and match cmd_row!
    if (
        not remote_hash
        or remote_hash != cmd_row.request_hash
        or not remote_cmd
        or remote_cmd != cmd_row.command.upper()
        or not remote_idemp
        or remote_idemp != cmd_row.idempotency_key
        or (remote_epoch is not None and int(remote_epoch) != int(cmd_row.world_epoch))
    ):
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=cmd_row.command,
            executed=False,
            http_status=status_code,
            verified_recovery=False,
            verdict=RecoveryVerdict.UNKNOWN_OUTCOME.value,
            recovery_source=None,
            detail=(
                "Receiver status lookup missing or mismatched mandatory identity fields "
                "(idempotency_key/request_hash/command/world_epoch); refusing reconciliation"
            ),
            response_payload=status_data,
        )

    if status_code == 200 and remote_status == "EXECUTED":
        cmd_row.http_status = 200
        cmd_row.response_json = dict(status_data.get("result") or status_data)
        cmd_row.execution_certainty = "COMMAND_EXECUTION_VERIFIED"
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


def reconcile_stranded_recovery_commands(
    db: Session,
    *,
    binding: CommerceSourceBinding,
    write_key: str | None = None,
    read_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
    read_connector: CommerceReadConnector | None = None,
    stale_executing_seconds: int = 30,
) -> int:
    """Reconcile commands stranded in EXECUTING, UNKNOWN_OUTCOME, or VERIFYING."""
    from datetime import timedelta

    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=max(5, stale_executing_seconds))
    stranded = list(
        db.scalars(
            select(RecoveryCommand).where(
                RecoveryCommand.source_binding_id == binding.id,
                RecoveryCommand.session_id == binding.session_id,
                RecoveryCommand.state.in_(["UNKNOWN_OUTCOME", "EXECUTING", "VERIFYING"]),
            )
        ).all()
    )
    reconciled = 0
    for cmd_row in stranded:
        if cmd_row.state == "EXECUTING":
            upd = cmd_row.updated_at
            if upd.tzinfo is None:
                upd = upd.replace(tzinfo=UTC)
            if upd > cutoff:
                continue
        if not cmd_row.anomaly_id:
            continue
        obs = db.get(AnomalyObservation, cmd_row.anomaly_id)
        if obs is None:
            continue
        res = reconcile_unknown_recovery_command(
            db,
            cmd_row=cmd_row,
            binding=binding,
            obs=obs,
            write_key=write_key,
            read_key=read_key,
            transport=transport,
            read_connector=read_connector,
        )
        if res.executed or cmd_row.state in ("VERIFIED", "FAILED", "MANUAL_REVIEW"):
            reconciled += 1
    return reconciled


def resolve_uncertain_recovery_command(
    db: Session,
    *,
    command_id: str,
    session_id: str,
    resolved_by: str,
    resolution_note: str = "Explicitly resolved after operator verification",
) -> RecoveryCommand:
    """Explicitly release an unresolved uncertainty guard after authorized manual review."""
    row = db.scalar(
        select(RecoveryCommand)
        .where(
            RecoveryCommand.id == command_id,
            RecoveryCommand.session_id == session_id,
        )
        .with_for_update()
    )
    if row is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"RecoveryCommand '{command_id}' not found in session '{session_id}'",
            status_code=404,
        )
    now = datetime.now(UTC)
    row.uncertainty_resolved_at = now
    row.uncertainty_resolved_by = resolved_by
    verif = dict(row.verification_json or {})
    verif["manual_resolution_note"] = resolution_note
    verif["manual_resolved_by"] = resolved_by
    verif["manual_resolved_at"] = now.isoformat()
    row.verification_json = verif
    row.updated_at = now
    db.commit()
    db.refresh(row)
    return row
