"""Case routes: intake, detail, events timeline, and status polling."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.errors import AppError, ErrorCode
from app.core.session import get_current_session
from app.db.session import get_db
from app.models.cases import Case, CaseEvent, Evidence
from app.models.enums import CaseState, EvidenceKind
from app.models.session import DemoSession
from app.schemas.cases import (
    CaseEventResponse,
    CaseResponse,
    CaseStatusResponse,
    CreateCaseRequest,
    EvidenceResponse,
)

router = APIRouter(prefix="/cases", tags=["cases"])


def _compute_payload_fingerprint(payload: CreateCaseRequest) -> str:
    canonical_repr = f"{payload.type}|{payload.goal or ''}|{payload.text.strip()}"
    return hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()


@router.post(
    "",
    response_model=CaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a Case from Vietnamese free-text intake (treated strictly as CLAIM)",
)
def create_case(
    payload: CreateCaseRequest,
    response: Response,
    idempotency_header: str | None = Header(default=None, alias="X-Idempotency-Key"),
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> CaseResponse:
    idempotency_key = idempotency_header or payload.idempotency_key
    payload_fingerprint = _compute_payload_fingerprint(payload)

    # Strengthened Idempotency check:
    # Same key + same payload -> 200 Replay
    # Same key + different payload -> 409 Conflict
    if idempotency_key:
        stmt = (
            select(Case)
            .options(selectinload(Case.evidence))
            .where(
                Case.session_id == current_session.id,
                Case.idempotency_key == idempotency_key,
            )
        )
        existing = db.scalar(stmt)
        if existing is not None:
            if (
                existing.idempotency_payload_hash
                and existing.idempotency_payload_hash != payload_fingerprint
            ):
                raise AppError(
                    ErrorCode.CONFLICT,
                    f"Idempotency key '{idempotency_key}' was previously used "
                    f"with a different request payload",
                    status_code=409,
                    details={"idempotency_key": idempotency_key},
                )
            # Replay existing case without re-incrementing quota or duplicating records
            response.status_code = status.HTTP_200_OK
            return _to_case_response(existing)

    # Acquire row-level lock on DemoSession to prevent race conditions on quota under concurrency
    db.refresh(current_session, with_for_update=True)
    session_obj = current_session

    if session_obj.case_count >= session_obj.max_cases:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            f"Session quota exceeded: maximum of {session_obj.max_cases} cases reached",
            status_code=429,
            details={
                "max_cases": session_obj.max_cases,
                "case_count": session_obj.case_count,
            },
        )

    now = datetime.now(UTC)
    case_id = f"CASE-{uuid.uuid4().hex[:10].upper()}"
    goal = payload.goal or f"Điều tra và xử lý khiếu nại: {payload.text[:80]}"

    new_case = Case(
        id=case_id,
        session_id=session_obj.id,
        type=payload.type,
        goal=goal,
        state=CaseState.NEW.value,
        version=1,
        owner="guest",
        idempotency_key=idempotency_key,
        idempotency_payload_hash=payload_fingerprint if idempotency_key else None,
        created_at=now,
        updated_at=now,
    )
    db.add(new_case)

    # Hard Domain Invariant 1:
    # User message is strictly an UNTRUSTED CLAIM; never authenticated payment proof.
    initial_evidence = Evidence(
        id=f"EVD-{uuid.uuid4().hex[:10].upper()}",
        case_id=case_id,
        session_id=session_obj.id,
        kind=EvidenceKind.CLAIM.value,
        source="CUSTOMER_MESSAGE",
        source_ref="intake_text",
        payload_json={"text": payload.text, "channel": "WEB_INTAKE"},
        confidence=None,
        observed_at=now,
    )
    db.add(initial_evidence)

    # Initial append-only audit event
    initial_event = CaseEvent(
        id=f"EVT-{uuid.uuid4().hex[:10].upper()}",
        case_id=case_id,
        session_id=session_obj.id,
        type="CASE_CREATED",
        payload_json={
            "initial_state": CaseState.NEW.value,
            "intake_length": len(payload.text),
            "evidence_id": initial_evidence.id,
        },
        created_at=now,
    )
    db.add(initial_event)

    try:
        session_obj.case_count += 1
        session_obj.updated_at = now
        db.commit()
        db.refresh(new_case)
        return _to_case_response(new_case, [initial_evidence])
    except IntegrityError:
        db.rollback()
        # Atomic recovery under concurrent retries with the same idempotency key
        if idempotency_key:
            stmt = (
                select(Case)
                .options(selectinload(Case.evidence))
                .where(
                    Case.session_id == current_session.id,
                    Case.idempotency_key == idempotency_key,
                )
            )
            existing = db.scalar(stmt)
            if existing is not None:
                if (
                    existing.idempotency_payload_hash
                    and existing.idempotency_payload_hash != payload_fingerprint
                ):
                    raise AppError(
                        ErrorCode.CONFLICT,
                        f"Idempotency key '{idempotency_key}' was previously used "
                        f"with a different request payload",
                        status_code=409,
                        details={"idempotency_key": idempotency_key},
                    ) from None
                response.status_code = status.HTTP_200_OK
                return _to_case_response(existing)
        raise


@router.get(
    "/{case_id}",
    response_model=CaseResponse,
    summary="Get Case detail and evidence (scoped to session)",
)
def get_case(
    case_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> CaseResponse:
    stmt = (
        select(Case)
        .options(selectinload(Case.evidence))
        .where(Case.id == case_id, Case.session_id == current_session.id)
    )
    case_obj = db.scalar(stmt)
    if case_obj is None:
        # QA T18: Prevent data leakage / ID probing across guest sessions
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Case '{case_id}' not found",
            status_code=404,
        )

    return _to_case_response(case_obj)


@router.get(
    "/{case_id}/events",
    response_model=list[CaseEventResponse],
    summary="Get Case decision trace / audit timeline (scoped to session)",
)
def get_case_events(
    case_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> list[CaseEventResponse]:
    stmt_case = select(Case.id).where(Case.id == case_id, Case.session_id == current_session.id)
    if db.scalar(stmt_case) is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Case '{case_id}' not found", status_code=404)

    stmt = (
        select(CaseEvent)
        .where(CaseEvent.case_id == case_id, CaseEvent.session_id == current_session.id)
        .order_by(CaseEvent.created_at.asc())
    )
    events = db.scalars(stmt).all()
    return [
        CaseEventResponse(
            id=e.id,
            case_id=e.case_id,
            type=e.type,
            payload_json=e.payload_json,
            created_at=e.created_at,
        )
        for e in events
    ]


@router.get(
    "/{case_id}/status",
    response_model=CaseStatusResponse,
    summary="Get lightweight Case status and version",
)
def get_case_status(
    case_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> CaseStatusResponse:
    stmt_case = select(Case).where(Case.id == case_id, Case.session_id == current_session.id)
    case_obj = db.scalar(stmt_case)
    if case_obj is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Case '{case_id}' not found", status_code=404)

    return CaseStatusResponse(
        case_id=case_obj.id,
        state=case_obj.state,
        version=case_obj.version,
        updated_at=case_obj.updated_at,
    )


def _to_case_response(
    case_obj: Case, fallback_evidence: list[Evidence] | None = None
) -> CaseResponse:
    evidence_list = case_obj.evidence if case_obj.evidence else (fallback_evidence or [])
    return CaseResponse(
        id=case_obj.id,
        session_id=case_obj.session_id,
        type=case_obj.type,
        goal=case_obj.goal,
        state=case_obj.state,
        version=case_obj.version,
        owner=case_obj.owner,
        idempotency_key=case_obj.idempotency_key,
        created_at=case_obj.created_at,
        updated_at=case_obj.updated_at,
        evidence=[
            EvidenceResponse(
                id=ev.id,
                case_id=ev.case_id,
                kind=ev.kind,
                source=ev.source,
                source_ref=ev.source_ref,
                payload_json=ev.payload_json,
                confidence=ev.confidence,
                observed_at=ev.observed_at,
            )
            for ev in evidence_list
        ],
    )
