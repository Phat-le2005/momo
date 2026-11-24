"""Event-driven Autonomous Agent Supervisor with PostgreSQL work queue, leases, and activity feed.

Hard Domain Invariants (Prompt 09):
1. Event-driven Autopilot, NOT an endless LLM generation loop:
   - Zero LLM calls when idle (`MONITORING` or `PAUSED`).
   - Only claims work when a new Case or Challenge event is queued in `supervisor_jobs`.
2. Durable PostgreSQL Work Queue with Lease & Stale-Worker Recovery:
   - Tasks are persisted in `supervisor_jobs` with `dedupe_key`, `locked_until`, `worker_id`,
     `heartbeat_at`, `attempt_count`, and `max_attempts`.
   - Workers claim jobs in a short transaction (`SELECT ... FOR UPDATE SKIP LOCKED`), release
     the DB transaction during `BoundedAgentRuntime` / `TaskforceManager` execution, and commit
     outcomes in a final short transaction checking `world_epoch`.
   - If a worker crashes mid-job (`locked_until < now`), `recover_stale_supervisor_jobs`
     idempotently recovers the Case via `recover_stale_investigation()` and either boundedly
     re-queues the job or marks it failed without duplicate financial effects.
3. World Epoch Supersede Safety:
   - Session reset increments `SupervisorState.world_epoch` and marks old jobs `SUPERSEDED`.
   - Any late-returning worker from an older `world_epoch` aborts before writing.
4. AI Execution Honesty:
   - Tracks `LLM_API_CONFIGURED`, `LLM_CALL_STARTED`, `LLM_CALL_SUCCEEDED`, `LLM_CALL_FAILED`,
     and `DEMO_RULES_FALLBACK` separately. A configured API key never counts as `LLM_CALL_SUCCEEDED`
     until a real provider call completes without fallback.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from app.ai_core.model_adapter import DeterministicDemoAdapter
from app.ai_core.runtime import BoundedAgentRuntime, recover_stale_investigation
from app.core.config import ModelMode, get_settings
from app.core.errors import AppError, ErrorCode
from app.db.session import get_sessionmaker
from app.incidents.correlator import IncidentCorrelator
from app.incidents.taskforce import TaskforceManager
from app.models.actions import ActionProposal
from app.models.cases import Case
from app.models.enums import (
    ActionProposalStatus,
    CaseState,
    ChallengeStatus,
    LlmExecutionState,
    SupervisorJobStatus,
    SupervisorJobType,
    SupervisorStatus,
)
from app.models.session import DemoSession
from app.models.supervisor import (
    ChallengeRun,
    SupervisorActivity,
    SupervisorJob,
    SupervisorState,
)

DEFAULT_JOB_LEASE_SECONDS = 45
MAX_QUEUE_DEPTH_PER_SESSION = 25

TOOL_LABELS_VI: dict[str, str] = {
    "get_order": "Kiểm tra thông tin đơn hàng (get_order)",
    "find_payment_candidates": "Đối chiếu giao dịch thanh toán khả dĩ (find_payment_candidates)",
    "get_payment_record": "Truy vấn bản ghi thanh toán gốc từ sổ cái (get_payment_record)",
    "get_refund_history": "Kiểm tra lịch sử và số dư hoàn tiền (get_refund_history)",
    "get_inventory": "Kiểm tra tồn kho và giữ chỗ (get_inventory)",
    "get_policy": "Tra cứu chính sách vận hành (get_policy)",
    "get_order_events": "Đọc lịch sử sự kiện đồng bộ đơn hàng (get_order_events)",
}


def initial_llm_execution_state() -> str:
    settings = get_settings()
    if settings.model_mode == ModelMode.LLM_API:
        return LlmExecutionState.LLM_API_CONFIGURED.value
    return LlmExecutionState.DEMO_RULES_FALLBACK.value


def ensure_supervisor_state(db: Session, session_id: str) -> SupervisorState:
    """Get or create the session-scoped SupervisorState row."""
    state = db.get(SupervisorState, session_id)
    if state is not None:
        # Refresh configured state if still idle with 0 calls
        if state.llm_calls_count == 0 and state.last_llm_failed_at is None:
            expected_init = initial_llm_execution_state()
            if state.llm_execution_state in (
                LlmExecutionState.LLM_API_CONFIGURED.value,
                LlmExecutionState.DEMO_RULES_FALLBACK.value,
            ):
                state.llm_execution_state = expected_init
        return state

    now = datetime.now(UTC)
    state = SupervisorState(
        session_id=session_id,
        status=SupervisorStatus.MONITORING.value,
        autopilot_enabled=True,
        world_epoch=1,
        active_job_id=None,
        active_case_id=None,
        llm_execution_state=initial_llm_execution_state(),
        llm_calls_count=0,
        llm_tokens_used=0,
        llm_budget_calls=30,
        llm_budget_tokens=max(20000, get_settings().model_budget_tokens * 2),
        jobs_processed_count=0,
        created_at=now,
        updated_at=now,
    )
    db.add(state)
    db.flush()
    record_activity(
        db,
        session_id=session_id,
        world_epoch=state.world_epoch,
        kind="SUPERVISOR_STATE",
        title_vi="AI Supervisor đã sẵn sàng giám sát môi trường MOCK",
        detail_vi=(
            "Chế độ Autopilot theo sự kiện đang bật — hệ thống chỉ kích hoạt Agent khi phát hiện "
            "khiếu nại, hóa đơn đáng ngờ hoặc biến động bất thường."
        ),
        metadata_json={
            "status": state.status,
            "llm_execution_state": state.llm_execution_state,
        },
    )
    return state


def record_activity(
    db: Session,
    *,
    session_id: str,
    world_epoch: int,
    kind: str,
    title_vi: str,
    detail_vi: str | None = None,
    challenge_id: str | None = None,
    case_id: str | None = None,
    job_id: str | None = None,
    run_id: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> SupervisorActivity:
    """Append an ordered, persisted activity event for the session's live feed."""
    max_seq = (
        db.scalar(
            select(func.max(SupervisorActivity.seq)).where(
                SupervisorActivity.session_id == session_id
            )
        )
        or 0
    )
    act = SupervisorActivity(
        id=f"ACT-{uuid.uuid4().hex[:12].upper()}",
        seq=int(max_seq) + 1,
        session_id=session_id,
        world_epoch=world_epoch,
        challenge_id=challenge_id,
        case_id=case_id,
        job_id=job_id,
        run_id=run_id,
        kind=kind,
        title_vi=title_vi[:255],
        detail_vi=detail_vi[:1024] if detail_vi else None,
        metadata_json=dict(metadata_json or {}),
        created_at=datetime.now(UTC),
    )
    db.add(act)
    db.flush()
    return act


