"""Case routes: intake, detail, events timeline, status polling, and bounded AI investigation."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, selectinload

from app.ai_core.model_adapter import get_model_adapter
from app.ai_core.runtime import (
    MAX_INVESTIGATIONS_PER_CASE,
    BoundedAgentRuntime,
    recover_stale_investigation,
)
from app.ai_core.schemas import InvestigationRunResult
from app.ai_core.understanding import analyze_vietnamese_complaint
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.rate_limit import enforce_ai_investigation_rate_limit
from app.core.session import get_current_session
from app.db.session import get_db, get_sessionmaker
from app.models.actions import ActionProposal
from app.models.cases import Case, CaseEvent, Evidence
from app.models.enums import ActionProposalStatus, CaseState, EvidenceKind
from app.models.session import DemoSession
from app.schemas.cases import (
    ActionProposalSummaryResponse,
    CaseEventResponse,
    CaseResponse,
    CaseStatusResponse,
    CreateCaseRequest,
    EvidenceResponse,
    InvestigateCaseRequest,
    InvestigationJobStatusResponse,
    RiskSignalSummaryResponse,
    VerificationSummaryResponse,
)

logger = logging.getLogger("finresolve.cases")

router = APIRouter(prefix="/cases", tags=["cases"])


def _compute_payload_fingerprint(payload: CreateCaseRequest) -> str:
    canonical_repr = f"{payload.type}|{payload.goal or ''}|{payload.text.strip()}"
    return hashlib.sha256(canonical_repr.encode("utf-8")).hexdigest()


def _execute_background_investigation(
    *,
    case_id: str,
    session_id: str,
    run_id: str,
    max_steps: int,
    simulate_timeout: bool,
) -> None:
    """Execute bounded AI investigation in an isolated background DB session."""
    maker = get_sessionmaker()
    with maker() as bg_db:
        try:
            runtime = BoundedAgentRuntime(bg_db, max_steps=max_steps)
            runtime.investigate_case(
                case_id=case_id,
                session_id=session_id,
                simulate_timeout=simulate_timeout,
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Background investigation run %s failed for case %s", run_id, case_id)
            bg_db.rollback()
            err_evt = CaseEvent(
                id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                case_id=case_id,
                session_id=session_id,
                type="INVESTIGATION_FAILED",
                payload_json={
                    "run_id": run_id,
                    "job_status": "FAILED",
                    "error": str(exc)[:300],
                },
                created_at=datetime.now(UTC),
            )
            bg_db.add(err_evt)
            bg_db.commit()


@router.get(
    "",
    response_model=list[CaseResponse],
    summary="List Case records in the authenticated demo session (Prompt 07 Case Center)",
)
def list_session_cases(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> list[CaseResponse]:
    """Tenant-scoped, read-only Case Center list; never accepts a caller session_id."""
    stmt = (
        select(Case)
        .options(
            selectinload(Case.evidence),
            selectinload(Case.action_proposals),
            selectinload(Case.risk_signals),
            selectinload(Case.verifications),
            selectinload(Case.events),
        )
        .where(Case.session_id == current_session.id)
        .order_by(Case.created_at.desc(), Case.id.desc())
        .limit(min(100, current_session.max_cases))
    )
    return [_to_case_response(item) for item in db.scalars(stmt).all()]


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
            .options(
                selectinload(Case.evidence),
                selectinload(Case.action_proposals),
                selectinload(Case.risk_signals),
                selectinload(Case.events),
            )
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
                .options(
                    selectinload(Case.evidence),
                    selectinload(Case.action_proposals),
                    selectinload(Case.risk_signals),
                    selectinload(Case.events),
                )
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


@router.post(
    "/{case_id}/investigate",
    response_model=InvestigationRunResult,
    status_code=status.HTTP_200_OK,
    summary="Run bounded agentic AI investigation on a Case (sync or async background job)",
)
def investigate_case(
    case_id: str,
    response: Response,
    background_tasks: BackgroundTasks,
    payload: InvestigateCaseRequest | None = None,
    _rate_limit: None = Depends(enforce_ai_investigation_rate_limit),
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> InvestigationRunResult:
    req = payload or InvestigateCaseRequest()

    if req.run_async:
        # Recover any stale crashed worker before checking overlapping investigation lock
        recover_stale_investigation(
            db,
            case_id=case_id,
            session_id=current_session.id,
        )
        # Validate Case ownership, non-overlapping lock, and rate limit before scheduling
        try:
            case_obj = db.scalar(
                select(Case)
                .options(selectinload(Case.evidence), selectinload(Case.events))
                .where(Case.id == case_id, Case.session_id == current_session.id)
                .with_for_update(nowait=True)
            )
        except OperationalError as err:
            db.rollback()
            raise AppError(
                ErrorCode.CONFLICT,
                f"OVERLAPPING_INVESTIGATION_BLOCKED: Another investigation is currently "
                f"running on Case '{case_id}'",
                status_code=409,
            ) from err

        if case_obj is None:
            raise AppError(ErrorCode.NOT_FOUND, f"Case '{case_id}' not found", status_code=404)

        active_run_id, inv_status = _extract_run_job_status(case_obj.events)
        if inv_status == "RUNNING" or case_obj.state == CaseState.INVESTIGATING.value:
            raise AppError(
                ErrorCode.CONFLICT,
                f"OVERLAPPING_INVESTIGATION_BLOCKED: Case '{case_obj.id}' already has an "
                f"active investigation in progress ('{active_run_id}')",
                status_code=409,
                details={"case_id": case_obj.id, "active_run_id": active_run_id},
            )

        existing_runs = (
            db.scalar(
                select(func.count(CaseEvent.id)).where(
                    CaseEvent.case_id == case_obj.id,
                    CaseEvent.session_id == current_session.id,
                    CaseEvent.type == "INVESTIGATION_STARTED",
                )
            )
            or 0
        )
        if existing_runs >= MAX_INVESTIGATIONS_PER_CASE:
            raise AppError(
                ErrorCode.RATE_LIMITED,
                f"Investigation quota exceeded for Case '{case_obj.id}' "
                f"(maximum {MAX_INVESTIGATIONS_PER_CASE} runs per case)",
                status_code=429,
            )

        run_id = f"RUN-{uuid.uuid4().hex[:10].upper()}"
        adapter = get_model_adapter(get_settings())
        queued_evt = CaseEvent(
            id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
            case_id=case_obj.id,
            session_id=current_session.id,
            type="INVESTIGATION_QUEUED",
            payload_json={
                "run_id": run_id,
                "job_status": "RUNNING",
                "model_mode": adapter.mode.value,
                "adapter_name": adapter.adapter_name,
                "max_steps": req.max_steps,
            },
            created_at=datetime.now(UTC),
        )
        db.add(queued_evt)
        db.commit()

        claim_text = case_obj.goal
        for ev in case_obj.evidence:
            if ev.kind == EvidenceKind.CLAIM.value:
                claim_text = str(ev.payload_json.get("text") or case_obj.goal)
                break
        understanding = analyze_vietnamese_complaint(claim_text, case_type=case_obj.type)

        background_tasks.add_task(
            _execute_background_investigation,
            case_id=case_obj.id,
            session_id=current_session.id,
            run_id=run_id,
            max_steps=req.max_steps,
            simulate_timeout=req.simulate_timeout,
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return InvestigationRunResult(
            run_id=run_id,
            case_id=case_obj.id,
            session_id=current_session.id,
            final_state=case_obj.state,
            case_version=case_obj.version,
            model_mode=adapter.mode,
            adapter_name=adapter.adapter_name,
            is_fallback=(adapter.mode.value == "DEMO_RULES"),
            steps_executed=0,
            replans_used=0,
            total_tokens_used=0,
            stop_reason="ASYNC_JOB_QUEUED",
            decision_summary=f"Đã đưa phiên điều tra {run_id} vào hàng đợi xử lý nền.",
            known_evidence_refs=[e.id for e in case_obj.evidence],
            unknowns=[],
            hypotheses=[],
            understanding=understanding,
            step_traces=[],
            proposed_action_ids=[],
            risk_signal_ids=[],
        )

    runtime = BoundedAgentRuntime(db, max_steps=req.max_steps)
    return runtime.investigate_case(
        case_id=case_id,
        session_id=current_session.id,
        simulate_timeout=req.simulate_timeout,
    )


@router.get(
    "/{case_id}/investigations/{run_id}",
    response_model=InvestigationJobStatusResponse,
    summary="Poll the status and result of a synchronous or asynchronous investigation run",
)
def get_investigation_job_status(
    case_id: str,
    run_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> InvestigationJobStatusResponse:
    # Idempotently persist INVESTIGATION_INTERRUPTED & recover Case from INVESTIGATING if stale
    recover_stale_investigation(
        db,
        case_id=case_id,
        session_id=current_session.id,
        run_id=run_id,
    )
    case_obj = db.scalar(
        select(Case)
        .options(selectinload(Case.events))
        .where(Case.id == case_id, Case.session_id == current_session.id)
    )
    if case_obj is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Case '{case_id}' not found", status_code=404)

    run_events = sorted(
        [e for e in case_obj.events if str(e.payload_json.get("run_id") or "") == run_id],
        key=lambda e: (e.created_at, e.id),
    )
    if not run_events:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Investigation run '{run_id}' not found for Case '{case_id}'",
            status_code=404,
        )

    started_at = run_events[0].created_at
    completed_at: datetime | None = None
    job_status: Literal["RUNNING", "COMPLETED", "FAILED", "INTERRUPTED"] = "RUNNING"
    model_mode = get_settings().model_mode.value
    steps_executed = 0
    stop_reason: str | None = None
    decision_summary: str | None = None
    proposed_action_ids: list[str] = []
    risk_signal_ids: list[str] = []
    error_message: str | None = None

    for evt in run_events:
        if evt.payload_json.get("model_mode"):
            model_mode = str(evt.payload_json["model_mode"])
        if evt.type == "AI_PLAN_GENERATED":
            steps_executed = max(steps_executed, int(evt.payload_json.get("step") or 0))
            decision_summary = str(evt.payload_json.get("decision_summary") or "")
        elif evt.type == "INVESTIGATION_COMPLETED":
            job_status = "COMPLETED"
            completed_at = evt.created_at
            steps_executed = int(evt.payload_json.get("steps_executed") or steps_executed)
            stop_reason = str(evt.payload_json.get("stop_reason") or "")
            decision_summary = str(evt.payload_json.get("decision_summary") or "")
            proposed_action_ids = [
                str(x) for x in (evt.payload_json.get("proposed_action_ids") or [])
            ]
            risk_signal_ids = [str(x) for x in (evt.payload_json.get("risk_signal_ids") or [])]
        elif evt.type == "INVESTIGATION_FAILED":
            job_status = "FAILED"
            completed_at = evt.created_at
            stop_reason = str(evt.payload_json.get("reason") or "INVESTIGATION_FAILED")
            error_message = str(evt.payload_json.get("error") or "Investigation failed")
        elif evt.type == "INVESTIGATION_INTERRUPTED":
            job_status = "INTERRUPTED"
            completed_at = evt.created_at
            stop_reason = str(evt.payload_json.get("reason") or "INVESTIGATION_INTERRUPTED")
            error_message = str(
                evt.payload_json.get("error")
                or evt.payload_json.get("reason")
                or "Investigation interrupted before completion"
            )

    return InvestigationJobStatusResponse(
        run_id=run_id,
        case_id=case_obj.id,
        session_id=case_obj.session_id,
        job_status=job_status,
        case_state=case_obj.state,
        case_version=case_obj.version,
        model_mode=model_mode,
        steps_executed=steps_executed,
        stop_reason=stop_reason,
        decision_summary=decision_summary,
        proposed_action_ids=proposed_action_ids,
        risk_signal_ids=risk_signal_ids,
        error_message=error_message,
        started_at=started_at,
        completed_at=completed_at,
    )


@router.get(
    "/{case_id}",
    response_model=CaseResponse,
    summary="Get Case detail, evidence, proposals, and risk signals (scoped to session)",
)
def get_case(
    case_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> CaseResponse:
    stmt = (
        select(Case)
        .options(
            selectinload(Case.evidence),
            selectinload(Case.action_proposals),
            selectinload(Case.risk_signals),
            selectinload(Case.verifications),
            selectinload(Case.events),
        )
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
    summary="Get lightweight Case status, version, and active investigation metadata",
)
def get_case_status(
    case_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> CaseStatusResponse:
    # Idempotently recover any stale crashed worker run on status poll (short txn when stale)
    recover_stale_investigation(
        db,
        case_id=case_id,
        session_id=current_session.id,
    )
    stmt_case = (
        select(Case)
        .options(selectinload(Case.events), selectinload(Case.action_proposals))
        .where(Case.id == case_id, Case.session_id == current_session.id)
    )
    case_obj = db.scalar(stmt_case)
    if case_obj is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Case '{case_id}' not found", status_code=404)

    latest_summary, model_mode, _ = _extract_latest_investigation_meta(case_obj.events)
    active_run_id, inv_status = _extract_run_job_status(case_obj.events)
    pending_proposals = sum(
        1 for p in case_obj.action_proposals if p.status == ActionProposalStatus.PROPOSED.value
    )

    return CaseStatusResponse(
        case_id=case_obj.id,
        state=case_obj.state,
        version=case_obj.version,
        updated_at=case_obj.updated_at,
        model_mode=model_mode or get_settings().model_mode.value,
        latest_decision_summary=latest_summary,
        pending_proposals_count=pending_proposals,
        active_run_id=active_run_id,
        investigation_status=inv_status,
    )


def _extract_run_job_status(
    events: list[CaseEvent] | None,
) -> tuple[str | None, Literal["IDLE", "RUNNING", "COMPLETED", "FAILED", "INTERRUPTED"]]:
    if not events:
        return None, "IDLE"
    for evt in reversed(events):
        if evt.type == "INVESTIGATION_COMPLETED":
            return str(evt.payload_json.get("run_id") or ""), "COMPLETED"
        if evt.type == "INVESTIGATION_FAILED":
            return str(evt.payload_json.get("run_id") or ""), "FAILED"
        if evt.type == "INVESTIGATION_INTERRUPTED":
            return str(evt.payload_json.get("run_id") or ""), "INTERRUPTED"
        if evt.type in ("INVESTIGATION_STARTED", "INVESTIGATION_QUEUED"):
            return str(evt.payload_json.get("run_id") or ""), "RUNNING"
    return None, "IDLE"


def _extract_latest_investigation_meta(
    events: list[CaseEvent] | None,
) -> tuple[str | None, str | None, list[str]]:
    if not events:
        return None, None, []
    latest_summary: str | None = None
    model_mode: str | None = None
    unknowns: list[str] = []

    for evt in reversed(events):
        if evt.type in ("INVESTIGATION_COMPLETED", "AI_PLAN_GENERATED"):
            if latest_summary is None:
                latest_summary = evt.payload_json.get("decision_summary")
            if model_mode is None:
                model_mode = evt.payload_json.get("model_mode")
            if not unknowns and isinstance(evt.payload_json.get("unknowns"), list):
                unknowns = [str(u) for u in evt.payload_json["unknowns"]]
            if latest_summary and model_mode:
                break
    return latest_summary, model_mode, unknowns


def _to_case_response(
    case_obj: Case, fallback_evidence: list[Evidence] | None = None
) -> CaseResponse:
    evidence_list = case_obj.evidence if case_obj.evidence else (fallback_evidence or [])
    proposals_list: list[ActionProposal] = (
        list(case_obj.action_proposals) if case_obj.action_proposals else []
    )
    risks_list = list(case_obj.risk_signals) if case_obj.risk_signals else []
    verifications_list = list(case_obj.verifications) if case_obj.verifications else []
    events_list = list(case_obj.events) if case_obj.events else []

    latest_summary, model_mode, unknowns = _extract_latest_investigation_meta(events_list)

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
        action_proposals=[
            ActionProposalSummaryResponse(
                id=pr.id,
                case_id=pr.case_id,
                action_type=pr.action_type,
                params_json=pr.params_json,
                evidence_refs=pr.evidence_refs,
                expected_version=pr.expected_version,
                risk_level=pr.risk_level,
                status=pr.status,
                created_at=pr.created_at,
            )
            for pr in proposals_list
        ],
        risk_signals=[
            RiskSignalSummaryResponse(
                id=rk.id,
                case_id=rk.case_id,
                category=rk.category,
                level=rk.level,
                evidence_refs=rk.evidence_refs,
                response=rk.response,
                details_json=rk.details_json,
                created_at=rk.created_at,
            )
            for rk in risks_list
        ],
        verifications=[
            VerificationSummaryResponse(
                id=vf.id,
                case_id=vf.case_id,
                action_execution_id=vf.action_execution_id,
                case_version=vf.case_version,
                verdict=vf.verdict,
                postconditions_json=vf.postconditions_json,
                checked_at=vf.checked_at,
            )
            for vf in verifications_list
        ],
        unknowns=unknowns,
        latest_decision_summary=latest_summary,
        model_mode=model_mode,
    )
