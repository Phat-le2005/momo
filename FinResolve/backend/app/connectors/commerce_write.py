"""Policy-Gated Commerce Write Connector & Post-Action Verifier (Prompt 13 Part C).

Security & Safety Invariants:
- Uses dedicated `X-Commerce-Write-Key` separate from read credentials.
- Validates destination URL with `validate_approved_commerce_url` and blocks redirects.
- Never trusts HTTP 200 alone as proof of recovery: ALWAYS re-reads authoritative Commerce
  state through `CommerceReadConnector` + `evaluate_recovery_postconditions`!
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.connectors.commerce import (
    CommerceConnectorError,
    CommerceReadConnector,
    validate_approved_commerce_url,
)
from app.core.config import get_settings
from app.models.cases import CaseEvent
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.monitoring.service import (
    RecoveryVerdict,
    build_connector_for_binding,
    verify_anomaly_recovery,
)

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
    command: str
    executed: bool
    http_status: int | None
    verified_recovery: bool
    verdict: str
    recovery_source: str | None
    detail: str
    response_payload: dict[str, Any]


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
        url = f"{self.base_url}/api/v1/service/recovery/execute"
        headers = {
            "X-Commerce-Write-Key": self._write_key,
            "X-Merchant-Id": self.merchant_id,
            "X-Commerce-Session-Id": self.commerce_session_id,
            "X-World-Epoch": str(self.world_epoch),
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


def execute_and_verify_commerce_recovery(
    db: Session,
    *,
    binding: CommerceSourceBinding,
    obs: AnomalyObservation,
    command: str,
    idempotency_key: str,
    expected_version: int | None = None,
    write_key: str | None = None,
    read_key: str | None = None,
    transport: httpx.BaseTransport | None = None,
    read_connector: CommerceReadConnector | None = None,
) -> RecoveryExecutionResult:
    """Execute a policy-gated LOCAL_MOCK recovery command AND independently verify postconditions.

    NEVER trusts HTTP 200 alone — always re-reads authoritative MiniStore state afterward!
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
        http_status, resp_data = writer.execute_command(
            command=command,
            idempotency_key=idempotency_key,
            order_id=obs.affected_order_id,
            payment_id=obs.affected_payment_id,
            event_id=(obs.affected_entities_json or {}).get("event_id"),
            expected_version=expected_version,
        )
    except CommerceConnectorError as exc:
        verdict = (
            RecoveryVerdict.UNKNOWN_OUTCOME.value
            if "timed out" in str(exc).lower()
            else RecoveryVerdict.SOURCE_UNAVAILABLE.value
        )
        obs.verification_verdict = verdict
        db.commit()
        return RecoveryExecutionResult(
            command=command,
            executed=False,
            http_status=exc.status_code,
            verified_recovery=False,
            verdict=verdict,
            recovery_source=None,
            detail=str(exc),
            response_payload={},
        )

    if obs.linked_case_id:
        db.add(
            CaseEvent(
                id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
                case_id=obs.linked_case_id,
                session_id=obs.session_id,
                type="COMMERCE_RECOVERY_COMMAND_EXECUTED",
                payload_json={
                    "command": command,
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
            command=command,
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
    return RecoveryExecutionResult(
        command=command,
        executed=True,
        http_status=http_status,
        verified_recovery=recovered,
        verdict=obs.verification_verdict or RecoveryVerdict.STILL_INCONSISTENT.value,
        recovery_source=obs.recovery_source,
        detail=detail,
        response_payload=resp_data,
    )