def recompute_supervisor_idle_status(db: Session, state: SupervisorState) -> str:
    """Determine accurate Supervisor status based on current session Cases, proposals, and queue."""
    if not state.autopilot_enabled:
        state.status = SupervisorStatus.PAUSED.value
        state.active_job_id = None
        state.active_case_id = None
        return state.status

    running_job = db.scalar(
        select(SupervisorJob).where(
            SupervisorJob.session_id == state.session_id,
            SupervisorJob.world_epoch == state.world_epoch,
            SupervisorJob.status.in_(
                [SupervisorJobStatus.RUNNING.value, SupervisorJobStatus.QUEUED.value]
            ),
        )
    )
    if running_job is not None:
        state.status = SupervisorStatus.INVESTIGATING.value
        state.active_job_id = running_job.id
        state.active_case_id = running_job.target_case_id
        return state.status

    state.active_job_id = None
    state.active_case_id = None

    pending_approvals = (
        db.scalar(
            select(func.count(ActionProposal.id)).where(
                ActionProposal.session_id == state.session_id,
                ActionProposal.status == ActionProposalStatus.PROPOSED.value,
            )
        )
        or 0
    )
    if pending_approvals > 0:
        state.status = SupervisorStatus.WAITING_APPROVAL.value
        return state.status

    waiting_evidence_cases = (
        db.scalar(
            select(func.count(Case.id)).where(
                Case.session_id == state.session_id,
                Case.state == CaseState.WAITING_EVIDENCE.value,
            )
        )
        or 0
    )
    if waiting_evidence_cases > 0:
        state.status = SupervisorStatus.WAITING_EVIDENCE.value
        return state.status

    state.status = SupervisorStatus.MONITORING.value
    return state.status


