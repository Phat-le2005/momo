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

# Prompt 14.6 Fix 3: Strict receiver semantic status allowlists (never treat unknown as executed)
SUPPORTED_EXECUTED_STATUSES: frozenset[str] = frozenset(
    {"EXECUTED", "SYNCED", "ALREADY_SYNCED", "APPLIED", "CONFIRMED"}
)
SUPPORTED_REJECTED_STATUSES: frozenset[str] = frozenset({"REJECTED", "FAILED", "DENIED", "ERROR"})
SUPPORTED_ESCALATED_STATUSES: frozenset[str] = frozenset(
    {"ESCALATED_MANUAL_REVIEW", "MANUAL_REVIEW"}
)

# Prompt 14.7 Fix 2: Machine-readable 409 conflict codes that prove NO mutation occurred.
# NOTE: IDEMPOTENCY_KEY_CONFLICT / REQUEST_HASH_MISMATCH are intentionally EXCLUDED because an
# idempotency hash conflict cannot prove that an earlier command never executed!
CONFIRMED_409_REJECTION_CODES: frozenset[str] = frozenset(
    {
        "VERSION_MISMATCH",
        "STALE_VERSION",
        "STALE_WORLD_EPOCH",
        "SCOPE_MISMATCH",
        "INVALID_AUTHORIZATION",
    }
)


def _is_strict_int(val: Any) -> bool:
    return isinstance(val, int) and not isinstance(val, bool)


def _extract_conflict_reason_code(resp_json: dict[str, Any]) -> str:
    """Extract ONLY documented machine-readable conflict reason_code from HTTP 409 body.

    Prompt 14.7 Fix 2: Never infer confirmed rejection from free-text message prefixes!
    """
    if not isinstance(resp_json, dict):
        return ""
    direct = str(resp_json.get("reason_code") or "").strip().upper()
    if direct:
        return direct
    err_obj = resp_json.get("error")
    if isinstance(err_obj, dict):
        details = err_obj.get("details")
        if isinstance(details, dict):
            rc = str(details.get("reason_code") or "").strip().upper()
            if rc:
                return rc
    return ""


