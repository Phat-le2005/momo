"""Autonomous Supervisor, Event Ingestion, and Mission Control routes (Prompt 09.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.rate_limit import enforce_ai_investigation_rate_limit
from app.core.session import get_current_session
from app.db.session import get_db
from app.models.actions import ActionProposal, Verification
from app.models.cases import Case
from app.models.enums import (
    ActionProposalStatus,
    CaseState,
    SupervisorJobStatus,
    SupervisorStatus,
)
from app.models.session import DemoSession
from app.models.supervisor import (
    ChallengeRun,
    SupervisorActivity,
    SupervisorJob,
    SupervisorState,
)
from app.schemas.supervisor import (
    ApprovalInboxItemResponse,
    ChallengeCatalogItemResponse,
    ChallengeRunResponse,
    ExtractReceiptRequest,
    LaunchChallengeRequest,
    MissionControlOverviewResponse,
    RecentVerificationItemResponse,
    SupervisorActivityItemResponse,
    SupervisorJobSummaryResponse,
    SupervisorStatusResponse,
)
from app.supervisor.challenges import (
    cancel_challenge_run,
    extract_mock_receipt_claim,
    launch_challenge,
    list_challenge_catalog,
)
from app.supervisor.events import (
    IngestEventResponse,
    NormalizedBusinessEvent,
    ingest_normalized_business_event,
)
from app.supervisor.service import (
    ensure_supervisor_state,
    get_worker_health,
    pause_supervisor,
    recompute_supervisor_idle_status,
    request_job_cancellation,
    resume_supervisor,
)
from app.supervisor.worker import notify_supervisor_worker

router = APIRouter(tags=["supervisor", "challenges", "events"])

SUPERVISOR_STATUS_LABELS_VI: dict[str, str] = {
    "MONITORING": "Đang giám sát",
    "INVESTIGATING": "Đang điều tra",
    "WAITING_EVIDENCE": "Chờ bổ sung bằng chứng",
    "WAITING_APPROVAL": "Chờ con người phê duyệt",
    "PAUSED": "Tạm dừng",
    "DEGRADED": "Suy giảm / Worker ngoại tuyến",
    "STOPPED": "Đã dừng",
}

LLM_EXECUTION_LABELS_VI: dict[str, str] = {
    "LLM_API_CONFIGURED": "Đã cấu hình LLM API (chưa phát sinh lượt gọi trong phiên)",
    "LLM_CALL_STARTED": "Đang gọi mô hình LLM suy luận",
    "LLM_CALL_SUCCEEDED": "Đã gọi LLM API thành công",
    "LLM_CALL_FAILED": "Gọi LLM lỗi — đã chuyển sang DEMO_RULES an toàn",
    "DEMO_RULES_FALLBACK": "Chế độ luật xác định (DEMO_RULES — 0 LLM calls)",
    "BUDGET_EXHAUSTED": "Đã đạt giới hạn ngân sách LLM của phiên",
}

CHALLENGE_STATUS_LABELS_VI: dict[str, str] = {
    "QUEUED": "Đang chờ worker xử lý",
    "RUNNING": "AI đang điều tra",
    "COMPLETED": "Đã hoàn tất",
    "WAITING_APPROVAL": "Chờ con người phê duyệt",
    "BLOCKED_SAFELY": "Đã chặn an toàn",
    "CANCELLED": "Đã dừng",
    "FAILED": "Lỗi kỹ thuật",
}


def build_supervisor_status_response(
    db: Session, state: SupervisorState
) -> SupervisorStatusResponse:
    recompute_supervisor_idle_status(db, state)
    settings = get_settings()
    health = get_worker_health(db)
    worker_healthy = bool(health["worker_healthy"])

    effective_status = state.status
    if (
        state.autopilot_enabled
        and not worker_healthy
        and effective_status == SupervisorStatus.MONITORING.value
    ):
        # Never report MONITORING as healthy if no background worker heartbeat is fresh!
        effective_status = SupervisorStatus.DEGRADED.value

    queue_depth = (
        db.scalar(
            select(func.count(SupervisorJob.id)).where(
                SupervisorJob.session_id == state.session_id,
                SupervisorJob.world_epoch == state.world_epoch,
                SupervisorJob.status.in_(
                    [
                        SupervisorJobStatus.QUEUED.value,
                        SupervisorJobStatus.RUNNING.value,
                        SupervisorJobStatus.CANCELLING.value,
                    ]
                ),
            )
        )
        or 0
    )
    total_cases = (
        db.scalar(select(func.count(Case.id)).where(Case.session_id == state.session_id)) or 0
    )
    active_cases = (
        db.scalar(
            select(func.count(Case.id)).where(
                Case.session_id == state.session_id,
                Case.state.in_(
                    [
                        CaseState.NEW.value,
                        CaseState.INVESTIGATING.value,
                        CaseState.WAITING_APPROVAL.value,
                        CaseState.WAITING_EVIDENCE.value,
                    ]
                ),
            )
        )
        or 0
    )
    waiting_approvals = (
        db.scalar(
            select(func.count(ActionProposal.id)).where(
                ActionProposal.session_id == state.session_id,
                ActionProposal.status == ActionProposalStatus.PROPOSED.value,
            )
        )
        or 0
    )

    return SupervisorStatusResponse(
        session_id=state.session_id,
        status=effective_status,
        status_label_vi=SUPERVISOR_STATUS_LABELS_VI.get(effective_status, effective_status),
        autopilot_enabled=state.autopilot_enabled,
        worker_healthy=worker_healthy,
        active_workers_count=int(health["active_workers_count"]),
        latest_worker_heartbeat_at=health["latest_worker_heartbeat_at"],
        world_epoch=state.world_epoch,
        active_job_id=state.active_job_id,
        active_case_id=state.active_case_id,
        configured_model_mode=settings.model_mode.value,
        configured_provider=settings.model_provider.strip() or None,
        configured_model_name=settings.model_name.strip() or None,
        llm_execution_state=state.llm_execution_state,
        llm_execution_label_vi=LLM_EXECUTION_LABELS_VI.get(
            state.llm_execution_state, state.llm_execution_state
        ),
        last_llm_succeeded_at=state.last_llm_succeeded_at,
        last_llm_failed_at=state.last_llm_failed_at,
        last_llm_error=state.last_llm_error,
        llm_calls_count=state.llm_calls_count,
        llm_attempts_count=state.llm_attempts_count,
        llm_succeeded_count=state.llm_succeeded_count,
        llm_failed_count=state.llm_failed_count,
        llm_timeout_count=state.llm_timeout_count,
        llm_fallback_count=state.llm_fallback_count,
        llm_tokens_unknown_count=state.llm_tokens_unknown_count,
        llm_tokens_used=state.llm_tokens_used,
        llm_budget_calls=state.llm_budget_calls,
        llm_budget_tokens=state.llm_budget_tokens,
        queue_depth=int(queue_depth),
        active_cases_count=int(active_cases),
        total_cases_count=int(total_cases),
        waiting_approvals_count=int(waiting_approvals),
        jobs_processed_count=state.jobs_processed_count,
        monitoring_note_vi=(
            "Giám sát theo sự kiện bởi Worker nền độc lập — khi không có sự kiện mới, "
            "AI không gọi LLM và không tiêu tốn token."
            if worker_healthy
            else (
                "Cảnh báo: chưa ghi nhận nhịp tim từ Supervisor Worker nền. "
                "Hãy chạy `python -m app.supervisor.worker` để xử lý hàng đợi tự động."
            )
        ),
        updated_at=state.updated_at,
    )


def _to_challenge_response(ch: ChallengeRun) -> ChallengeRunResponse:
    return ChallengeRunResponse(
        id=ch.id,
        session_id=ch.session_id,
        world_epoch=ch.world_epoch,
        challenge_type=ch.challenge_type,
        preset_id=ch.preset_id,
        title_vi=ch.title_vi,
        status=ch.status,
        status_label_vi=CHALLENGE_STATUS_LABELS_VI.get(ch.status, ch.status),
        input_json=dict(ch.input_json or {}),
        generated_refs_json=dict(ch.generated_refs_json or {}),
        linked_case_ids=list(ch.linked_case_ids_json or []),
        linked_incident_ids=list(ch.linked_incident_ids_json or []),
        agent_run_ids=list(ch.agent_run_ids_json or []),
        outcome_json=dict(ch.outcome_json or {}),
        safety_verdict=ch.safety_verdict,
        source_mode=ch.source_mode,
        created_at=ch.created_at,
        updated_at=ch.updated_at,
    )


def _to_activity_response(act: SupervisorActivity) -> SupervisorActivityItemResponse:
    return SupervisorActivityItemResponse(
        id=act.id,
        seq=act.seq,
        session_id=act.session_id,
        world_epoch=act.world_epoch,
        challenge_id=act.challenge_id,
        case_id=act.case_id,
        job_id=act.job_id,
        run_id=act.run_id,
        kind=act.kind,
        title_vi=act.title_vi,
        detail_vi=act.detail_vi,
        metadata_json=dict(act.metadata_json or {}),
        created_at=act.created_at,
    )


def _to_job_summary(job: SupervisorJob) -> SupervisorJobSummaryResponse:
    return SupervisorJobSummaryResponse(
        id=job.id,
        job_type=job.job_type,
        status=job.status,
        target_case_id=job.target_case_id,
        challenge_id=job.challenge_id,
        attempt_count=job.attempt_count,
        max_attempts=job.max_attempts,
        worker_id=job.worker_id,
        cancel_requested_at=job.cancel_requested_at,
        error_message=job.error_message,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _build_approval_inbox(db: Session, session_id: str) -> list[ApprovalInboxItemResponse]:
    proposals = list(
        db.scalars(
            select(ActionProposal)
            .where(
                ActionProposal.session_id == session_id,
                ActionProposal.status == ActionProposalStatus.PROPOSED.value,
            )
            .order_by(ActionProposal.created_at.desc())
        ).all()
    )
    items: list[ApprovalInboxItemResponse] = []
    for p in proposals:
        case_obj = db.get(Case, p.case_id) if p.case_id else None
        params = dict(p.params_json or {})
        ord_id = params.get("order_id") or ""
        pay_id = params.get("payment_id") or ""
        amt = params.get("amount")

        if p.action_type == "ORDER_SYNC":
            action_label_vi = "Đồng bộ trạng thái đơn hàng"
            summary_vi = (
                f"AI đề xuất đồng bộ trạng thái đơn hàng {ord_id} sang PAID vì giao dịch "
                f"thanh toán MOCK {pay_id} đã được xác minh trong sổ cái."
            )
        elif p.action_type == "REFUND_EXECUTE":
            action_label_vi = "Thực thi hoàn tiền"
            summary_vi = (
                f"AI đề xuất hoàn tiền {amt} VND cho đơn hàng {ord_id} (giao dịch {pay_id}) "
                f"sau khi kiểm tra số dư khả dụng hợp lệ."
            )
        else:
            action_label_vi = f"Tạm giữ đơn hàng ({p.action_type})"
            summary_vi = f"AI đề xuất hành động {p.action_type} cho đơn hàng {ord_id}."

        items.append(
            ApprovalInboxItemResponse(
                proposal_id=p.id,
                case_id=p.case_id or "",
                case_goal=case_obj.goal if case_obj else "",
                case_state=case_obj.state if case_obj else "WAITING_APPROVAL",
                action_type=p.action_type,
                action_label_vi=action_label_vi,
                summary_vi=summary_vi,
                params_json=params,
                evidence_refs=list(p.evidence_refs or []),
                risk_level=p.risk_level,
                expected_version=p.expected_version,
                status=p.status,
                created_at=p.created_at,
            )
        )
    return items


@router.get(
    "/supervisor/status",
    response_model=SupervisorStatusResponse,
    summary="Read-only Supervisor status, worker health, LLM telemetry, and queue depth",
)
def get_supervisor_status(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> SupervisorStatusResponse:
    state = ensure_supervisor_state(db, current_session.id)
    resp = build_supervisor_status_response(db, state)
    db.commit()
    return resp


@router.post(
    "/supervisor/pause",
    response_model=SupervisorStatusResponse,
    summary="Pause Autonomous Agent Supervisor for the current session",
)
def pause_session_supervisor(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> SupervisorStatusResponse:
    state = pause_supervisor(db, current_session.id)
    return build_supervisor_status_response(db, state)


@router.post(
    "/supervisor/resume",
    response_model=SupervisorStatusResponse,
    summary="Resume Autonomous Agent Supervisor and wake background worker (non-blocking)",
)
def resume_session_supervisor(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> SupervisorStatusResponse:
    state = resume_supervisor(db, current_session.id)
    notify_supervisor_worker()
    return build_supervisor_status_response(db, state)


@router.post(
    "/supervisor/jobs/{job_id}/cancel",
    response_model=SupervisorJobSummaryResponse,
    summary="Cooperatively cancel a queued or running Supervisor job",
)
def cancel_supervisor_job(
    job_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> SupervisorJobSummaryResponse:
    job = request_job_cancellation(db, session_id=current_session.id, job_id=job_id)
    db.commit()
    db.refresh(job)
    return _to_job_summary(job)


@router.get(
    "/supervisor/activity",
    response_model=list[SupervisorActivityItemResponse],
    summary="Get ordered, persisted Agent activity timeline events (supports after_seq cursor)",
)
def list_supervisor_activity(
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=60, ge=1, le=200),
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> list[SupervisorActivityItemResponse]:
    state = ensure_supervisor_state(db, current_session.id)
    db.commit()
    rows = list(
        db.scalars(
            select(SupervisorActivity)
            .where(
                SupervisorActivity.session_id == current_session.id,
                SupervisorActivity.world_epoch == state.world_epoch,
                SupervisorActivity.seq > after_seq,
            )
            .order_by(SupervisorActivity.seq.asc())
            .limit(limit)
        ).all()
    )
    return [_to_activity_response(r) for r in rows]


@router.get(
    "/supervisor/overview",
    response_model=MissionControlOverviewResponse,
    summary="Read-only AI Mission Control snapshot (never executes Agent jobs synchronously)",
)
def get_mission_control_overview(
    after_seq: int = Query(default=0, ge=0),
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> MissionControlOverviewResponse:
    state = ensure_supervisor_state(db, current_session.id)
    sup_status = build_supervisor_status_response(db, state)

    challenges = list(
        db.scalars(
            select(ChallengeRun)
            .where(
                ChallengeRun.session_id == current_session.id,
                ChallengeRun.world_epoch == state.world_epoch,
            )
            .order_by(ChallengeRun.created_at.desc())
            .limit(10)
        ).all()
    )
    active_jobs = list(
        db.scalars(
            select(SupervisorJob)
            .where(
                SupervisorJob.session_id == current_session.id,
                SupervisorJob.world_epoch == state.world_epoch,
            )
            .order_by(SupervisorJob.created_at.desc())
            .limit(15)
        ).all()
    )
    activities = list(
        db.scalars(
            select(SupervisorActivity)
            .where(
                SupervisorActivity.session_id == current_session.id,
                SupervisorActivity.world_epoch == state.world_epoch,
                SupervisorActivity.seq > after_seq,
            )
            .order_by(SupervisorActivity.seq.desc())
            .limit(60)
        ).all()
    )
    inbox = _build_approval_inbox(db, current_session.id)
    verifs = list(
        db.scalars(
            select(Verification)
            .join(Case, Case.id == Verification.case_id)
            .where(Case.session_id == current_session.id)
            .order_by(Verification.checked_at.desc())
            .limit(10)
        ).all()
    )
    latest_seq = (
        db.scalar(
            select(func.max(SupervisorActivity.seq)).where(
                SupervisorActivity.session_id == current_session.id,
                SupervisorActivity.world_epoch == state.world_epoch,
            )
        )
        or 0
    )
    db.commit()

    ch_responses = [_to_challenge_response(c) for c in challenges]
    return MissionControlOverviewResponse(
        supervisor=sup_status,
        latest_challenge=ch_responses[0] if ch_responses else None,
        recent_challenges=ch_responses,
        active_jobs=[_to_job_summary(j) for j in active_jobs],
        activities=[_to_activity_response(a) for a in activities],
        approval_inbox=inbox,
        recent_verifications=[
            RecentVerificationItemResponse(
                verification_id=v.id,
                case_id=v.case_id,
                action_execution_id=v.action_execution_id,
                verdict=v.verdict,
                postconditions_json=dict(v.postconditions_json or {}),
                checked_at=v.checked_at,
            )
            for v in verifs
        ],
        latest_seq=int(latest_seq),
    )


@router.post(
    "/events/ingest",
    response_model=IngestEventResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a normalized Commerce/Payment event and enqueue Supervisor investigation (202)",
)
def ingest_business_event(
    payload: NormalizedBusinessEvent,
    _rate_limit: None = Depends(enforce_ai_investigation_rate_limit),
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> IngestEventResponse:
    res = ingest_normalized_business_event(db, session_obj=current_session, event=payload)
    notify_supervisor_worker()
    return res


@router.get(
    "/challenges/catalog",
    response_model=list[ChallengeCatalogItemResponse],
    summary="List Adversarial Challenge Lab regression fixtures and presets",
)
def get_challenge_catalog() -> list[ChallengeCatalogItemResponse]:
    return [ChallengeCatalogItemResponse(**item) for item in list_challenge_catalog()]


@router.post(
    "/challenges/receipt-extract",
    status_code=status.HTTP_200_OK,
    summary="Structured text parser for synthetic MOCK / TEST receipts (not OCR, untrusted CLAIM)",
)
def extract_receipt_sample(
    payload: ExtractReceiptRequest,
    current_session: DemoSession = Depends(get_current_session),
) -> dict[str, Any]:
    _ = current_session
    return extract_mock_receipt_claim(
        raw_content=payload.raw_content,
        filename=payload.filename,
    )


@router.post(
    "/challenges",
    response_model=ChallengeRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Enqueue a Challenge Lab anomaly asynchronously (returns 202 Accepted immediately)",
)
def create_and_enqueue_challenge(
    payload: LaunchChallengeRequest,
    _rate_limit: None = Depends(enforce_ai_investigation_rate_limit),
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> ChallengeRunResponse:
    challenge = launch_challenge(
        db,
        session_obj=current_session,
        challenge_type=payload.challenge_type,
        preset_id=payload.preset_id,
        parameters=payload.parameters,
    )
    notify_supervisor_worker()
    return _to_challenge_response(challenge)


@router.get(
    "/challenges",
    response_model=list[ChallengeRunResponse],
    summary="List executed challenges in the current session",
)
def list_session_challenges(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> list[ChallengeRunResponse]:
    state = ensure_supervisor_state(db, current_session.id)
    rows = list(
        db.scalars(
            select(ChallengeRun)
            .where(
                ChallengeRun.session_id == current_session.id,
                ChallengeRun.world_epoch == state.world_epoch,
            )
            .order_by(ChallengeRun.created_at.desc())
        ).all()
    )
    return [_to_challenge_response(r) for r in rows]


@router.get(
    "/challenges/{challenge_id}",
    response_model=ChallengeRunResponse,
    summary="Get detail and evidence-backed outcome of a challenge",
)
def get_challenge_detail(
    challenge_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> ChallengeRunResponse:
    ch = db.scalar(
        select(ChallengeRun).where(
            ChallengeRun.id == challenge_id,
            ChallengeRun.session_id == current_session.id,
        )
    )
    if ch is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Challenge '{challenge_id}' not found in current session",
            status_code=404,
        )
    return _to_challenge_response(ch)


@router.post(
    "/challenges/{challenge_id}/cancel",
    response_model=ChallengeRunResponse,
    summary="Cooperatively cancel an active or queued challenge and its Supervisor jobs",
)
def cancel_session_challenge(
    challenge_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> ChallengeRunResponse:
    ch = cancel_challenge_run(
        db,
        session_id=current_session.id,
        challenge_id=challenge_id,
    )
    return _to_challenge_response(ch)