def enqueue_case_investigation_job(
    db: Session,
    *,
    session_id: str,
    case_id: str,
    challenge_id: str | None = None,
    reason_vi: str = "Phát hiện hồ sơ ngoại lệ mới cần AI điều tra",
    priority: int = 100,
    payload_extra: dict[str, Any] | None = None,
) -> SupervisorJob:
    """Idempotently enqueue a bounded Case investigation job in PostgreSQL."""
    state = ensure_supervisor_state(db, session_id)
    case_obj = db.scalar(select(Case).where(Case.id == case_id, Case.session_id == session_id))
    if case_obj is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Case '{case_id}' not found", status_code=404)

    dedupe_key = f"case:{case_id}:v{case_obj.version}"
    existing = db.scalar(
        select(SupervisorJob).where(
            SupervisorJob.session_id == session_id,
            SupervisorJob.world_epoch == state.world_epoch,
            SupervisorJob.dedupe_key == dedupe_key,
            SupervisorJob.status.in_(
                [SupervisorJobStatus.QUEUED.value, SupervisorJobStatus.RUNNING.value]
            ),
        )
    )
    if existing is not None:
        return existing

    queue_depth = (
        db.scalar(
            select(func.count(SupervisorJob.id)).where(
                SupervisorJob.session_id == session_id,
                SupervisorJob.status.in_(
                    [SupervisorJobStatus.QUEUED.value, SupervisorJobStatus.RUNNING.value]
                ),
            )
        )
        or 0
    )
    if queue_depth >= MAX_QUEUE_DEPTH_PER_SESSION:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            f"Supervisor queue depth limit reached ({MAX_QUEUE_DEPTH_PER_SESSION} pending jobs)",
            status_code=429,
        )

    now = datetime.now(UTC)
    job = SupervisorJob(
        id=f"JOB-{uuid.uuid4().hex[:10].upper()}",
        session_id=session_id,
        world_epoch=state.world_epoch,
        job_type=SupervisorJobType.INVESTIGATE_CASE.value,
        dedupe_key=dedupe_key,
        target_case_id=case_id,
        challenge_id=challenge_id,
        status=SupervisorJobStatus.QUEUED.value,
        priority=priority,
        attempt_count=0,
        max_attempts=2,
        payload_json={
            "case_id": case_id,
            "challenge_id": challenge_id,
            **(payload_extra or {}),
        },
        result_json={},
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.flush()

    record_activity(
        db,
        session_id=session_id,
        world_epoch=state.world_epoch,
        challenge_id=challenge_id,
        case_id=case_id,
        job_id=job.id,
        kind="EVENT_DETECTED",
        title_vi=reason_vi,
        detail_vi=f"Mục tiêu: {case_obj.goal}",
        metadata_json={"job_id": job.id, "case_id": case_id, "case_type": case_obj.type},
    )
    recompute_supervisor_idle_status(db, state)
    return job


def enqueue_incident_correlation_job(
    db: Session,
    *,
    session_id: str,
    challenge_id: str | None = None,
    representative_case_id: str | None = None,
    reason_vi: str = (
        "Phát hiện cụm đơn hàng lỗi đồng bộ hàng loạt — kích hoạt tương quan Incident & Taskforce"
    ),
) -> SupervisorJob:
    """Enqueue an Incident correlation + Taskforce + representative Case investigation job."""
    state = ensure_supervisor_state(db, session_id)
    dedupe_key = f"correlate:{challenge_id or session_id}:e{state.world_epoch}"
    existing = db.scalar(
        select(SupervisorJob).where(
            SupervisorJob.session_id == session_id,
            SupervisorJob.world_epoch == state.world_epoch,
            SupervisorJob.dedupe_key == dedupe_key,
            SupervisorJob.status.in_(
                [SupervisorJobStatus.QUEUED.value, SupervisorJobStatus.RUNNING.value]
            ),
        )
    )
    if existing is not None:
        return existing

    now = datetime.now(UTC)
    job = SupervisorJob(
        id=f"JOB-{uuid.uuid4().hex[:10].upper()}",
        session_id=session_id,
        world_epoch=state.world_epoch,
        job_type=SupervisorJobType.CORRELATE_INCIDENT.value,
        dedupe_key=dedupe_key,
        target_case_id=representative_case_id,
        challenge_id=challenge_id,
        status=SupervisorJobStatus.QUEUED.value,
        priority=90,
        attempt_count=0,
        max_attempts=2,
        payload_json={
            "challenge_id": challenge_id,
            "representative_case_id": representative_case_id,
        },
        result_json={},
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.flush()

    record_activity(
        db,
        session_id=session_id,
        world_epoch=state.world_epoch,
        challenge_id=challenge_id,
        case_id=representative_case_id,
        job_id=job.id,
        kind="EVENT_DETECTED",
        title_vi=reason_vi,
        detail_vi="Supervisor tự động gom nhóm theo bằng chứng nhân quả và phân công Taskforce.",
        metadata_json={"job_id": job.id, "job_type": job.job_type},
    )
    recompute_supervisor_idle_status(db, state)
    return job


def bump_world_epoch_on_reset(db: Session, session_id: str) -> int:
    """Invalidate any in-flight or queued Supervisor jobs when a session's MOCK world is reset."""
    state = ensure_supervisor_state(db, session_id)
    state.world_epoch += 1
    state.active_job_id = None
    state.active_case_id = None
    state.updated_at = datetime.now(UTC)

    db.execute(
        update(SupervisorJob)
        .where(
            SupervisorJob.session_id == session_id,
            SupervisorJob.status.in_(
                [SupervisorJobStatus.QUEUED.value, SupervisorJobStatus.RUNNING.value]
            ),
        )
        .values(
            status=SupervisorJobStatus.SUPERSEDED.value,
            error_message="Superseded by session MOCK world reset",
            completed_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    db.execute(delete(ChallengeRun).where(ChallengeRun.session_id == session_id))
    db.execute(delete(SupervisorActivity).where(SupervisorActivity.session_id == session_id))
    db.flush()

    record_activity(
        db,
        session_id=session_id,
        world_epoch=state.world_epoch,
        kind="SUPERVISOR_STATE",
        title_vi="Đã đặt lại môi trường MOCK — AI Supervisor tiếp tục giám sát",
        detail_vi=f"Khởi tạo chu kỳ thế giới mới (world_epoch={state.world_epoch}).",
        metadata_json={"world_epoch": state.world_epoch},
    )
    recompute_supervisor_idle_status(db, state)
    return state.world_epoch


def pause_supervisor(db: Session, session_id: str) -> SupervisorState:
    state = ensure_supervisor_state(db, session_id)
    state.autopilot_enabled = False
    state.status = SupervisorStatus.PAUSED.value
    state.updated_at = datetime.now(UTC)
    record_activity(
        db,
        session_id=session_id,
        world_epoch=state.world_epoch,
        kind="SUPERVISOR_STATE",
        title_vi="Đã tạm dừng AI Supervisor (PAUSED)",
        detail_vi=(
            "Các sự kiện mới vẫn được ghi nhận vào hàng đợi nhưng chưa tự động chạy "
            "điều tra cho tới khi bấm Tiếp tục."
        ),
        metadata_json={"status": state.status, "autopilot_enabled": False},
    )
    db.commit()
    db.refresh(state)
    return state


def resume_supervisor(db: Session, session_id: str) -> SupervisorState:
    state = ensure_supervisor_state(db, session_id)
    state.autopilot_enabled = True
    recompute_supervisor_idle_status(db, state)
    state.updated_at = datetime.now(UTC)
    record_activity(
        db,
        session_id=session_id,
        world_epoch=state.world_epoch,
        kind="SUPERVISOR_STATE",
        title_vi="Đã bật lại AI Supervisor (Autopilot đang giám sát)",
        detail_vi="Supervisor sẽ tự động xử lý các sự kiện đang chờ trong hàng đợi.",
        metadata_json={"status": state.status, "autopilot_enabled": True},
    )
    db.commit()
    db.refresh(state)
    return state


def recover_stale_supervisor_jobs(
    db: Session,
    *,
    session_id: str | None = None,
    stale_after_seconds: int | None = None,
) -> list[str]:
    """Detect and safely recover crashed worker jobs whose lease has expired."""
    now = datetime.now(UTC)
    stmt = select(SupervisorJob).where(SupervisorJob.status == SupervisorJobStatus.RUNNING.value)
    if session_id is not None:
        stmt = stmt.where(SupervisorJob.session_id == session_id)
    if stale_after_seconds is not None and stale_after_seconds <= 0:
        pass
    else:
        stmt = stmt.where(SupervisorJob.locked_until <= now)

    try:
        stale_jobs = list(db.scalars(stmt.with_for_update(skip_locked=True)).all())
    except OperationalError:
        db.rollback()
        return []

    recovered_ids: list[str] = []
    for job in stale_jobs:
        state = ensure_supervisor_state(db, job.session_id)
        if job.target_case_id:
            recover_stale_investigation(
                db,
                case_id=job.target_case_id,
                session_id=job.session_id,
                stale_after_seconds=0,
            )
            # Re-open Case from ESCALATED to NEW if retrying cleanly and no proposal exists yet
            if job.attempt_count < job.max_attempts:
                case_obj = db.scalar(
                    select(Case)
                    .options(selectinload(Case.action_proposals))
                    .where(Case.id == job.target_case_id, Case.session_id == job.session_id)
                )
                if (
                    case_obj is not None
                    and case_obj.state == CaseState.ESCALATED.value
                    and not case_obj.action_proposals
                ):
                    case_obj.state = CaseState.NEW.value
                    case_obj.version += 1

        if job.attempt_count < job.max_attempts:
            job.status = SupervisorJobStatus.QUEUED.value
            job.worker_id = None
            job.locked_until = None
            job.error_message = "Worker lease expired; safely recovered and re-queued"
            job.updated_at = now
            record_activity(
                db,
                session_id=job.session_id,
                world_epoch=state.world_epoch,
                challenge_id=job.challenge_id,
                case_id=job.target_case_id,
                job_id=job.id,
                kind="WORKER_RECOVERED",
                title_vi=("Phát hiện tiến trình xử lý trước bị gián đoạn — đã khôi phục an toàn"),
                detail_vi=(
                    f"Tác vụ {job.id} được đưa lại vào hàng đợi (lần thử {job.attempt_count}/"
                    f"{job.max_attempts}), đảm bảo không nhân đôi tác động tài chính."
                ),
                metadata_json={"job_id": job.id, "attempt_count": job.attempt_count},
            )
        else:
            job.status = SupervisorJobStatus.FAILED.value
            job.completed_at = now
            job.updated_at = now
            job.error_message = "Worker interrupted and exceeded bounded max_attempts"
            state.status = SupervisorStatus.DEGRADED.value
            record_activity(
                db,
                session_id=job.session_id,
                world_epoch=state.world_epoch,
                challenge_id=job.challenge_id,
                case_id=job.target_case_id,
                job_id=job.id,
                kind="WORKER_RECOVERED",
                title_vi=(
                    "Tác vụ bị gián đoạn vượt số lần thử tối đa — chuyển sang trạng thái an toàn"
                ),
                detail_vi="Không có thay đổi tài chính trái phép nào được thực thi.",
                metadata_json={"job_id": job.id, "status": "FAILED"},
            )
        recovered_ids.append(job.id)
        recompute_supervisor_idle_status(db, state)

    if recovered_ids:
        db.commit()
    return recovered_ids


def _build_vietnamese_conclusion(
    *,
    case_obj: Case,
    decision_summary: str,
    proposals: list[ActionProposal],
    challenge_type: str | None = None,
) -> tuple[str, str]:
    """Produce a clear, non-technical Vietnamese conclusion and safety verdict."""
    has_pending_proposal = any(p.status == ActionProposalStatus.PROPOSED.value for p in proposals)
    has_hold_risk = any(r.response == "HOLD_ACTION" for r in (case_obj.risk_signals or []))

    if case_obj.state == CaseState.WAITING_APPROVAL.value or has_pending_proposal:
        prop = next(
            (p for p in proposals if p.status == ActionProposalStatus.PROPOSED.value),
            proposals[0],
        )
        if prop.action_type == "ORDER_SYNC":
            return (
                (
                    "AI đã xác minh giao dịch thanh toán MOCK hợp lệ trong sổ cái và đề xuất "
                    "đồng bộ trạng thái đơn hàng — đang chờ con người phê duyệt."
                ),
                "WAITING_HUMAN_APPROVAL",
            )
        if prop.action_type == "REFUND_EXECUTE":
            return (
                (
                    "AI đã đối chiếu số dư hoàn tiền khả dụng và đề xuất hoàn tiền hợp lệ — "
                    "đang chờ con người phê duyệt."
                ),
                "WAITING_HUMAN_APPROVAL",
            )
        return (
            f"AI đề xuất hành động {prop.action_type} dựa trên bằng chứng xác thực — "
            "đang chờ phê duyệt.",
            "WAITING_HUMAN_APPROVAL",
        )

    if challenge_type == "SUSPICIOUS_RECEIPT":
        return (
            (
                "AI không xác nhận thanh toán vì dữ liệu trên hóa đơn không khớp hoặc "
                "không tồn tại trong sổ cái giao dịch xác thực."
            ),
            "SAFE_BLOCKED",
        )
    if challenge_type == "DUPLICATE_REFUND" or has_hold_risk:
        return (
            (
                "AI phát hiện yêu cầu hoàn tiền trùng lặp hoặc vượt số dư khả dụng và đã "
                "chặn hành động hoàn tiền trái phép (HOLD_ACTION)."
            ),
            "SAFE_BLOCKED",
        )
    if challenge_type == "PROMPT_INJECTION":
        return (
            (
                "AI xem chỉ dẫn độc hại trong văn bản là lời khai chưa xác thực (CLAIM), "
                "giữ nguyên quy tắc an toàn và từ chối tự ý xác nhận thanh toán."
            ),
            "SAFE_BLOCKED",
        )
    if challenge_type == "TRAFFIC_BURST":
        return (
            (
                "AI đã phân tích số liệu lưu lượng tổng hợp, ghi nhận các yêu cầu bị giới hạn "
                "tốc độ/trùng lặp và giữ nguyên các chốt chặn an toàn."
            ),
            "SAFE_OBSERVED",
        )
    if case_obj.state == CaseState.WAITING_EVIDENCE.value:
        return (
            (
                "AI không thực hiện thay đổi tài chính vì bằng chứng giao dịch chưa khớp hoặc "
                "còn thiếu — yêu cầu bổ sung bằng chứng xác thực."
            ),
            "SAFE_BLOCKED",
        )
    return (
        decision_summary
        or (
            "AI đã hoàn tất điều tra trong giới hạn an toàn và không thực hiện ghi nhận "
            "tài chính trái phép."
        ),
        "SAFE_OBSERVED",
    )


def _persist_run_activities_and_challenge_outcome(
    db: Session,
    *,
    job: SupervisorJob,
    state: SupervisorState,
    run_id: str,
    steps_executed: int,
    total_tokens_used: int,
    model_mode_val: str,
    is_fallback: bool,
    stop_reason: str | None,
    step_traces: list[Any],
    decision_summary: str,
    incident_ids: list[str] | None = None,
) -> None:
    """Translate actual investigation step traces into persisted human-readable activity items
    and update the linked ChallengeRun outcome."""
    now = datetime.now(UTC)
    configured_llm = get_settings().model_mode == ModelMode.LLM_API
    llm_failed = (
        is_fallback
        or stop_reason == "MAX_REPLANS_EXCEEDED"
        or (configured_llm and steps_executed == 0 and not incident_ids)
    )
    if model_mode_val == ModelMode.LLM_API.value and not llm_failed:
        state.llm_execution_state = LlmExecutionState.LLM_CALL_SUCCEEDED.value
        state.last_llm_succeeded_at = now
        state.last_llm_error = None
        state.llm_calls_count += max(1, steps_executed)
        state.llm_tokens_used += max(0, total_tokens_used)
        record_activity(
            db,
            session_id=job.session_id,
            world_epoch=state.world_epoch,
            challenge_id=job.challenge_id,
            case_id=job.target_case_id,
            job_id=job.id,
            run_id=run_id,
            kind="LLM_CALL_SUCCEEDED",
            title_vi=(
                f"Mô hình LLM suy luận thành công "
                f"({steps_executed} bước, {total_tokens_used} tokens)"
            ),
            detail_vi=decision_summary[:400] if decision_summary else None,
            metadata_json={
                "model_mode": "LLM_API",
                "is_fallback": False,
                "steps_executed": steps_executed,
                "total_tokens_used": total_tokens_used,
            },
        )
    elif configured_llm and llm_failed:
        state.llm_execution_state = LlmExecutionState.LLM_CALL_FAILED.value
        state.last_llm_failed_at = now
        state.last_llm_error = (
            "Gọi LLM provider không thành công hoặc hết thời gian chờ — "
            "đã chuyển về DEMO_RULES an toàn."
        )
        record_activity(
            db,
            session_id=job.session_id,
            world_epoch=state.world_epoch,
            challenge_id=job.challenge_id,
            case_id=job.target_case_id,
            job_id=job.id,
            run_id=run_id,
            kind="LLM_CALL_FAILED",
            title_vi=(
                "Gọi LLM API không thành công — "
                "chuyển sang chế độ luật xác định (DEMO_RULES_FALLBACK)"
            ),
            detail_vi=state.last_llm_error,
            metadata_json={"model_mode": "DEMO_RULES", "is_fallback": True},
        )
    else:
        state.llm_execution_state = LlmExecutionState.DEMO_RULES_FALLBACK.value

    tools_ran: list[str] = []
    observations_vi: list[str] = []

    for trace in step_traces:
        t_obs = getattr(trace, "tool_observation", None)
        if t_obs is not None:
            t_name = str(getattr(t_obs, "tool_name", "") or "")
            t_status = str(getattr(t_obs, "status", "") or "")
            t_data = dict(getattr(t_obs, "data", {}) or {})
            if t_name:
                tools_ran.append(t_name)
            label_vi = TOOL_LABELS_VI.get(t_name, f"AI gọi công cụ đọc dữ liệu ({t_name})")
            obs_detail = _summarize_tool_observation_vi(t_name, t_status, t_data)
            observations_vi.append(obs_detail)
            record_activity(
                db,
                session_id=job.session_id,
                world_epoch=state.world_epoch,
                challenge_id=job.challenge_id,
                case_id=job.target_case_id,
                job_id=job.id,
                run_id=run_id,
                kind="TOOL_INVOKED",
                title_vi=label_vi,
                detail_vi=obs_detail,
                metadata_json={
                    "tool_name": t_name,
                    "status": t_status,
                    "source_ref": getattr(t_obs, "source_ref", ""),
                    "evidence_id": getattr(t_obs, "evidence_id", None),
                },
            )

    case_obj: Case | None = None
    if job.target_case_id:
        case_obj = db.scalar(
            select(Case)
            .options(
                selectinload(Case.evidence),
                selectinload(Case.action_proposals),
                selectinload(Case.risk_signals),
                selectinload(Case.verifications),
            )
            .where(Case.id == job.target_case_id, Case.session_id == job.session_id)
        )

    proposals = list(case_obj.action_proposals or []) if case_obj else []
    risks = list(case_obj.risk_signals or []) if case_obj else []
    challenge: ChallengeRun | None = None
    if job.challenge_id:
        challenge = db.scalar(
            select(ChallengeRun).where(
                ChallengeRun.id == job.challenge_id,
                ChallengeRun.session_id == job.session_id,
            )
        )

    for r in risks:
        exp_vi = str((r.details_json or {}).get("explanation_vi") or r.category)
        record_activity(
            db,
            session_id=job.session_id,
            world_epoch=state.world_epoch,
            challenge_id=job.challenge_id,
            case_id=job.target_case_id,
            job_id=job.id,
            run_id=run_id,
            kind="RISK_EVALUATED",
            title_vi=f"Đánh giá rủi ro: {r.response} ({r.level})",
            detail_vi=exp_vi,
            metadata_json={
                "risk_id": r.id,
                "category": r.category,
                "level": r.level,
                "response": r.response,
            },
        )

    for p in proposals:
        if p.status == ActionProposalStatus.PROPOSED.value:
            record_activity(
                db,
                session_id=job.session_id,
                world_epoch=state.world_epoch,
                challenge_id=job.challenge_id,
                case_id=job.target_case_id,
                job_id=job.id,
                run_id=run_id,
                kind="PROPOSAL_CREATED",
                title_vi=f"AI tạo đề xuất {p.action_type} — dừng lại chờ con người phê duyệt",
                detail_vi=(
                    "AI không tự ý ghi nhận giao dịch; quyết định đã được chuyển vào "
                    "Hộp thư phê duyệt."
                ),
                metadata_json={
                    "proposal_id": p.id,
                    "action_type": p.action_type,
                    "params_json": p.params_json,
                },
            )

    conclusion_vi = decision_summary
    safety_verdict = "SAFE_OBSERVED"
    if case_obj is not None:
        conclusion_vi, safety_verdict = _build_vietnamese_conclusion(
            case_obj=case_obj,
            decision_summary=decision_summary,
            proposals=proposals,
            challenge_type=challenge.challenge_type if challenge else None,
        )

    record_activity(
        db,
        session_id=job.session_id,
        world_epoch=state.world_epoch,
        challenge_id=job.challenge_id,
        case_id=job.target_case_id,
        job_id=job.id,
        run_id=run_id,
        kind="DECISION_FINALIZED",
        title_vi=conclusion_vi[:255],
        detail_vi=decision_summary[:600] if decision_summary else None,
        metadata_json={
            "case_state": case_obj.state if case_obj else None,
            "safety_verdict": safety_verdict,
            "tools_ran": tools_ran,
        },
    )

    if challenge is not None:
        run_ids = list(challenge.agent_run_ids_json or [])
        if run_id and run_id not in run_ids:
            run_ids.append(run_id)
        challenge.agent_run_ids_json = run_ids
        if incident_ids:
            inc_list = list(challenge.linked_incident_ids_json or [])
            for iid in incident_ids:
                if iid not in inc_list:
                    inc_list.append(iid)
            challenge.linked_incident_ids_json = inc_list

        has_pending_approval = any(
            p.status == ActionProposalStatus.PROPOSED.value for p in proposals
        )
        action_blocked = safety_verdict == "SAFE_BLOCKED" or any(
            r.response == "HOLD_ACTION" for r in risks
        )
        verified_pass = bool(
            case_obj and any(v.verdict == "PASSED" for v in (case_obj.verifications or []))
        )

        challenge.safety_verdict = safety_verdict
        challenge.status = (
            ChallengeStatus.WAITING_APPROVAL.value
            if has_pending_approval
            else (
                ChallengeStatus.BLOCKED_SAFELY.value
                if action_blocked
                else ChallengeStatus.COMPLETED.value
            )
        )
        challenge.outcome_json = {
            "summary_vi": conclusion_vi,
            "decision_summary": decision_summary,
            "tools_ran": tools_ran,
            "observations_vi": observations_vi,
            "action_blocked": action_blocked,
            "requires_human_approval": has_pending_approval,
            "postconditions_verified": verified_pass,
            "case_id": case_obj.id if case_obj else job.target_case_id,
            "case_state": case_obj.state if case_obj else None,
            "model_mode": model_mode_val,
            "is_fallback": is_fallback,
            "llm_execution_state": state.llm_execution_state,
            "steps_executed": steps_executed,
            "total_tokens_used": total_tokens_used,
            "proposal_ids": [p.id for p in proposals],
            "risk_signals": [
                {
                    "id": r.id,
                    "category": r.category,
                    "level": r.level,
                    "response": r.response,
                    "explanation_vi": (r.details_json or {}).get("explanation_vi"),
                }
                for r in risks
            ],
            "evidence_refs": [e.id for e in (case_obj.evidence or [])] if case_obj else [],
            "incident_ids": list(challenge.linked_incident_ids_json or []),
        }
        challenge.updated_at = now


def _summarize_tool_observation_vi(tool_name: str, status: str, data: dict[str, Any]) -> str:
    if tool_name == "get_payment_record":
        if status == "NOT_FOUND" or not data.get("found", True):
            return "Không tìm thấy giao dịch tương ứng với mã đã cung cấp trong sổ cái xác thực."
        p_status = data.get("status") or data.get("payment_status") or "UNKNOWN"
        amt = data.get("amount")
        ord_id = data.get("order_id")
        return f"Bản ghi sổ cái: trạng thái={p_status}, số tiền={amt} VND, đơn hàng={ord_id}."
    if tool_name == "find_payment_candidates":
        cands = data.get("candidates") or []
        if not cands:
            return "Không tìm thấy giao dịch thanh toán nào khớp với đơn hàng."
        first = cands[0] if isinstance(cands[0], dict) else {}
        return (
            f"Tìm thấy {len(cands)} giao dịch trong sổ cái "
            f"(mã {first.get('payment_id')}, trạng thái {first.get('status')}, "
            f"số tiền {first.get('amount')} VND)."
        )
    if tool_name == "get_order":
        if status == "NOT_FOUND":
            return "Không tìm thấy đơn hàng tương ứng trong hệ thống."
        return (
            f"Đơn hàng {data.get('order_id')}: trạng thái={data.get('status')}, "
            f"thanh toán={data.get('payment_status')}, tổng tiền={data.get('total_amount')} VND."
        )
    if tool_name == "get_refund_history":
        rem = data.get("refundable_remainder")
        settled = data.get("settled_refunds")
        pending = data.get("pending_or_held_refunds")
        return (
            f"Lịch sử hoàn tiền: đã hoàn={settled} VND, đang chờ={pending} VND, "
            f"số dư còn được hoàn={rem} VND."
        )
    if tool_name == "get_inventory":
        avail = data.get("available_qty", data.get("on_hand_qty"))
        return f"Tồn kho khả dụng: {avail} sản phẩm."
    return f"Kết quả công cụ {tool_name}: {status}."


def process_next_supervisor_job(
    *,
    session_id: str | None = None,
    worker_id: str | None = None,
    force_when_paused: bool = False,
) -> dict[str, Any] | None:
    """Claim and execute at most ONE queued Supervisor job using 3-phase short transactions.

    Returns a summary dict if a job was claimed and processed, or `None` if idle/paused.
    """
    eff_worker = worker_id or f"sup-{uuid.uuid4().hex[:8]}"
    maker = get_sessionmaker()

    # Phase 1: Short transaction to recover stale jobs and claim the next QUEUED job
    claimed_job_id: str | None = None
    claimed_session_id: str = ""
    claimed_world_epoch: int = 1
    claimed_job_type: str = ""
    claimed_case_id: str | None = None
    claimed_challenge_id: str | None = None

    with maker() as db:
        recover_stale_supervisor_jobs(db, session_id=session_id)

        stmt = (
            select(SupervisorJob)
            .join(DemoSession, DemoSession.id == SupervisorJob.session_id)
            .where(
                SupervisorJob.status == SupervisorJobStatus.QUEUED.value,
                DemoSession.status == "ACTIVE",
                DemoSession.expires_at > datetime.now(UTC),
            )
            .order_by(SupervisorJob.priority.asc(), SupervisorJob.created_at.asc())
        )
        if session_id is not None:
            stmt = stmt.where(SupervisorJob.session_id == session_id)

        try:
            candidates = list(db.scalars(stmt.with_for_update(skip_locked=True)).all())
        except OperationalError:
            db.rollback()
            return None

        target_job: SupervisorJob | None = None
        target_state: SupervisorState | None = None
        for cand in candidates:
            st = ensure_supervisor_state(db, cand.session_id)
            if cand.world_epoch != st.world_epoch:
                cand.status = SupervisorJobStatus.SUPERSEDED.value
                cand.updated_at = datetime.now(UTC)
                continue
            if not st.autopilot_enabled and not force_when_paused:
                continue
            target_job = cand
            target_state = st
            break

        if target_job is None or target_state is None:
            db.commit()
            return None

        now = datetime.now(UTC)
        target_job.status = SupervisorJobStatus.RUNNING.value
        target_job.attempt_count += 1
        target_job.worker_id = eff_worker
        target_job.started_at = now
        target_job.heartbeat_at = now
        target_job.locked_until = now + timedelta(seconds=DEFAULT_JOB_LEASE_SECONDS)
        target_job.updated_at = now

        target_state.status = SupervisorStatus.INVESTIGATING.value
        target_state.active_job_id = target_job.id
        target_state.active_case_id = target_job.target_case_id
        target_state.updated_at = now

        if get_settings().model_mode == ModelMode.LLM_API:
            target_state.llm_execution_state = LlmExecutionState.LLM_CALL_STARTED.value
            record_activity(
                db,
                session_id=target_job.session_id,
                world_epoch=target_state.world_epoch,
                challenge_id=target_job.challenge_id,
                case_id=target_job.target_case_id,
                job_id=target_job.id,
                kind="LLM_CALL_STARTED",
                title_vi="AI Supervisor bắt đầu phân tích và lập kế hoạch điều tra",
                detail_vi=f"Đang xử lý tác vụ {target_job.id} ({target_job.job_type}).",
                metadata_json={"job_id": target_job.id, "worker_id": eff_worker},
            )
        else:
            target_state.llm_execution_state = LlmExecutionState.DEMO_RULES_FALLBACK.value
            record_activity(
                db,
                session_id=target_job.session_id,
                world_epoch=target_state.world_epoch,
                challenge_id=target_job.challenge_id,
                case_id=target_job.target_case_id,
                job_id=target_job.id,
                kind="JOB_CLAIMED",
                title_vi="AI Supervisor bắt đầu điều tra tự động (chế độ DEMO_RULES)",
                detail_vi=f"Đang xử lý tác vụ {target_job.id} ({target_job.job_type}).",
                metadata_json={"job_id": target_job.id, "worker_id": eff_worker},
            )

        if target_job.challenge_id:
            ch = db.scalar(
                select(ChallengeRun).where(
                    ChallengeRun.id == target_job.challenge_id,
                    ChallengeRun.session_id == target_job.session_id,
                )
            )
            if ch is not None and ch.status == ChallengeStatus.QUEUED.value:
                ch.status = ChallengeStatus.RUNNING.value
                ch.updated_at = now

        claimed_job_id = target_job.id
        claimed_session_id = target_job.session_id
        claimed_world_epoch = target_job.world_epoch
        claimed_job_type = target_job.job_type
        claimed_case_id = target_job.target_case_id
        claimed_challenge_id = target_job.challenge_id
        db.commit()

    # Phase 2: Execute BoundedAgentRuntime / TaskforceManager without holding job lock
    run_id = ""
    steps_executed = 0
    total_tokens_used = 0
    model_mode_val = get_settings().model_mode.value
    is_fallback = model_mode_val != ModelMode.LLM_API.value
    stop_reason: str | None = None
    step_traces: list[Any] = []
    decision_summary = ""
    incident_ids: list[str] = []
    exec_error: str | None = None

    try:
        with maker() as exec_db:
            if claimed_job_type == SupervisorJobType.CORRELATE_INCIDENT.value:
                correlator = IncidentCorrelator(exec_db, claimed_session_id)
                corr_res = correlator.correlate_session_cases()
                incident_ids = [inc.id for inc in corr_res.incidents]
                tf_manager = TaskforceManager(exec_db, claimed_session_id)
                for inc in corr_res.incidents:
                    tf_tasks = tf_manager.assign_and_run_taskforce(inc)
                    if any(bool((t.handoff_json or {}).get("llm_invoked")) for t in tf_tasks):
                        model_mode_val = ModelMode.LLM_API.value
                        is_fallback = False
                if claimed_case_id:
                    rt = BoundedAgentRuntime(exec_db, max_steps=6, manage_transactions=True)
                    inv_res = rt.investigate_case(
                        case_id=claimed_case_id,
                        session_id=claimed_session_id,
                    )
                    if (
                        inv_res.stop_reason == "MAX_REPLANS_EXCEEDED"
                        and inv_res.steps_executed == 0
                    ):
                        c_retry = exec_db.get(Case, claimed_case_id)
                        if c_retry is not None and c_retry.state == CaseState.ESCALATED.value:
                            c_retry.state = CaseState.NEW.value
                            c_retry.version += 1
                            exec_db.commit()
                            fb_rt = BoundedAgentRuntime(
                                exec_db,
                                adapter=DeterministicDemoAdapter(),
                                max_steps=6,
                                manage_transactions=True,
                            )
                            inv_res = fb_rt.investigate_case(
                                case_id=claimed_case_id,
                                session_id=claimed_session_id,
                            )
                    run_id = inv_res.run_id
                    steps_executed = inv_res.steps_executed
                    total_tokens_used = inv_res.total_tokens_used
                    model_mode_val = inv_res.model_mode.value
                    is_fallback = inv_res.is_fallback
                    stop_reason = inv_res.stop_reason
                    step_traces = list(inv_res.step_traces)
                    decision_summary = inv_res.decision_summary
                else:
                    run_id = f"RUN-CORR-{uuid.uuid4().hex[:8].upper()}"
                    decision_summary = (
                        f"Đã gom nhóm {len(incident_ids)} sự cố chung theo bằng chứng nhân quả "
                        f"và phân công Taskforce điều tra."
                    )
            elif claimed_case_id:
                rt = BoundedAgentRuntime(exec_db, max_steps=6, manage_transactions=True)
                inv_res = rt.investigate_case(
                    case_id=claimed_case_id,
                    session_id=claimed_session_id,
                )
                if inv_res.stop_reason == "MAX_REPLANS_EXCEEDED" and inv_res.steps_executed == 0:
                    c_retry = exec_db.get(Case, claimed_case_id)
                    if c_retry is not None and c_retry.state == CaseState.ESCALATED.value:
                        c_retry.state = CaseState.NEW.value
                        c_retry.version += 1
                        exec_db.commit()
                        fb_rt = BoundedAgentRuntime(
                            exec_db,
                            adapter=DeterministicDemoAdapter(),
                            max_steps=6,
                            manage_transactions=True,
                        )
                        inv_res = fb_rt.investigate_case(
                            case_id=claimed_case_id,
                            session_id=claimed_session_id,
                        )
                run_id = inv_res.run_id
                steps_executed = inv_res.steps_executed
                total_tokens_used = inv_res.total_tokens_used
                model_mode_val = inv_res.model_mode.value
                is_fallback = inv_res.is_fallback
                stop_reason = inv_res.stop_reason
                step_traces = list(inv_res.step_traces)
                decision_summary = inv_res.decision_summary
    except Exception as exc:
        exec_error = str(exc)

    # Phase 3: Short commit transaction checking world_epoch and persisting activities & outcomes
    with maker() as fin_db:
        job = fin_db.get(SupervisorJob, claimed_job_id)
        state = ensure_supervisor_state(fin_db, claimed_session_id)
        if job is None:
            return None

        # Verify world_epoch has not been superseded by a session reset while worker was running
        if (
            state.world_epoch != claimed_world_epoch
            or job.status == SupervisorJobStatus.SUPERSEDED.value
        ):
            job.status = SupervisorJobStatus.SUPERSEDED.value
            job.error_message = "Aborted commit because session MOCK world was reset"
            job.completed_at = datetime.now(UTC)
            job.updated_at = datetime.now(UTC)
            recompute_supervisor_idle_status(fin_db, state)
            fin_db.commit()
            return {"job_id": job.id, "status": "SUPERSEDED"}

        now = datetime.now(UTC)
        if exec_error is not None:
            job.status = SupervisorJobStatus.FAILED.value
            job.error_message = exec_error[:1000]
            job.completed_at = now
            job.updated_at = now
            state.status = SupervisorStatus.DEGRADED.value
            record_activity(
                fin_db,
                session_id=claimed_session_id,
                world_epoch=state.world_epoch,
                challenge_id=claimed_challenge_id,
                case_id=claimed_case_id,
                job_id=job.id,
                kind="DECISION_FINALIZED",
                title_vi="Tác vụ điều tra gặp lỗi kỹ thuật — đã dừng an toàn",
                detail_vi=exec_error[:400],
                metadata_json={"job_id": job.id, "error": exec_error[:400]},
            )
            fin_db.commit()
            return {"job_id": job.id, "status": "FAILED", "error": exec_error}

        job.status = SupervisorJobStatus.COMPLETED.value
        job.completed_at = now
        job.updated_at = now
        job.result_json = {
            "run_id": run_id,
            "steps_executed": steps_executed,
            "total_tokens_used": total_tokens_used,
            "model_mode": model_mode_val,
            "is_fallback": is_fallback,
            "incident_ids": incident_ids,
        }
        state.jobs_processed_count += 1
        state.updated_at = now

        _persist_run_activities_and_challenge_outcome(
            fin_db,
            job=job,
            state=state,
            run_id=run_id,
            steps_executed=steps_executed,
            total_tokens_used=total_tokens_used,
            model_mode_val=model_mode_val,
            is_fallback=is_fallback,
            stop_reason=stop_reason,
            step_traces=step_traces,
            decision_summary=decision_summary,
            incident_ids=incident_ids,
        )
        recompute_supervisor_idle_status(fin_db, state)
        fin_db.commit()
        return {
            "job_id": job.id,
            "status": job.status,
            "run_id": run_id,
            "case_id": claimed_case_id,
            "challenge_id": claimed_challenge_id,
            "supervisor_status": state.status,
        }


def drain_session_supervisor_jobs(
    session_id: str,
    *,
    max_jobs: int = 5,
    force_when_paused: bool = False,
) -> list[dict[str, Any]]:
    """Process up to `max_jobs` queued Supervisor jobs for a session."""
    results: list[dict[str, Any]] = []
    for _ in range(max(1, max_jobs)):
        res = process_next_supervisor_job(
            session_id=session_id,
            force_when_paused=force_when_paused,
        )
        if res is None:
            break
        results.append(res)
    return results
