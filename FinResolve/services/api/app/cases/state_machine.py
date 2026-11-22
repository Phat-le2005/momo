"""Case state machine with deterministic transitions and mandatory verification for RESOLVED."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.models.actions import Verification
from app.models.cases import Case, CaseEvent
from app.models.enums import CaseState, VerificationVerdict

# Explicit, validated state transition graph.
# Terminal states: RESOLVED, FAILED.
ALLOWED_TRANSITIONS: dict[CaseState, set[CaseState]] = {
    CaseState.NEW: {
        CaseState.INVESTIGATING,
        CaseState.ESCALATED,
        CaseState.FAILED,
    },
    CaseState.INVESTIGATING: {
        CaseState.WAITING_EVIDENCE,
        CaseState.WAITING_APPROVAL,
        CaseState.EXECUTING,
        CaseState.VERIFYING,
        CaseState.ESCALATED,
        CaseState.FAILED,
        CaseState.UNKNOWN_OUTCOME,
    },
    CaseState.WAITING_EVIDENCE: {
        CaseState.INVESTIGATING,
        CaseState.ESCALATED,
        CaseState.FAILED,
        CaseState.UNKNOWN_OUTCOME,
    },
    CaseState.WAITING_APPROVAL: {
        CaseState.EXECUTING,
        CaseState.INVESTIGATING,
        CaseState.ESCALATED,
        CaseState.FAILED,
    },
    CaseState.EXECUTING: {
        CaseState.VERIFYING,
        CaseState.FAILED,
        CaseState.UNKNOWN_OUTCOME,
    },
    CaseState.VERIFYING: {
        CaseState.RESOLVED,
        CaseState.INVESTIGATING,
        CaseState.FAILED,
        CaseState.UNKNOWN_OUTCOME,
    },
    CaseState.RESOLVED: set(),
    CaseState.ESCALATED: {
        CaseState.INVESTIGATING,
        CaseState.FAILED,
    },
    CaseState.FAILED: set(),
    CaseState.UNKNOWN_OUTCOME: {
        CaseState.INVESTIGATING,
        CaseState.ESCALATED,
        CaseState.FAILED,
    },
}


def can_transition(current: CaseState, target: CaseState) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, set())


def transition_case(
    db: Session,
    case: Case,
    target_state: CaseState,
    *,
    reason: str,
    expected_version: int | None = None,
    verification: Verification | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> Case:
    """Transition a Case to target_state with database-level concurrency control and validation.

    Hard Domain Invariants:
    1. Database-Level Concurrency Control: Acquires exclusive row lock (SELECT FOR UPDATE)
       on the Case row to serialize concurrent transitions.
    2. Optimistic Concurrency: If expected_version is provided, locked case.version must match.
    3. Verification Relevance: No transition to RESOLVED is allowed without a fresh relevant
       authoritative Verification record where verdict == PASSED and case_version == case.version.
       A stale verification from an earlier version cannot authorize RESOLVED.
    """
    # 1. Acquire DB-level exclusive row lock to serialize concurrent transitions
    stmt_lock = select(Case).where(Case.id == case.id).with_for_update()
    locked_case = db.scalar(stmt_lock)
    if locked_case is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Case '{case.id}' not found", status_code=404)
    case = locked_case

    try:
        current_enum = CaseState(case.state)
    except ValueError as err:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            f"Case has corrupt state value: {case.state}",
            status_code=500,
        ) from err

    # 2. Optimistic concurrency check under DB row lock
    if expected_version is not None and case.version != expected_version:
        raise AppError(
            ErrorCode.CONFLICT,
            f"Optimistic concurrency conflict: expected case version {expected_version}, "
            f"but current version is {case.version}",
            status_code=409,
            details={
                "case_id": case.id,
                "current_version": case.version,
                "expected_version": expected_version,
            },
        )

    if not can_transition(current_enum, target_state):
        raise AppError(
            ErrorCode.CONFLICT,
            f"Invalid state transition from '{case.state}' to '{target_state.value}'",
            status_code=409,
            details={
                "current_state": case.state,
                "target_state": target_state.value,
                "allowed_targets": [s.value for s in ALLOWED_TRANSITIONS.get(current_enum, set())],
            },
        )

    if target_state == CaseState.RESOLVED:
        # Require fresh, relevant PASSED verification tied to this exact case version
        valid_passed_verification = False
        if verification:
            if (
                verification.case_id == case.id
                and verification.verdict == VerificationVerdict.PASSED.value
                and verification.case_version == case.version
            ):
                valid_passed_verification = True
        else:
            stmt = (
                select(Verification)
                .where(
                    Verification.case_id == case.id,
                    Verification.verdict == VerificationVerdict.PASSED.value,
                    Verification.case_version == case.version,
                )
                .order_by(Verification.checked_at.desc())
                .limit(1)
            )
            existing_fresh = db.scalar(stmt)
            if existing_fresh is not None:
                valid_passed_verification = True

        if not valid_passed_verification:
            raise AppError(
                ErrorCode.CONFLICT,
                f"Cannot mark Case as RESOLVED without a verified postcondition: "
                f"requires a fresh relevant PASSED verification for case version {case.version}",
                status_code=409,
                details={
                    "case_id": case.id,
                    "case_version": case.version,
                    "target_state": CaseState.RESOLVED.value,
                    "verification_present": verification is not None,
                },
            )

    old_state = case.state
    case.version += 1
    case.state = target_state.value

    event_payload = {
        "from_state": old_state,
        "to_state": target_state.value,
        "reason": reason,
        "version": case.version,
    }
    if extra_payload:
        event_payload.update(extra_payload)

    event = CaseEvent(
        id=f"EVT-{uuid.uuid4().hex[:12]}",
        case_id=case.id,
        session_id=case.session_id,
        type="STATE_TRANSITION",
        payload_json=event_payload,
    )
    db.add(event)
    db.flush()
    return case