def _validate_receiver_http200_identity(
    *,
    resp_json: dict[str, Any],
    expected_idempotency_key: str,
    expected_command: str,
    expected_request_hash: str,
    expected_merchant_id: str,
    expected_commerce_session_id: str,
    expected_world_epoch: int,
    expected_order_id: str | None,
    expected_payment_id: str | None = None,
    expected_event_id: str | None = None,
) -> tuple[bool, str]:
    """Strictly verify complete receiver identity envelope & semantic status on HTTP 200."""
    if not isinstance(resp_json, dict) or not resp_json:
        return False, "EMPTY_OR_MALFORMED_RESPONSE_BODY"

    resp_cmd_id = str(resp_json.get("command_id") or resp_json.get("id") or "").strip()
    resp_idemp = str(resp_json.get("idempotency_key") or "").strip()
    resp_cmd = str(resp_json.get("command") or "").strip().upper()
    resp_hash = str(resp_json.get("request_hash") or "").strip()
    resp_merchant = str(resp_json.get("merchant_id") or "").strip()
    resp_csess = str(resp_json.get("commerce_session_id") or "").strip()
    resp_status = str(resp_json.get("status") or "").strip().upper()
    resp_epoch = resp_json.get("world_epoch")

    if not resp_cmd_id:
        return False, "MISSING_RECEIVER_COMMAND_ID"
    if not resp_idemp or resp_idemp != expected_idempotency_key:
        return False, "MISSING_OR_MISMATCHED_IDEMPOTENCY_KEY"
    if not resp_cmd or resp_cmd != expected_command:
        return False, "MISSING_OR_MISMATCHED_COMMAND"
    if not resp_hash or resp_hash != expected_request_hash:
        return False, "MISSING_OR_MISMATCHED_REQUEST_HASH"
    if not resp_merchant or resp_merchant != expected_merchant_id:
        return False, "MISSING_OR_MISMATCHED_MERCHANT_SCOPE"
    if not resp_csess or resp_csess != expected_commerce_session_id:
        return False, "MISSING_OR_MISMATCHED_COMMERCE_SESSION"
    if (
        not isinstance(resp_epoch, int)
        or isinstance(resp_epoch, bool)
        or resp_epoch != int(expected_world_epoch)
    ):
        return False, "MISSING_OR_INVALID_WORLD_EPOCH"

    resp_order = str(resp_json.get("order_id") or resp_json.get("target_order_id") or "").strip()
    resp_payment = str(
        resp_json.get("payment_id") or resp_json.get("target_payment_id") or ""
    ).strip()
    resp_event = str(resp_json.get("event_id") or resp_json.get("target_event_id") or "").strip()

    if expected_order_id and expected_command in (
        "REQUEST_ORDER_SYNC",
        "REQUEST_PAYMENT_RECONCILIATION",
    ):
        if not resp_order or resp_order != expected_order_id:
            return False, "MISSING_OR_MISMATCHED_TARGET_ORDER"
    elif resp_order and expected_order_id and resp_order != expected_order_id:
        return False, "MISMATCHED_TARGET_ORDER"

    if expected_payment_id and expected_command == "REQUEST_ORDER_SYNC":
        if not resp_payment or resp_payment != expected_payment_id:
            return False, "MISSING_OR_MISMATCHED_TARGET_PAYMENT"
    elif resp_payment and expected_payment_id and resp_payment != expected_payment_id:
        return False, "MISMATCHED_TARGET_PAYMENT"

    if expected_event_id and expected_command == "REQUEST_EVENT_REDELIVERY":
        if not resp_event or resp_event != expected_event_id:
            return False, "MISSING_OR_MISMATCHED_TARGET_EVENT"
    elif resp_event and expected_event_id and resp_event != expected_event_id:
        return False, "MISMATCHED_TARGET_EVENT"

    all_supported = (
        SUPPORTED_EXECUTED_STATUSES | SUPPORTED_REJECTED_STATUSES | SUPPORTED_ESCALATED_STATUSES
    )
    if not resp_status or resp_status not in all_supported:
        return False, f"UNKNOWN_OR_MISSING_EXECUTION_STATUS:{resp_status or 'EMPTY'}"

    return True, resp_status


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

    # 1b (Prompt 14.8 + 14.9 Fix P1): Resolve canonical business identity (order -> payment ->
    # event) BEFORE acquiring the PostgreSQL advisory lock, and block conflicting commands across
    # ANY combination of order/payment/event references targeting the same business resource!
    from sqlalchemy import and_, or_
    from sqlalchemy import text as sa_text

    from app.models.supervisor import SupervisorState

    resolved_order_id: str | None = (
        obs.affected_order_id
        or (
            str(fresh_order.get("id"))
            if isinstance(fresh_order, dict) and fresh_order.get("id")
            else None
        )
        or (
            str(fresh_payment.get("order_id"))
            if isinstance(fresh_payment, dict) and fresh_payment.get("order_id")
            else None
        )
    )
    resolved_payment_id: str | None = obs.affected_payment_id or (
        str(fresh_payment.get("id"))
        if isinstance(fresh_payment, dict) and fresh_payment.get("id")
        else None
    )
    resolved_event_id: str | None = str(target_event_id) if target_event_id else None

    canonical_target = resolved_order_id or resolved_payment_id or resolved_event_id or obs.id
    target_lock_key = (
        f"biz:{binding.merchant_id}:{binding.commerce_session_id}:"
        f"{binding.commerce_world_epoch}:{canonical_target}"
    )
    db.execute(sa_text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": target_lock_key})

    # Re-verify controller binding authority & dashboard world_epoch inside the locked boundary
    fresh_binding = db.scalar(
        select(CommerceSourceBinding).where(CommerceSourceBinding.id == binding.id)
    )
    if fresh_binding is not None and not fresh_binding.is_enabled:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "CONTROLLER_OWNERSHIP_REVOKED: binding is no longer active for mutation dispatch",
            status_code=403,
        )
    sup_state_now = db.scalar(
        select(SupervisorState).where(SupervisorState.session_id == binding.session_id)
    )
    if sup_state_now is not None and sup_state_now.world_epoch != obs.world_epoch:
        raise AppError(
            ErrorCode.CONFLICT,
            "STALE_WORLD_EPOCH_AT_DISPATCH: dashboard world epoch changed before dispatch",
            status_code=409,
        )

    target_match_clauses: list[Any] = []
    if resolved_order_id:
        target_match_clauses.append(RecoveryCommand.target_order_id == resolved_order_id)
    if resolved_payment_id:
        target_match_clauses.append(RecoveryCommand.target_payment_id == resolved_payment_id)
    if resolved_event_id:
        target_match_clauses.append(RecoveryCommand.target_event_id == resolved_event_id)
    if not target_match_clauses:
        target_match_clauses.append(RecoveryCommand.anomaly_id == obs.id)

    conflict_scope: list[Any] = [
        RecoveryCommand.merchant_id == binding.merchant_id,
        RecoveryCommand.commerce_session_id == binding.commerce_session_id,
        RecoveryCommand.world_epoch == binding.commerce_world_epoch,
        or_(*target_match_clauses),
        or_(
            RecoveryCommand.state.in_(["EXECUTING", "UNKNOWN_OUTCOME", "VERIFYING"]),
            and_(
                RecoveryCommand.state == "MANUAL_REVIEW",
                RecoveryCommand.execution_certainty.in_(["UNKNOWN", "COMMAND_EXECUTION_UNKNOWN"]),
                RecoveryCommand.uncertainty_resolved_at.is_(None),
            ),
        ),
    ]
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
        # Prompt 14.5 Fix 1: ANY transport/timeout/reset/malformed-response failure after
        # possible dispatch MUST remain UNKNOWN_OUTCOME + COMMAND_EXECUTION_UNKNOWN!
        # NEVER mark FAILED or release uncertainty guards on ambiguous transport errors!
        cmd_row.state = "UNKNOWN_OUTCOME"
        cmd_row.execution_certainty = "COMMAND_EXECUTION_UNKNOWN"
        cmd_row.http_status = exc.status_code
        cmd_row.response_json = {
            "error": str(exc),
            "outcome_classification": "COMMAND_EXECUTION_UNKNOWN",
        }
        cmd_row.updated_at = datetime.now(UTC)
        verdict = RecoveryVerdict.UNKNOWN_OUTCOME.value
        obs.verification_verdict = verdict
        if obs.linked_case_id:
            db.add(
                CaseEvent(
                    id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
                    case_id=obs.linked_case_id,
                    session_id=obs.session_id,
                    type="COMMERCE_RECOVERY_OUTCOME_UNKNOWN",
                    payload_json={
                        "command_id": cmd_row.id,
                        "command": norm_cmd,
                        "idempotency_key": idempotency_key,
                        "execution_certainty": "COMMAND_EXECUTION_UNKNOWN",
                        "error": str(exc),
                    },
                    created_at=datetime.now(UTC),
                )
            )
        db.commit()
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=norm_cmd,
            executed=False,
            http_status=exc.status_code,
            verified_recovery=False,
            verdict=verdict,
            recovery_source=None,
            detail=f"Ambiguous post-dispatch transport outcome (guard retained): {exc}",
            response_payload={},
        )

    cmd_row.http_status = http_status
    cmd_row.response_json = resp_data if isinstance(resp_data, dict) else {"raw": str(resp_data)}
    cmd_row.updated_at = datetime.now(UTC)

    # 1. Ambiguous 5xx / unexpected status codes -> UNKNOWN_OUTCOME (retain guard!)
    if http_status >= 500 or http_status not in (200, 400, 401, 403, 404, 409, 422):
        cmd_row.state = "UNKNOWN_OUTCOME"
        cmd_row.execution_certainty = "COMMAND_EXECUTION_UNKNOWN"
        obs.verification_verdict = RecoveryVerdict.UNKNOWN_OUTCOME.value
        if obs.linked_case_id:
            db.add(
                CaseEvent(
                    id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
                    case_id=obs.linked_case_id,
                    session_id=obs.session_id,
                    type="COMMERCE_RECOVERY_OUTCOME_UNKNOWN",
                    payload_json={
                        "command_id": cmd_row.id,
                        "command": norm_cmd,
                        "http_status": http_status,
                        "idempotency_key": idempotency_key,
                        "execution_certainty": "COMMAND_EXECUTION_UNKNOWN",
                    },
                    created_at=datetime.now(UTC),
                )
            )
        db.commit()
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=norm_cmd,
            executed=False,
            http_status=http_status,
            verified_recovery=False,
            verdict=RecoveryVerdict.UNKNOWN_OUTCOME.value,
            recovery_source=None,
            detail=f"Ambiguous receiver HTTP {http_status}; outcome uncertain (guard retained)",
            response_payload=cmd_row.response_json,
        )

    # 2. HTTP 409 Conflict: Distinguish confirmed pre-mutation rejection vs uncertain/in-flight
    if http_status == 409:
        conflict_code = _extract_conflict_reason_code(cmd_row.response_json)
        if conflict_code not in CONFIRMED_409_REJECTION_CODES:
            # Uncertain / in-progress / undocumented 409 -> preserve UNKNOWN_OUTCOME + guard!
            cmd_row.state = "UNKNOWN_OUTCOME"
            cmd_row.execution_certainty = "COMMAND_EXECUTION_UNKNOWN"
            obs.verification_verdict = RecoveryVerdict.UNKNOWN_OUTCOME.value
            if obs.linked_case_id:
                db.add(
                    CaseEvent(
                        id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
                        case_id=obs.linked_case_id,
                        session_id=obs.session_id,
                        type="COMMERCE_RECOVERY_OUTCOME_UNKNOWN",
                        payload_json={
                            "command_id": cmd_row.id,
                            "command": norm_cmd,
                            "http_status": 409,
                            "conflict_reason_code": conflict_code or "UNDOCUMENTED_CONFLICT",
                            "idempotency_key": idempotency_key,
                            "execution_certainty": "COMMAND_EXECUTION_UNKNOWN",
                            "response": cmd_row.response_json,
                        },
                        created_at=datetime.now(UTC),
                    )
                )
            db.commit()
            return RecoveryExecutionResult(
                command_id=cmd_row.id,
                command=norm_cmd,
                executed=False,
                http_status=409,
                verified_recovery=False,
                verdict=RecoveryVerdict.UNKNOWN_OUTCOME.value,
                recovery_source=None,
                detail=(
                    f"Ambiguous/in-progress HTTP 409 conflict "
                    f"({conflict_code or 'UNDOCUMENTED_CONFLICT'}); preserving UNKNOWN_OUTCOME"
                ),
                response_payload=cmd_row.response_json,
            )

    # 3. Definitive pre-commit client rejection (4xx or confirmed pre-mutation 409)
    if http_status != 200:
        cmd_row.state = "FAILED"
        cmd_row.execution_certainty = "CONFIRMED_REJECTED"
        if obs.linked_case_id:
            db.add(
                CaseEvent(
                    id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
                    case_id=obs.linked_case_id,
                    session_id=obs.session_id,
                    type="COMMERCE_RECOVERY_COMMAND_REJECTED",
                    payload_json={
                        "command_id": cmd_row.id,
                        "command": norm_cmd,
                        "http_status": http_status,
                        "idempotency_key": idempotency_key,
                        "execution_certainty": "CONFIRMED_REJECTED",
                        "response": cmd_row.response_json,
                    },
                    created_at=datetime.now(UTC),
                )
            )
        db.commit()
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=norm_cmd,
            executed=False,
            http_status=http_status,
            verified_recovery=False,
            verdict=RecoveryVerdict.STILL_INCONSISTENT.value,
            recovery_source=None,
            detail=f"Commerce recovery command definitively rejected (HTTP {http_status})",
            response_payload=cmd_row.response_json,
        )

    # 4. HTTP 200: Strictly require complete receiver identity + supported semantic status
    identity_ok, identity_detail = _validate_receiver_http200_identity(
        resp_json=cmd_row.response_json,
        expected_idempotency_key=idempotency_key,
        expected_command=norm_cmd,
        expected_request_hash=req_hash,
        expected_merchant_id=binding.merchant_id,
        expected_commerce_session_id=binding.commerce_session_id,
        expected_world_epoch=binding.commerce_world_epoch,
        expected_order_id=obs.affected_order_id,
        expected_payment_id=obs.affected_payment_id,
        expected_event_id=str(target_event_id) if target_event_id else None,
    )
    if not identity_ok:
        cmd_row.state = "UNKNOWN_OUTCOME"
        cmd_row.execution_certainty = "COMMAND_EXECUTION_UNKNOWN"
        obs.verification_verdict = RecoveryVerdict.UNKNOWN_OUTCOME.value
        if obs.linked_case_id:
            db.add(
                CaseEvent(
                    id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
                    case_id=obs.linked_case_id,
                    session_id=obs.session_id,
                    type="COMMERCE_RECOVERY_OUTCOME_UNKNOWN",
                    payload_json={
                        "command_id": cmd_row.id,
                        "command": norm_cmd,
                        "http_status": 200,
                        "reason": identity_detail,
                        "response": cmd_row.response_json,
                    },
                    created_at=datetime.now(UTC),
                )
            )
        db.commit()
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=norm_cmd,
            executed=False,
            http_status=200,
            verified_recovery=False,
            verdict=RecoveryVerdict.UNKNOWN_OUTCOME.value,
            recovery_source=None,
            detail=f"{identity_detail} on HTTP 200; preserving UNKNOWN_OUTCOME",
            response_payload=cmd_row.response_json,
        )

    resp_semantic = identity_detail

    if resp_semantic in SUPPORTED_REJECTED_STATUSES:
        cmd_row.state = "FAILED"
        cmd_row.execution_certainty = "CONFIRMED_REJECTED"
        if obs.linked_case_id:
            db.add(
                CaseEvent(
                    id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
                    case_id=obs.linked_case_id,
                    session_id=obs.session_id,
                    type="COMMERCE_RECOVERY_COMMAND_REJECTED",
                    payload_json={
                        "command_id": cmd_row.id,
                        "command": norm_cmd,
                        "http_status": 200,
                        "semantic_status": resp_semantic,
                        "response": cmd_row.response_json,
                    },
                    created_at=datetime.now(UTC),
                )
            )
        db.commit()
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=norm_cmd,
            executed=False,
            http_status=200,
            verified_recovery=False,
            verdict=RecoveryVerdict.STILL_INCONSISTENT.value,
            recovery_source=None,
            detail=f"HTTP 200 carried unsuccessful semantic status '{resp_semantic}'",
            response_payload=cmd_row.response_json,
        )

    if resp_semantic in SUPPORTED_ESCALATED_STATUSES:
        cmd_row.state = "MANUAL_REVIEW"
        cmd_row.execution_certainty = "CONFIRMED_ESCALATED"
        if obs.linked_case_id:
            db.add(
                CaseEvent(
                    id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
                    case_id=obs.linked_case_id,
                    session_id=obs.session_id,
                    type="COMMERCE_RECOVERY_ESCALATED",
                    payload_json={
                        "command_id": cmd_row.id,
                        "command": norm_cmd,
                        "http_status": 200,
                        "response": cmd_row.response_json,
                    },
                    created_at=datetime.now(UTC),
                )
            )
        db.commit()
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=norm_cmd,
            executed=False,
            http_status=200,
            verified_recovery=False,
            verdict=RecoveryVerdict.STILL_INCONSISTENT.value,
            recovery_source=None,
            detail="Receiver escalated command to manual review without mutation",
            response_payload=cmd_row.response_json,
        )

    cmd_row.state = "VERIFYING"
    cmd_row.execution_certainty = "COMMAND_EXECUTION_VERIFIED"
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
                    "http_status": 200,
                    "idempotency_key": idempotency_key,
                    "execution_certainty": "COMMAND_EXECUTION_VERIFIED",
                    "response": cmd_row.response_json,
                },
                created_at=datetime.now(UTC),
            )
        )
    db.commit()

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
    # Prompt 14.7 Fix 2: Validate the same complete identity envelope during status reconciliation!
    recon_ok, recon_detail = _validate_receiver_http200_identity(
        resp_json=status_data if isinstance(status_data, dict) else {},
        expected_idempotency_key=cmd_row.idempotency_key,
        expected_command=cmd_row.command.upper(),
        expected_request_hash=cmd_row.request_hash,
        expected_merchant_id=cmd_row.merchant_id,
        expected_commerce_session_id=cmd_row.commerce_session_id,
        expected_world_epoch=cmd_row.world_epoch,
        expected_order_id=cmd_row.target_order_id,
        expected_payment_id=cmd_row.target_payment_id,
        expected_event_id=cmd_row.target_event_id,
    )
    if not recon_ok:
        return RecoveryExecutionResult(
            command_id=cmd_row.id,
            command=cmd_row.command,
            executed=False,
            http_status=status_code,
            verified_recovery=False,
            verdict=RecoveryVerdict.UNKNOWN_OUTCOME.value,
            recovery_source=None,
            detail=(
                f"Receiver status lookup failed strict identity check ({recon_detail}); "
                "refusing reconciliation and preserving UNKNOWN_OUTCOME"
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


AUTHORIZED_UNCERTAINTY_RESOLVER_ROLES: frozenset[str] = frozenset(
    {"DEMO_MANAGER", "INCIDENT_ADMIN", "OPERATOR_ADMIN"}
)


def resolve_uncertain_recovery_command(
    db: Session,
    *,
    command_id: str,
    session_id: str,
    resolved_by: str,
    operator_role: str = "DEMO_MANAGER",
    supporting_evidence_ref: str = "",
    resolution_note: str = "Explicitly resolved after operator verification",
) -> RecoveryCommand:
    """Explicitly release an unresolved uncertainty guard after authorized operator review.

    Requires authenticated operator identity, authorized role, non-empty supporting evidence
    reference, and records an immutable audit event on the linked Case.
    """
    norm_role = operator_role.strip().upper()
    if norm_role not in AUTHORIZED_UNCERTAINTY_RESOLVER_ROLES or not resolved_by.strip():
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"UNAUTHORIZED_UNCERTAINTY_RESOLUTION: role '{operator_role}' is not authorized "
            "to release an uncertain recovery command guard",
            status_code=403,
        )
    if not supporting_evidence_ref.strip():
        raise AppError(
            ErrorCode.BAD_REQUEST,
            "MISSING_RESOLUTION_EVIDENCE: supporting_evidence_ref is required to release "
            "an uncertain recovery command guard",
            status_code=400,
        )

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
    row.uncertainty_resolved_by = f"{norm_role}:{resolved_by.strip()}"
    verif = dict(row.verification_json or {})
    verif["manual_resolution_note"] = resolution_note
    verif["manual_resolved_by"] = row.uncertainty_resolved_by
    verif["manual_resolution_evidence_ref"] = supporting_evidence_ref.strip()
    verif["manual_resolved_at"] = now.isoformat()
    row.verification_json = verif
    row.updated_at = now
    if row.case_id:
        db.add(
            CaseEvent(
                id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
                case_id=row.case_id,
                session_id=session_id,
                type="UNCERTAIN_COMMAND_MANUALLY_RESOLVED",
                payload_json={
                    "command_id": row.id,
                    "resolved_by": row.uncertainty_resolved_by,
                    "supporting_evidence_ref": supporting_evidence_ref.strip(),
                    "resolution_note": resolution_note,
                },
                created_at=now,
            )
        )
    db.commit()
    db.refresh(row)
    return row
