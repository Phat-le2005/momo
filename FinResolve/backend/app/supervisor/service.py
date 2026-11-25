"""Event-driven Autonomous Supervisor with fenced queue, budgets, and telemetry.

Hard Domain Invariants (Prompt 09 & Prompt 09.1):
1. True Async Separation:
   - HTTP routes enqueue durable `SupervisorJob` rows and return immediately.
   - Dedicated standalone worker (`python -m app.supervisor.worker`) claims and executes jobs;
     embedded worker is disabled by default (`ENABLE_EMBEDDED_SUPERVISOR_WORKER=false`) to prevent
     unintended duplication.
2. Fenced Worker Reliability:
   - Atomic job claim via `SELECT ... FOR UPDATE SKIP LOCKED` with unique `claim_token`.
   - Periodic lease renewal + `SupervisorWorker` heartbeat tracking.
   - Bounded retries with exponential backoff (`next_retry_at`).
   - Database-enforced active deduplication (`uq_supervisor_jobs_active_dedupe`) and bounded queue
     capacity across all job types.
3. Cooperative Cancellation, Reset & Session Expiry:
   - `QUEUED` jobs cancel immediately (`CANCELLED`); `RUNNING` jobs enter `CANCELLING`
     (`cancel_requested_at`) and stop cleanly at the next execution boundary without overwriting
     `CANCELLED` or `SUPERSEDED` with `COMPLETED`.
   - `world_epoch` and `DemoSession.is_active` are verified at claim, guard checkpoints, and commit.
4. Evidence-Grounded Conclusions & Honest Model Telemetry:
   - Conclusions derive strictly from actual tool observations, RiskSignals, ActionProposals,
     and Verifier outcomes — NEVER from challenge_type or preset_id.
   - Actual LLM attempts, successes, failures, timeouts, fallbacks, and token usage are recorded
     at the ModelAdapter boundary with enforced per-session call & token budgets.
"""

from __future__ import annotations

import os
import socket
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, selectinload

from app.ai_core.model_adapter import DeterministicDemoAdapter
from app.ai_core.runtime import BoundedAgentRuntime, recover_stale_investigation
from app.ai_core.telemetry import (
    BudgetExhaustedError,
    JobCancelledError,
    JobSupersededError,
    LlmInvocationRecord,
    RuntimeExecutionContext,
    reset_execution_context,
    set_execution_context,
)
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
    EvidenceKind,
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
    SupervisorWorker,
)
from app.policy.executor import ActionExecutor

DEFAULT_JOB_LEASE_SECONDS = 30
MAX_QUEUE_DEPTH_PER_SESSION = 25
CONSERVATIVE_LLM_TOKEN_RESERVATION = 1000
UNKNOWN_USAGE_FALLBACK_TOKENS = 500


class _JobHeartbeatThread(threading.Thread):
    """Independent periodic heartbeat thread that renews job lease and worker heartbeat
    during external I/O using short-lived DB sessions without holding open transactions."""

    def __init__(
        self,
        *,
        job_id: str,
        session_id: str,
        claim_token: str,
        world_epoch: int,
        worker_id: str,
        worker_mode: str,
    ) -> None:
        super().__init__(name=f"finresolve-hb-{job_id[:12]}", daemon=True)
        self._job_id = job_id
        self._session_id = session_id
        self._claim_token = claim_token
        self._world_epoch = world_epoch
        self._worker_id = worker_id
        self._worker_mode = worker_mode
        self._stop_event = threading.Event()
        self.ownership_lost = threading.Event()
        self.cancel_detected = threading.Event()
        self.renewals_count = 0

    def stop(self) -> None:
        self._stop_event.set()

    def _renew_once(self) -> bool:
        settings = get_settings()
        maker = get_sessionmaker()
        now = datetime.now(UTC)
        try:
            with maker() as hdb:
                sess = hdb.get(DemoSession, self._session_id)
                if sess is None or not sess.is_active:
                    self.ownership_lost.set()
                    return False

                state = hdb.get(SupervisorState, self._session_id)
                if state is None or state.world_epoch != self._world_epoch:
                    self.ownership_lost.set()
                    return False

                job = hdb.scalar(
                    select(SupervisorJob)
                    .where(SupervisorJob.id == self._job_id)
                    .with_for_update(skip_locked=True)
                )
                if job is None:
                    exists = hdb.scalar(
                        select(SupervisorJob.id).where(SupervisorJob.id == self._job_id)
                    )
                    hdb.rollback()
                    if exists is None:
                        self.ownership_lost.set()
                        return False
                    return True
                if job.claim_token != self._claim_token or job.worker_id != self._worker_id:
                    self.ownership_lost.set()
                    hdb.rollback()
                    return False

                if (
                    job.status
                    in (
                        SupervisorJobStatus.CANCELLING.value,
                        SupervisorJobStatus.CANCELLED.value,
                    )
                    or job.cancel_requested_at is not None
                ):
                    self.cancel_detected.set()
                    self.ownership_lost.set()
                    hdb.rollback()
                    return False

                if job.status != SupervisorJobStatus.RUNNING.value:
                    self.ownership_lost.set()
                    hdb.rollback()
                    return False

                if job.locked_until is not None and job.locked_until <= now:
                    # Never renew an already-expired lease that another worker may reclaim
                    self.ownership_lost.set()
                    hdb.rollback()
                    return False

                job.heartbeat_at = now
                job.locked_until = now + timedelta(seconds=settings.supervisor_job_lease_seconds)
                job.updated_at = now
                upsert_worker_heartbeat(
                    hdb,
                    worker_id=self._worker_id,
                    worker_mode=self._worker_mode,
                    status="BUSY",
                    active_job_id=self._job_id,
                )
                hdb.commit()
                self.renewals_count += 1
                return True
        except Exception:  # noqa: BLE001
            return True

    def run(self) -> None:
        interval = max(0.1, float(get_settings().supervisor_heartbeat_interval_seconds))
        while not self._stop_event.wait(timeout=interval):
            if not self._renew_once():
                break


def initial_llm_execution_state() -> str:
    settings = get_settings()
    if settings.model_mode == ModelMode.LLM_API:
        return LlmExecutionState.LLM_API_CONFIGURED.value
    return LlmExecutionState.DEMO_RULES_FALLBACK.value


def upsert_worker_heartbeat(
    db: Session,
    *,
    worker_id: str,
    worker_mode: str = "STANDALONE",
    status: str = "IDLE",
    active_job_id: str | None = None,
) -> SupervisorWorker:
    """Register or refresh a Supervisor worker's heartbeat row in PostgreSQL."""
    now = datetime.now(UTC)
    worker = db.get(SupervisorWorker, worker_id)
    if worker is None:
        worker = SupervisorWorker(
            worker_id=worker_id,
            worker_mode=worker_mode,
            hostname=socket.gethostname()[:120],
            pid=os.getpid(),
            status=status,
            active_job_id=active_job_id,
            last_heartbeat_at=now,
            started_at=now,
            updated_at=now,
        )
        db.add(worker)
    else:
        worker.worker_mode = worker_mode
        worker.status = status
        worker.active_job_id = active_job_id
        worker.last_heartbeat_at = now
        worker.updated_at = now
    db.flush()
    return worker


def get_worker_health(db: Session) -> dict[str, Any]:
    """Compute real worker health from `supervisor_workers` heartbeats (never fabricated)."""
    ttl = get_settings().worker_heartbeat_ttl_seconds
    cutoff = datetime.now(UTC) - timedelta(seconds=ttl)
    workers = list(
        db.scalars(
            select(SupervisorWorker).order_by(SupervisorWorker.last_heartbeat_at.desc())
        ).all()
    )
    healthy_workers = [
        w for w in workers if w.status != "STOPPED" and w.last_heartbeat_at >= cutoff
    ]
    latest_hb = workers[0].last_heartbeat_at if workers else None
    return {
        "worker_healthy": len(healthy_workers) > 0,
        "active_workers_count": len(healthy_workers),
        "latest_worker_heartbeat_at": latest_hb,
        "worker_ids": [w.worker_id for w in healthy_workers],
        "heartbeat_ttl_seconds": ttl,
    }


def ensure_supervisor_state(db: Session, session_id: str) -> SupervisorState:
    """Get or create the session-scoped SupervisorState row."""
    state = db.get(SupervisorState, session_id)
    if state is not None:
        if state.llm_attempts_count == 0 and state.last_llm_failed_at is None:
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
        activity_seq=0,
        active_job_id=None,
        active_case_id=None,
        llm_execution_state=initial_llm_execution_state(),
        llm_calls_count=0,
        llm_attempts_count=0,
        llm_dispatched_count=0,
        llm_reserved_calls=0,
        llm_reserved_tokens=0,
        llm_succeeded_count=0,
        llm_failed_count=0,
        llm_timeout_count=0,
        llm_fallback_count=0,
        llm_tokens_unknown_count=0,
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
            "Chế độ Autopilot theo sự kiện đang bật — hệ thống chỉ kích hoạt Agent khi "
            "phát hiện khiếu nại, sự kiện nghiệp vụ hoặc biến động bất thường."
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
    """Append a concurrency-safe, ordered activity event using locked `activity_seq`."""
    state = db.scalar(
        select(SupervisorState).where(SupervisorState.session_id == session_id).with_for_update()
    )
    if state is None:
        state = ensure_supervisor_state(db, session_id)
    max_existing = (
        db.scalar(
            select(func.max(SupervisorActivity.seq)).where(
                SupervisorActivity.session_id == session_id
            )
        )
        or 0
    )
    next_seq = max(int(state.activity_seq), int(max_existing)) + 1
    state.activity_seq = next_seq

    act = SupervisorActivity(
        id=f"ACT-{uuid.uuid4().hex[:12].upper()}",
        seq=next_seq,
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
    """Determine accurate Supervisor status from DB state (queue, proposals, cases)."""
    if not state.autopilot_enabled:
        state.status = SupervisorStatus.PAUSED.value
        state.active_job_id = None
        state.active_case_id = None
        return state.status

    active_job = db.scalar(
        select(SupervisorJob)
        .where(
            SupervisorJob.session_id == state.session_id,
            SupervisorJob.world_epoch == state.world_epoch,
            SupervisorJob.status.in_(
                [
                    SupervisorJobStatus.RUNNING.value,
                    SupervisorJobStatus.CANCELLING.value,
                    SupervisorJobStatus.QUEUED.value,
                ]
            ),
        )
        .order_by(SupervisorJob.priority.asc(), SupervisorJob.created_at.asc())
    )
    if active_job is not None:
        state.status = SupervisorStatus.INVESTIGATING.value
        state.active_job_id = active_job.id
        state.active_case_id = active_job.target_case_id
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

    waiting_evidence = (
        db.scalar(
            select(func.count(Case.id)).where(
                Case.session_id == state.session_id,
                Case.state == CaseState.WAITING_EVIDENCE.value,
            )
        )
        or 0
    )
    if waiting_evidence > 0:
        state.status = SupervisorStatus.WAITING_EVIDENCE.value
        return state.status

    if state.llm_execution_state == LlmExecutionState.BUDGET_EXHAUSTED.value:
        state.status = SupervisorStatus.DEGRADED.value
        return state.status

    state.status = SupervisorStatus.MONITORING.value
    return state.status


def _enqueue_bounded_job(
    db: Session,
    *,
    session_id: str,
    job_type: str,
    dedupe_key: str,
    target_case_id: str | None = None,
    challenge_id: str | None = None,
    priority: int = 100,
    max_attempts: int = 2,
    payload_json: dict[str, Any] | None = None,
    activity_title_vi: str,
    activity_detail_vi: str | None = None,
) -> SupervisorJob:
    """Enforce queue capacity & DB-backed deduplication for EVERY SupervisorJob type."""
    session_obj = db.get(DemoSession, session_id)
    if session_obj is None or not session_obj.is_active:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "Session is expired or inactive; cannot enqueue new Supervisor jobs",
            status_code=403,
        )

    state = ensure_supervisor_state(db, session_id)
    existing = db.scalar(
        select(SupervisorJob).where(
            SupervisorJob.session_id == session_id,
            SupervisorJob.world_epoch == state.world_epoch,
            SupervisorJob.dedupe_key == dedupe_key,
            SupervisorJob.status.in_(
                [
                    SupervisorJobStatus.QUEUED.value,
                    SupervisorJobStatus.RUNNING.value,
                    SupervisorJobStatus.CANCELLING.value,
                ]
            ),
        )
    )
    if existing is not None:
        return existing

    queue_depth = (
        db.scalar(
            select(func.count(SupervisorJob.id)).where(
                SupervisorJob.session_id == session_id,
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
    if queue_depth >= MAX_QUEUE_DEPTH_PER_SESSION:
        raise AppError(
            ErrorCode.RATE_LIMITED,
            f"Supervisor queue depth limit reached ({MAX_QUEUE_DEPTH_PER_SESSION} active jobs)",
            status_code=429,
        )

    now = datetime.now(UTC)
    job = SupervisorJob(
        id=f"JOB-{uuid.uuid4().hex[:10].upper()}",
        session_id=session_id,
        world_epoch=state.world_epoch,
        job_type=job_type,
        dedupe_key=dedupe_key,
        target_case_id=target_case_id,
        challenge_id=challenge_id,
        status=SupervisorJobStatus.QUEUED.value,
        priority=priority,
        attempt_count=0,
        max_attempts=max_attempts,
        payload_json=dict(payload_json or {}),
        result_json={},
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        # Concurrent enqueue hit `uq_supervisor_jobs_active_dedupe` -> return existing active job
        dup = db.scalar(
            select(SupervisorJob).where(
                SupervisorJob.session_id == session_id,
                SupervisorJob.world_epoch == state.world_epoch,
                SupervisorJob.dedupe_key == dedupe_key,
                SupervisorJob.status.in_(
                    [
                        SupervisorJobStatus.QUEUED.value,
                        SupervisorJobStatus.RUNNING.value,
                        SupervisorJobStatus.CANCELLING.value,
                    ]
                ),
            )
        )
        if dup is not None:
            return dup
        raise

    record_activity(
        db,
        session_id=session_id,
        world_epoch=state.world_epoch,
        challenge_id=challenge_id,
        case_id=target_case_id,
        job_id=job.id,
        kind="JOB_QUEUED",
        title_vi=activity_title_vi,
        detail_vi=activity_detail_vi,
        metadata_json={"job_id": job.id, "job_type": job_type, "case_id": target_case_id},
    )
    recompute_supervisor_idle_status(db, state)
    return job


def enqueue_case_investigation_job(
    db: Session,
    *,
    session_id: str,
    case_id: str,
    challenge_id: str | None = None,
    reason_vi: str = "Phát hiện hồ sơ ngoại lệ mới — đưa vào hàng đợi điều tra",
    priority: int = 100,
    payload_extra: dict[str, Any] | None = None,
) -> SupervisorJob:
    case_obj = db.scalar(select(Case).where(Case.id == case_id, Case.session_id == session_id))
    if case_obj is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Case '{case_id}' not found", status_code=404)

    return _enqueue_bounded_job(
        db,
        session_id=session_id,
        job_type=SupervisorJobType.INVESTIGATE_CASE.value,
        dedupe_key=f"case:{case_id}:v{case_obj.version}",
        target_case_id=case_id,
        challenge_id=challenge_id,
        priority=priority,
        payload_json={
            "case_id": case_id,
            "challenge_id": challenge_id,
            **(payload_extra or {}),
        },
        activity_title_vi=reason_vi,
        activity_detail_vi=f"Mục tiêu: {case_obj.goal}",
    )


def enqueue_incident_correlation_job(
    db: Session,
    *,
    session_id: str,
    challenge_id: str | None = None,
    representative_case_id: str | None = None,
    reason_vi: str = (
        "Phát hiện cụm đơn hàng lỗi đồng bộ — đưa vào hàng đợi tương quan Incident & Taskforce"
    ),
) -> SupervisorJob:
    state = ensure_supervisor_state(db, session_id)
    return _enqueue_bounded_job(
        db,
        session_id=session_id,
        job_type=SupervisorJobType.CORRELATE_INCIDENT.value,
        dedupe_key=f"correlate:{challenge_id or session_id}:e{state.world_epoch}",
        target_case_id=representative_case_id,
        challenge_id=challenge_id,
        priority=90,
        payload_json={
            "challenge_id": challenge_id,
            "representative_case_id": representative_case_id,
        },
        activity_title_vi=reason_vi,
        activity_detail_vi=("Worker sẽ gom nhóm theo bằng chứng nhân quả và phân công Taskforce."),
    )


def enqueue_approved_action_execution_job(
    db: Session,
    *,
    session_id: str,
    proposal_id: str,
    case_id: str | None,
    action_type: str,
) -> SupervisorJob:
    """Durably enqueue automatic execution + post-commit Verifier after explicit human approval."""
    return _enqueue_bounded_job(
        db,
        session_id=session_id,
        job_type=SupervisorJobType.EXECUTE_APPROVED_ACTION.value,
        dedupe_key=f"exec_proposal:{proposal_id}",
        target_case_id=case_id,
        priority=50,
        max_attempts=2,
        payload_json={
            "proposal_id": proposal_id,
            "idempotency_key": f"IDEMP-AUTO-EXEC-{proposal_id}",
            "action_type": action_type,
        },
        activity_title_vi=(
            f"Đã ghi nhận phê duyệt {action_type} — xếp hàng đợi tự động thực thi & kiểm chứng"
        ),
        activity_detail_vi=(
            "Worker sẽ kiểm tra lại Policy, thực thi idempotent và chạy Verifier độc lập."
        ),
    )


def request_job_cancellation(
    db: Session,
    *,
    session_id: str,
    job_id: str,
    reason: str = "Cancelled by user",
) -> SupervisorJob:
    """Cooperatively cancel a job: QUEUED -> CANCELLED immediately; RUNNING -> CANCELLING."""
    job = db.scalar(
        select(SupervisorJob)
        .where(SupervisorJob.id == job_id, SupervisorJob.session_id == session_id)
        .with_for_update()
    )
    if job is None:
        raise AppError(ErrorCode.NOT_FOUND, f"Job '{job_id}' not found", status_code=404)

    now = datetime.now(UTC)
    state = ensure_supervisor_state(db, session_id)
    if job.status == SupervisorJobStatus.QUEUED.value:
        job.status = SupervisorJobStatus.CANCELLED.value
        job.cancel_requested_at = now
        job.completed_at = now
        job.error_message = reason
        job.updated_at = now
        record_activity(
            db,
            session_id=session_id,
            world_epoch=state.world_epoch,
            challenge_id=job.challenge_id,
            case_id=job.target_case_id,
            job_id=job.id,
            kind="JOB_CANCELLED",
            title_vi=f"Đã hủy ngay tác vụ đang chờ ({job.id})",
            detail_vi=reason,
            metadata_json={"job_id": job.id, "status": "CANCELLED"},
        )
    elif job.status == SupervisorJobStatus.RUNNING.value:
        job.status = SupervisorJobStatus.CANCELLING.value
        job.cancel_requested_at = now
        job.error_message = f"Cancellation requested: {reason}"
        job.updated_at = now
        record_activity(
            db,
            session_id=session_id,
            world_epoch=state.world_epoch,
            challenge_id=job.challenge_id,
            case_id=job.target_case_id,
            job_id=job.id,
            kind="JOB_CANCELLED",
            title_vi=f"Đã gửi yêu cầu dừng hợp tác cho tác vụ đang chạy ({job.id})",
            detail_vi=(
                "Worker sẽ dừng an toàn trước bước/công cụ tiếp theo; "
                "mọi hành động đã commit trước đó vẫn giữ nguyên kiểm toán."
            ),
            metadata_json={"job_id": job.id, "status": "CANCELLING"},
        )
    recompute_supervisor_idle_status(db, state)
    return job


def bump_world_epoch_on_reset(db: Session, session_id: str) -> int:
    """Invalidate all queued/running Supervisor jobs when a session's MOCK world is reset."""
    state = ensure_supervisor_state(db, session_id)
    state.world_epoch += 1
    state.active_job_id = None
    state.active_case_id = None
    state.updated_at = datetime.now(UTC)
    now = datetime.now(UTC)

    db.execute(
        update(SupervisorJob)
        .where(
            SupervisorJob.session_id == session_id,
            SupervisorJob.status.in_(
                [
                    SupervisorJobStatus.QUEUED.value,
                    SupervisorJobStatus.RUNNING.value,
                    SupervisorJobStatus.CANCELLING.value,
                ]
            ),
        )
        .values(
            status=SupervisorJobStatus.SUPERSEDED.value,
            error_message="Superseded by session MOCK world reset",
            completed_at=now,
            updated_at=now,
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
            "Không nhận thêm tác vụ mới; tác vụ đang chạy dở (nếu có) sẽ hoàn tất an toàn "
            "hoặc dừng theo yêu cầu hủy."
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
        detail_vi="Worker nền đã được đánh thức để tiếp tục xử lý các tác vụ hợp lệ.",
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
    stmt = select(SupervisorJob).where(
        SupervisorJob.status.in_(
            [SupervisorJobStatus.RUNNING.value, SupervisorJobStatus.CANCELLING.value]
        )
    )
    if session_id is not None:
        stmt = stmt.where(SupervisorJob.session_id == session_id)
    if stale_after_seconds is None or stale_after_seconds > 0:
        stmt = stmt.where(SupervisorJob.locked_until <= now)

    try:
        stale_jobs = list(db.scalars(stmt.with_for_update(skip_locked=True)).all())
    except OperationalError:
        db.rollback()
        return []

    recovered_ids: list[str] = []
    for job in stale_jobs:
        state = ensure_supervisor_state(db, job.session_id)
        sess = db.get(DemoSession, job.session_id)
        if sess is None or not sess.is_active or job.world_epoch != state.world_epoch:
            job.status = SupervisorJobStatus.SUPERSEDED.value
            job.claim_token = None
            job.completed_at = now
            job.updated_at = now
            job.error_message = "Superseded during stale-lease recovery (expired/reset session)"
            recovered_ids.append(job.id)
            continue

        if job.status == SupervisorJobStatus.CANCELLING.value or job.cancel_requested_at:
            job.status = SupervisorJobStatus.CANCELLED.value
            job.claim_token = None
            job.completed_at = now
            job.updated_at = now
            if job.challenge_id:
                ch_cancel = db.scalar(
                    select(ChallengeRun).where(
                        ChallengeRun.id == job.challenge_id,
                        ChallengeRun.session_id == job.session_id,
                    )
                )
                if ch_cancel is not None and ch_cancel.status in (
                    ChallengeStatus.QUEUED.value,
                    ChallengeStatus.RUNNING.value,
                    ChallengeStatus.CANCELLING.value,
                ):
                    ch_cancel.status = ChallengeStatus.CANCELLED.value
                    ch_cancel.safety_verdict = "CANCELLED_BY_USER"
                    ch_cancel.updated_at = now
            recovered_ids.append(job.id)
            recompute_supervisor_idle_status(db, state)
            continue

        if job.target_case_id:
            recover_stale_investigation(
                db,
                case_id=job.target_case_id,
                session_id=job.session_id,
                stale_after_seconds=0,
            )
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
            backoff_s = min(15, 2 * max(1, job.attempt_count))
            job.status = SupervisorJobStatus.QUEUED.value
            job.worker_id = None
            job.claim_token = None
            job.locked_until = None
            job.next_retry_at = now + timedelta(seconds=backoff_s)
            job.error_message = (
                f"Worker lease expired; safely recovered and scheduled retry (+{backoff_s}s)"
            )
            job.updated_at = now
            record_activity(
                db,
                session_id=job.session_id,
                world_epoch=state.world_epoch,
                challenge_id=job.challenge_id,
                case_id=job.target_case_id,
                job_id=job.id,
                kind="WORKER_RECOVERED",
                title_vi="Phát hiện worker trước bị gián đoạn — đã khôi phục an toàn",
                detail_vi=(
                    f"Tác vụ {job.id} được đưa lại vào hàng đợi (lần thử {job.attempt_count}/"
                    f"{job.max_attempts}), không nhân đôi tác động tài chính."
                ),
                metadata_json={
                    "job_id": job.id,
                    "attempt_count": job.attempt_count,
                    "backoff_seconds": backoff_s,
                },
            )
        else:
            job.status = SupervisorJobStatus.FAILED.value
            job.claim_token = None
            job.completed_at = now
            job.updated_at = now
            job.error_message = "Worker interrupted and exceeded bounded max_attempts"
            state.status = SupervisorStatus.DEGRADED.value
            if job.challenge_id:
                ch_fail = db.scalar(
                    select(ChallengeRun).where(
                        ChallengeRun.id == job.challenge_id,
                        ChallengeRun.session_id == job.session_id,
                    )
                )
                if ch_fail is not None and ch_fail.status in (
                    ChallengeStatus.QUEUED.value,
                    ChallengeStatus.RUNNING.value,
                    ChallengeStatus.CANCELLING.value,
                ):
                    ch_fail.status = ChallengeStatus.FAILED.value
                    ch_fail.safety_verdict = "WORKER_INTERRUPTED_FAILED"
                    ch_fail.updated_at = now
            record_activity(
                db,
                session_id=job.session_id,
                world_epoch=state.world_epoch,
                challenge_id=job.challenge_id,
                case_id=job.target_case_id,
                job_id=job.id,
                kind="WORKER_RECOVERED",
                title_vi="Tác vụ vượt số lần thử tối đa sau khi gián đoạn — dừng an toàn",
                detail_vi="Không có thay đổi tài chính trái phép nào được thực thi.",
                metadata_json={"job_id": job.id, "status": "FAILED"},
            )
        recovered_ids.append(job.id)
        recompute_supervisor_idle_status(db, state)

    if recovered_ids:
        db.commit()
    return recovered_ids


def _build_evidence_grounded_conclusion(
    *,
    case_obj: Case,
    decision_summary: str,
) -> tuple[str, str, str]:
    """Derive conclusion, safety_verdict, and challenge status STRICTLY from actual DB evidence.

    Never inspects `challenge_type` or `preset_id`. Identical challenge types with different
    ledger facts produce genuinely different outcomes.
    """
    proposals = list(case_obj.action_proposals or [])
    risks = list(case_obj.risk_signals or [])
    verifications = list(case_obj.verifications or [])
    evidences = list(case_obj.evidence or [])

    passed_verif = any(v.verdict == "PASSED" for v in verifications)
    if case_obj.state == CaseState.RESOLVED.value and passed_verif:
        return (
            "Hành động đã được phê duyệt, thực thi và Verifier kiểm chứng hậu điều kiện "
            "thành công (PASSED) — hồ sơ đã chuyển sang RESOLVED.",
            "VERIFIED_RESOLVED",
            ChallengeStatus.COMPLETED.value,
        )

    pending_props = [p for p in proposals if p.status == ActionProposalStatus.PROPOSED.value]
    if case_obj.state == CaseState.WAITING_APPROVAL.value or pending_props:
        prop = pending_props[0] if pending_props else proposals[0]
        if prop.action_type == "ORDER_SYNC":
            return (
                "AI đã đối chiếu sổ cái thanh toán MOCK xác thực và đề xuất đồng bộ trạng thái "
                "đơn hàng — đang dừng chờ con người phê duyệt.",
                "WAITING_HUMAN_APPROVAL",
                ChallengeStatus.WAITING_APPROVAL.value,
            )
        if prop.action_type == "REFUND_EXECUTE":
            return (
                "AI đã kiểm tra số dư hoàn tiền khả dụng trên sổ cái và đề xuất hoàn tiền hợp lệ "
                "— đang dừng chờ con người phê duyệt.",
                "WAITING_HUMAN_APPROVAL",
                ChallengeStatus.WAITING_APPROVAL.value,
            )
        return (
            f"AI đề xuất hành động {prop.action_type} dựa trên bằng chứng xác thực — "
            "đang chờ con người phê duyệt.",
            "WAITING_HUMAN_APPROVAL",
            ChallengeStatus.WAITING_APPROVAL.value,
        )

    hold_risks = [r for r in risks if r.response == "HOLD_ACTION"]
    if hold_risks:
        exp = str((hold_risks[0].details_json or {}).get("explanation_vi") or "")
        summary = (
            f"Risk Engine đã tạm giữ hành động (HOLD_ACTION): {exp}"
            if exp
            else (
                "Risk Engine đã tạm giữ hành động (HOLD_ACTION) theo quy tắc an toàn tài chính; "
                "không quy kết gian lận và không thực thi ghi nhận trái phép."
            )
        )
        return summary, "ACTION_HELD_BY_RISK", ChallengeStatus.BLOCKED_SAFELY.value

    review_risks = [r for r in risks if r.response == "REVIEW"]
    if review_risks:
        return (
            "Phát hiện tín hiệu bất thường cần nhân viên xem xét (REVIEW) — chưa thực hiện "
            "thay đổi tài chính nào.",
            "REVIEW_REQUIRED",
            ChallengeStatus.COMPLETED.value,
        )

    has_unknown_ev = any(e.kind == EvidenceKind.UNKNOWN.value for e in evidences)
    if case_obj.state == CaseState.UNKNOWN_OUTCOME.value or has_unknown_ev:
        return (
            "Kết quả truy vấn từ nguồn ngoài chưa xác định (UNKNOWN) — hệ thống giữ nguyên "
            "trạng thái an toàn và không thử lại lệnh ghi mù quáng.",
            "UNKNOWN",
            ChallengeStatus.COMPLETED.value,
        )

    if case_obj.state == CaseState.WAITING_EVIDENCE.value:
        obs_facts = [e for e in evidences if e.kind == EvidenceKind.OBSERVED_FACT.value]
        not_found = any((e.payload_json or {}).get("status") == "NOT_FOUND" for e in obs_facts)
        if not_found:
            return (
                "Không tìm thấy giao dịch khớp trong sổ cái xác thực — AI không xác nhận "
                "thanh toán và yêu cầu bổ sung bằng chứng (WAITING_EVIDENCE).",
                "WAITING_EVIDENCE",
                ChallengeStatus.BLOCKED_SAFELY.value,
            )
        return (
            "Bằng chứng xác thực hiện có không khớp hoặc chưa đủ điều kiện hành động — "
            "AI không xác nhận giao dịch và chuyển sang WAITING_EVIDENCE.",
            "WAITING_EVIDENCE",
            ChallengeStatus.BLOCKED_SAFELY.value,
        )

    if case_obj.state == CaseState.ESCALATED.value:
        return (
            decision_summary
            or "Hồ sơ đã được chuyển cấp xử lý (ESCALATED) do chưa đủ bằng chứng hoặc "
            "vượt giới hạn điều tra an toàn.",
            "ESCALATED",
            ChallengeStatus.COMPLETED.value,
        )

    return (
        decision_summary
        or "AI đã hoàn tất đối chiếu dữ liệu xác thực và không thực hiện thay đổi tài chính.",
        "OBSERVED_NO_ACTION",
        ChallengeStatus.COMPLETED.value,
    )


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


def _build_execution_context(
    *,
    job_id: str,
    session_id: str,
    claim_token: str,
    world_epoch: int,
    worker_id: str,
    worker_mode: str,
    challenge_id: str | None,
    case_id: str | None,
) -> RuntimeExecutionContext:
    """Create the context-local guard, atomic budget enforcer, and live telemetry recorder."""
    maker = get_sessionmaker()
    pending_reservations: list[int] = []
    res_lock = threading.Lock()

    def _check_guard(stage: str) -> None:
        _ = stage
        with maker() as gdb:
            job = gdb.get(SupervisorJob, job_id)
            state = gdb.get(SupervisorState, session_id)
            sess = gdb.get(DemoSession, session_id)
            if sess is None or not sess.is_active:
                raise JobSupersededError("DemoSession expired or revoked during job execution")
            if state is None or state.world_epoch != world_epoch:
                raise JobSupersededError("Session MOCK world was reset during job execution")
            if job is None or job.claim_token != claim_token:
                raise JobSupersededError("Worker lost fencing claim_token ownership")
            if (
                job.status == SupervisorJobStatus.CANCELLING.value
                or job.status == SupervisorJobStatus.CANCELLED.value
                or job.cancel_requested_at is not None
            ):
                raise JobCancelledError("Cooperative cancellation requested for running job")
            if job.status == SupervisorJobStatus.SUPERSEDED.value:
                raise JobSupersededError("Job was superseded")

    def _before_llm_call() -> None:
        _check_guard("before_llm_call")
        now = datetime.now(UTC)
        with maker() as bdb:
            state = bdb.scalar(
                select(SupervisorState)
                .where(SupervisorState.session_id == session_id)
                .with_for_update()
            )
            if state is None:
                state = ensure_supervisor_state(bdb, session_id)
            effective_calls = state.llm_attempts_count + state.llm_reserved_calls
            effective_tokens = state.llm_tokens_used + state.llm_reserved_tokens
            if (
                effective_calls >= state.llm_budget_calls
                or effective_tokens >= state.llm_budget_tokens
            ):
                state.llm_execution_state = LlmExecutionState.BUDGET_EXHAUSTED.value
                state.status = SupervisorStatus.DEGRADED.value
                state.last_llm_error = (
                    f"Session LLM budget exhausted "
                    f"(calls={effective_calls}/{state.llm_budget_calls}, "
                    f"tokens={effective_tokens}/{state.llm_budget_tokens})"
                )
                state.updated_at = now
                record_activity(
                    bdb,
                    session_id=session_id,
                    world_epoch=world_epoch,
                    challenge_id=challenge_id,
                    case_id=case_id,
                    job_id=job_id,
                    kind="BUDGET_EXHAUSTED",
                    title_vi="Đã đạt giới hạn ngân sách LLM của phiên — dừng gọi mô hình an toàn",
                    detail_vi=state.last_llm_error,
                    metadata_json={
                        "llm_attempts_count": state.llm_attempts_count,
                        "llm_reserved_calls": state.llm_reserved_calls,
                        "llm_budget_calls": state.llm_budget_calls,
                        "llm_tokens_used": state.llm_tokens_used,
                        "llm_reserved_tokens": state.llm_reserved_tokens,
                        "llm_budget_tokens": state.llm_budget_tokens,
                    },
                )
                bdb.commit()
                raise BudgetExhaustedError(state.last_llm_error)

            remaining_tokens = max(0, state.llm_budget_tokens - effective_tokens)
            reserve_tok = min(CONSERVATIVE_LLM_TOKEN_RESERVATION, max(1, remaining_tokens))
            state.llm_reserved_calls += 1
            state.llm_reserved_tokens += reserve_tok
            state.llm_dispatched_count += 1
            state.llm_execution_state = LlmExecutionState.LLM_CALL_STARTED.value
            state.updated_at = now
            with res_lock:
                pending_reservations.append(reserve_tok)
            record_activity(
                bdb,
                session_id=session_id,
                world_epoch=world_epoch,
                challenge_id=challenge_id,
                case_id=case_id,
                job_id=job_id,
                kind="LLM_CALL_STARTED",
                title_vi="Đang gọi mô hình LLM suy luận (bước có giới hạn ngân sách)",
                detail_vi=(
                    f"Lượt gọi thứ {state.llm_attempts_count + state.llm_reserved_calls}/"
                    f"{state.llm_budget_calls}."
                ),
                metadata_json={
                    "attempt_number": state.llm_attempts_count + state.llm_reserved_calls,
                    "reserved_tokens": reserve_tok,
                },
            )
            bdb.commit()

    def _release_llm_reservation() -> None:
        with res_lock:
            reserve_tok = pending_reservations.pop() if pending_reservations else 0
        if reserve_tok <= 0:
            return
        with maker() as rdb:
            state = rdb.scalar(
                select(SupervisorState)
                .where(SupervisorState.session_id == session_id)
                .with_for_update()
            )
            if state is not None:
                state.llm_reserved_calls = max(0, state.llm_reserved_calls - 1)
                state.llm_reserved_tokens = max(0, state.llm_reserved_tokens - reserve_tok)
                state.llm_dispatched_count = max(0, state.llm_dispatched_count - 1)
                state.updated_at = datetime.now(UTC)
                rdb.commit()

    def _record_llm_outcome(rec: LlmInvocationRecord) -> None:
        now = datetime.now(UTC)
        reserve_tok = 0
        if not rec.is_fallback:
            with res_lock:
                reserve_tok = pending_reservations.pop() if pending_reservations else 0
        with maker() as rdb:
            state = rdb.scalar(
                select(SupervisorState)
                .where(SupervisorState.session_id == session_id)
                .with_for_update()
            )
            if state is None:
                state = ensure_supervisor_state(rdb, session_id)
            if rec.is_fallback:
                state.llm_fallback_count += 1
                if state.llm_execution_state not in (
                    LlmExecutionState.LLM_CALL_FAILED.value,
                    LlmExecutionState.BUDGET_EXHAUSTED.value,
                ):
                    state.llm_execution_state = LlmExecutionState.DEMO_RULES_FALLBACK.value
            else:
                if reserve_tok > 0:
                    state.llm_reserved_calls = max(0, state.llm_reserved_calls - 1)
                    state.llm_reserved_tokens = max(0, state.llm_reserved_tokens - reserve_tok)
                state.llm_attempts_count += 1
                if rec.succeeded:
                    state.llm_succeeded_count += 1
                    state.llm_calls_count += 1
                    state.last_llm_succeeded_at = now
                    state.last_llm_error = None
                    state.llm_execution_state = LlmExecutionState.LLM_CALL_SUCCEEDED.value
                    if rec.tokens_unknown or rec.total_tokens is None:
                        state.llm_tokens_unknown_count += 1
                        # Apply conservative token charge so unknown usage cannot bypass budgets
                        state.llm_tokens_used += UNKNOWN_USAGE_FALLBACK_TOKENS
                    else:
                        state.llm_tokens_used += max(0, rec.total_tokens)
                    record_activity(
                        rdb,
                        session_id=session_id,
                        world_epoch=world_epoch,
                        challenge_id=challenge_id,
                        case_id=case_id,
                        job_id=job_id,
                        kind="LLM_CALL_SUCCEEDED",
                        title_vi=f"LLM phản hồi thành công ({rec.provider}:{rec.model_name})",
                        detail_vi=(
                            "Token usage: unknown (áp mức dự phòng "
                            f"{UNKNOWN_USAGE_FALLBACK_TOKENS} tokens để bảo vệ ngân sách)."
                            if rec.tokens_unknown
                            else f"Đã dùng {rec.total_tokens} tokens."
                        ),
                        metadata_json={
                            "provider": rec.provider,
                            "model_name": rec.model_name,
                            "total_tokens": rec.total_tokens,
                            "tokens_unknown": rec.tokens_unknown,
                        },
                    )
                else:
                    state.llm_failed_count += 1
                    if rec.timed_out:
                        state.llm_timeout_count += 1
                    state.last_llm_failed_at = now
                    state.last_llm_error = (rec.error_summary or "LLM provider error")[:500]
                    state.llm_execution_state = LlmExecutionState.LLM_CALL_FAILED.value
                    record_activity(
                        rdb,
                        session_id=session_id,
                        world_epoch=world_epoch,
                        challenge_id=challenge_id,
                        case_id=case_id,
                        job_id=job_id,
                        kind="LLM_CALL_FAILED",
                        title_vi=(
                            "Lượt gọi LLM quá thời gian chờ (timeout)"
                            if rec.timed_out
                            else "Lượt gọi LLM gặp lỗi từ nhà cung cấp"
                        ),
                        detail_vi=state.last_llm_error,
                        metadata_json={
                            "provider": rec.provider,
                            "timed_out": rec.timed_out,
                        },
                    )
            state.updated_at = now
            rdb.commit()

    def _emit_activity(
        kind: str,
        title_vi: str,
        detail_vi: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> None:
        with maker() as adb:
            state = adb.get(SupervisorState, session_id)
            if state is None or state.world_epoch != world_epoch:
                return
            record_activity(
                adb,
                session_id=session_id,
                world_epoch=world_epoch,
                challenge_id=challenge_id,
                case_id=case_id,
                job_id=job_id,
                kind=kind,
                title_vi=title_vi,
                detail_vi=detail_vi,
                metadata_json=metadata_json,
            )
            adb.commit()

    return RuntimeExecutionContext(
        job_id=job_id,
        session_id=session_id,
        claim_token=claim_token,
        world_epoch=world_epoch,
        check_guard=_check_guard,
        before_llm_call=_before_llm_call,
        record_llm_outcome=_record_llm_outcome,
        emit_activity=_emit_activity,
        release_llm_reservation=_release_llm_reservation,
    )


@contextmanager
def session_budget_context(session_id: str) -> Iterator[None]:
    """Attach a lightweight session-level LLM budget context for legacy/manual API routes
    when no Supervisor worker execution context is already active."""
    from app.ai_core.telemetry import get_execution_context

    if get_execution_context() is not None:
        yield
        return

    maker = get_sessionmaker()
    pending_reservations: list[int] = []
    res_lock = threading.Lock()

    def _before_llm_call() -> None:
        now = datetime.now(UTC)
        with maker() as bdb:
            state = bdb.scalar(
                select(SupervisorState)
                .where(SupervisorState.session_id == session_id)
                .with_for_update()
            )
            if state is None:
                state = ensure_supervisor_state(bdb, session_id)
            effective_calls = state.llm_attempts_count + state.llm_reserved_calls
            effective_tokens = state.llm_tokens_used + state.llm_reserved_tokens
            if (
                effective_calls >= state.llm_budget_calls
                or effective_tokens >= state.llm_budget_tokens
            ):
                state.llm_execution_state = LlmExecutionState.BUDGET_EXHAUSTED.value
                state.last_llm_error = (
                    f"Session LLM budget exhausted "
                    f"(calls={effective_calls}/{state.llm_budget_calls}, "
                    f"tokens={effective_tokens}/{state.llm_budget_tokens})"
                )
                state.updated_at = now
                bdb.commit()
                raise BudgetExhaustedError(state.last_llm_error)

            remaining_tokens = max(0, state.llm_budget_tokens - effective_tokens)
            reserve_tok = min(CONSERVATIVE_LLM_TOKEN_RESERVATION, max(1, remaining_tokens))
            state.llm_reserved_calls += 1
            state.llm_reserved_tokens += reserve_tok
            state.llm_dispatched_count += 1
            state.llm_execution_state = LlmExecutionState.LLM_CALL_STARTED.value
            state.updated_at = now
            with res_lock:
                pending_reservations.append(reserve_tok)
            bdb.commit()

    def _release_llm_reservation() -> None:
        with res_lock:
            reserve_tok = pending_reservations.pop() if pending_reservations else 0
        if reserve_tok <= 0:
            return
        with maker() as rdb:
            state = rdb.scalar(
                select(SupervisorState)
                .where(SupervisorState.session_id == session_id)
                .with_for_update()
            )
            if state is not None:
                state.llm_reserved_calls = max(0, state.llm_reserved_calls - 1)
                state.llm_reserved_tokens = max(0, state.llm_reserved_tokens - reserve_tok)
                state.llm_dispatched_count = max(0, state.llm_dispatched_count - 1)
                state.updated_at = datetime.now(UTC)
                rdb.commit()

    def _record_llm_outcome(rec: LlmInvocationRecord) -> None:
        now = datetime.now(UTC)
        reserve_tok = 0
        if not rec.is_fallback:
            with res_lock:
                reserve_tok = pending_reservations.pop() if pending_reservations else 0
        with maker() as rdb:
            state = rdb.scalar(
                select(SupervisorState)
                .where(SupervisorState.session_id == session_id)
                .with_for_update()
            )
            if state is None:
                state = ensure_supervisor_state(rdb, session_id)
            if rec.is_fallback:
                state.llm_fallback_count += 1
            else:
                if reserve_tok > 0:
                    state.llm_reserved_calls = max(0, state.llm_reserved_calls - 1)
                    state.llm_reserved_tokens = max(0, state.llm_reserved_tokens - reserve_tok)
                state.llm_attempts_count += 1
                if rec.succeeded:
                    state.llm_succeeded_count += 1
                    state.llm_calls_count += 1
                    state.last_llm_succeeded_at = now
                    state.llm_execution_state = LlmExecutionState.LLM_CALL_SUCCEEDED.value
                    if rec.tokens_unknown or rec.total_tokens is None:
                        state.llm_tokens_unknown_count += 1
                        state.llm_tokens_used += UNKNOWN_USAGE_FALLBACK_TOKENS
                    else:
                        state.llm_tokens_used += max(0, rec.total_tokens)
                else:
                    state.llm_failed_count += 1
                    if rec.timed_out:
                        state.llm_timeout_count += 1
                    state.last_llm_failed_at = now
                    state.last_llm_error = (rec.error_summary or "LLM provider error")[:500]
                    state.llm_execution_state = LlmExecutionState.LLM_CALL_FAILED.value
            state.updated_at = now
            rdb.commit()

    def _noop_guard(_stage: str) -> None:
        return None

    def _noop_emit(
        _kind: str,
        _title: str,
        _detail: str | None = None,
        _meta: dict[str, Any] | None = None,
    ) -> None:
        return None

    ctx = RuntimeExecutionContext(
        job_id="",
        session_id=session_id,
        claim_token="",
        world_epoch=1,
        check_guard=_noop_guard,
        before_llm_call=_before_llm_call,
        record_llm_outcome=_record_llm_outcome,
        emit_activity=_noop_emit,
        release_llm_reservation=_release_llm_reservation,
    )
    tok = set_execution_context(ctx)
    try:
        yield
    finally:
        reset_execution_context(tok)


def _finalize_challenge_and_case_outcome(
    db: Session,
    *,
    job: SupervisorJob,
    state: SupervisorState,
    run_id: str,
    steps_executed: int,
    total_tokens_used: int,
    model_mode_val: str,
    is_fallback: bool,
    step_traces: list[Any],
    decision_summary: str,
    incident_ids: list[str] | None = None,
) -> None:
    """Persist evidence-grounded conclusion and update linked ChallengeRun."""
    now = datetime.now(UTC)
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
            observations_vi.append(_summarize_tool_observation_vi(t_name, t_status, t_data))

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
            metadata_json={"risk_id": r.id, "category": r.category, "response": r.response},
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
                kind="ACTION_PROPOSED",
                title_vi=f"AI tạo đề xuất {p.action_type} — dừng chờ con người phê duyệt",
                detail_vi="Quyết định đã được chuyển vào Hộp thư phê duyệt.",
                metadata_json={"proposal_id": p.id, "action_type": p.action_type},
            )

    if case_obj is not None:
        conclusion_vi, safety_verdict, ch_status = _build_evidence_grounded_conclusion(
            case_obj=case_obj,
            decision_summary=decision_summary,
        )
    else:
        conclusion_vi = decision_summary or "Hoàn tất xử lý tác vụ."
        safety_verdict = "OBSERVED_NO_ACTION"
        ch_status = ChallengeStatus.COMPLETED.value

    record_activity(
        db,
        session_id=job.session_id,
        world_epoch=state.world_epoch,
        challenge_id=job.challenge_id,
        case_id=job.target_case_id,
        job_id=job.id,
        run_id=run_id,
        kind="INVESTIGATION_COMPLETED",
        title_vi=conclusion_vi[:255],
        detail_vi=decision_summary[:600] if decision_summary else None,
        metadata_json={
            "case_state": case_obj.state if case_obj else None,
            "safety_verdict": safety_verdict,
            "tools_ran": tools_ran,
        },
    )

    if job.challenge_id:
        challenge = db.scalar(
            select(ChallengeRun).where(
                ChallengeRun.id == job.challenge_id,
                ChallengeRun.session_id == job.session_id,
            )
        )
        if challenge is not None and challenge.status != ChallengeStatus.CANCELLED.value:
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
            action_blocked = safety_verdict in (
                "ACTION_HELD_BY_RISK",
                "WAITING_EVIDENCE",
            ) or any(r.response == "HOLD_ACTION" for r in risks)
            verified_pass = bool(
                case_obj and any(v.verdict == "PASSED" for v in (case_obj.verifications or []))
            )

            challenge.safety_verdict = safety_verdict
            challenge.status = ch_status
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


def process_next_supervisor_job(
    *,
    session_id: str | None = None,
    worker_id: str | None = None,
    worker_mode: str = "STANDALONE",
    force_when_paused: bool = False,
    commerce_transport: Any = None,
) -> dict[str, Any] | None:
    """Claim and execute at most ONE queued Supervisor job with fencing tokens & live heartbeats."""
    eff_worker = worker_id or f"sup-{uuid.uuid4().hex[:8]}"
    maker = get_sessionmaker()

    claimed_job_id: str | None = None
    claimed_session_id = ""
    claimed_world_epoch = 1
    claimed_job_type = ""
    claimed_case_id: str | None = None
    claimed_challenge_id: str | None = None
    claimed_payload: dict[str, Any] = {}
    claim_token = f"CLM-{uuid.uuid4().hex[:14].upper()}"

    # Phase 1: Short atomic claim transaction
    with maker() as db:
        upsert_worker_heartbeat(
            db, worker_id=eff_worker, worker_mode=worker_mode, status="IDLE", active_job_id=None
        )
        recover_stale_supervisor_jobs(db, session_id=session_id)

        now = datetime.now(UTC)
        stmt = (
            select(SupervisorJob)
            .join(DemoSession, DemoSession.id == SupervisorJob.session_id)
            .where(
                SupervisorJob.status == SupervisorJobStatus.QUEUED.value,
                DemoSession.status == "ACTIVE",
                DemoSession.expires_at > now,
                or_(
                    SupervisorJob.next_retry_at.is_(None),
                    SupervisorJob.next_retry_at <= now,
                ),
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
                cand.updated_at = now
                continue
            if (
                not st.autopilot_enabled
                and not force_when_paused
                and cand.job_type != SupervisorJobType.EXECUTE_APPROVED_ACTION.value
            ):
                continue
            target_job = cand
            target_state = st
            break

        if target_job is None or target_state is None:
            db.commit()
            return None

        target_job.status = SupervisorJobStatus.RUNNING.value
        target_job.attempt_count += 1
        target_job.worker_id = eff_worker
        target_job.claim_token = claim_token
        target_job.started_at = now
        target_job.heartbeat_at = now
        lease_s = get_settings().supervisor_job_lease_seconds
        target_job.locked_until = now + timedelta(seconds=lease_s)
        target_job.updated_at = now

        target_state.status = SupervisorStatus.INVESTIGATING.value
        target_state.active_job_id = target_job.id
        target_state.active_case_id = target_job.target_case_id
        target_state.updated_at = now

        upsert_worker_heartbeat(
            db,
            worker_id=eff_worker,
            worker_mode=worker_mode,
            status="BUSY",
            active_job_id=target_job.id,
        )
        record_activity(
            db,
            session_id=target_job.session_id,
            world_epoch=target_state.world_epoch,
            challenge_id=target_job.challenge_id,
            case_id=target_job.target_case_id,
            job_id=target_job.id,
            kind="JOB_CLAIMED",
            title_vi=f"Worker {eff_worker} nhận tác vụ {target_job.job_type} ({target_job.id})",
            detail_vi=f"Lần thử {target_job.attempt_count}/{target_job.max_attempts}.",
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
        claimed_payload = dict(target_job.payload_json or {})
        db.commit()

    # Phase 2: Execute job under fenced RuntimeExecutionContext + independent periodic heartbeat
    hb_thread = _JobHeartbeatThread(
        job_id=claimed_job_id,
        session_id=claimed_session_id,
        claim_token=claim_token,
        world_epoch=claimed_world_epoch,
        worker_id=eff_worker,
        worker_mode=worker_mode,
    )
    hb_thread.start()

    exec_ctx = _build_execution_context(
        job_id=claimed_job_id,
        session_id=claimed_session_id,
        claim_token=claim_token,
        world_epoch=claimed_world_epoch,
        worker_id=eff_worker,
        worker_mode=worker_mode,
        challenge_id=claimed_challenge_id,
        case_id=claimed_case_id,
    )
    ctx_tok = set_execution_context(exec_ctx)

    run_id = ""
    steps_executed = 0
    total_tokens_used = 0
    model_mode_val = get_settings().model_mode.value
    is_fallback = model_mode_val != ModelMode.LLM_API.value
    step_traces: list[Any] = []
    decision_summary = ""
    incident_ids: list[str] = []
    terminal_outcome: str = "COMPLETED"
    exec_error: str | None = None

    try:
        exec_ctx.check_guard("phase2_start")
        with maker() as exec_db:
            if claimed_job_type == SupervisorJobType.EXECUTE_APPROVED_ACTION.value:
                prop_id = str(claimed_payload.get("proposal_id") or "")
                idemp_key = str(
                    claimed_payload.get("idempotency_key") or f"IDEMP-AUTO-EXEC-{prop_id}"
                )
                executor = ActionExecutor(exec_db, claimed_session_id)
                outcome = executor.execute_proposal(
                    proposal_id=prop_id,
                    idempotency_key=idemp_key,
                )
                verdict_val = outcome.verification.verdict if outcome.verification else "NONE"
                case_state_val = outcome.case.state if outcome.case else None
                run_id = f"EXEC-{outcome.execution.id}"
                decision_summary = (
                    f"Thực thi {outcome.proposal.action_type} (state={outcome.execution.state}) "
                    f"& Verifier kiểm chứng độc lập: {verdict_val} (Case -> {case_state_val})."
                )
                exec_ctx.emit_activity(
                    "ACTION_EXECUTED",
                    f"Đã thực thi hành động được phê duyệt {outcome.proposal.action_type}",
                    f"Execution: {outcome.execution.id} ({outcome.execution.state})",
                    {"proposal_id": prop_id, "execution_state": outcome.execution.state},
                )
                exec_ctx.emit_activity(
                    "VERIFICATION_COMPLETED",
                    f"Verifier hoàn tất kiểm chứng hậu điều kiện: {verdict_val}",
                    f"Trạng thái hồ sơ sau kiểm chứng: {case_state_val}",
                    {"verdict": verdict_val, "case_state": case_state_val},
                )
                if outcome.proposal.case_id and verdict_val == "PASSED":
                    # Update linked ChallengeRun ONLY for this Case (never close Incidents!)
                    linked_challenges = list(
                        exec_db.scalars(
                            select(ChallengeRun).where(
                                ChallengeRun.session_id == claimed_session_id,
                                ChallengeRun.world_epoch == claimed_world_epoch,
                            )
                        ).all()
                    )
                    for ch in linked_challenges:
                        linked_ids = list(ch.linked_case_ids_json or [])
                        if outcome.proposal.case_id in linked_ids:
                            total_cases_count = len(linked_ids)
                            if total_cases_count > 1:
                                resolved_count = (
                                    exec_db.scalar(
                                        select(func.count(Case.id)).where(
                                            Case.session_id == claimed_session_id,
                                            Case.id.in_(linked_ids),
                                            Case.state == CaseState.RESOLVED.value,
                                        )
                                    )
                                    or 0
                                )
                            else:
                                resolved_count = 1
                            all_resolved = resolved_count >= max(1, total_cases_count)
                            ch.status = (
                                ChallengeStatus.COMPLETED.value
                                if all_resolved
                                else ChallengeStatus.PARTIALLY_RESOLVED.value
                            )
                            ch.safety_verdict = (
                                "VERIFIED_RESOLVED" if all_resolved else "PARTIALLY_RESOLVED"
                            )
                            ch.outcome_json = {
                                **(ch.outcome_json or {}),
                                "postconditions_verified": True,
                                "requires_human_approval": False,
                                "case_state": case_state_val,
                                "verified_cases_count": int(resolved_count),
                                "total_cases_count": total_cases_count,
                                "all_linked_cases_resolved": all_resolved,
                                "summary_vi": (
                                    f"Đã phê duyệt & thực thi {outcome.proposal.action_type}, "
                                    "Verifier kiểm chứng hậu điều kiện thành công (PASSED) — "
                                    "hồ sơ chuyển sang RESOLVED."
                                    if all_resolved
                                    else (
                                        f"Đã xác minh {resolved_count}/{total_cases_count} hồ sơ "
                                        f"({outcome.proposal.case_id} -> RESOLVED). "
                                        "Các hồ sơ còn lại trong thử thách hàng loạt chưa được "
                                        "tự động đóng và cần phê duyệt/kiểm chứng độc lập."
                                    )
                                ),
                            }
                            ch.updated_at = datetime.now(UTC)
                    exec_db.commit()
            elif claimed_job_type == SupervisorJobType.CORRELATE_INCIDENT.value:
                correlator = IncidentCorrelator(exec_db, claimed_session_id)
                corr_res = correlator.correlate_session_cases()
                incident_ids = [inc.id for inc in corr_res.incidents]
                exec_ctx.check_guard("after_correlation")
                tf_manager = TaskforceManager(exec_db, claimed_session_id)
                for inc in corr_res.incidents:
                    exec_ctx.check_guard(f"before_taskforce_{inc.id}")
                    exec_ctx.emit_activity(
                        "TASKFORCE_ACTIVATED",
                        f"Kích hoạt Taskforce đa vai trò cho Incident {inc.id}",
                        inc.title,
                        {"incident_id": inc.id},
                    )
                    tf_tasks = tf_manager.assign_and_run_taskforce(inc)
                    if any(bool((t.handoff_json or {}).get("llm_invoked")) for t in tf_tasks):
                        model_mode_val = ModelMode.LLM_API.value
                        is_fallback = False
                if claimed_case_id:
                    exec_ctx.check_guard("before_rep_case_investigation")
                    inv_res = _run_case_with_honest_fallback(
                        exec_db,
                        case_id=claimed_case_id,
                        session_id=claimed_session_id,
                    )
                    run_id = inv_res.run_id
                    steps_executed = inv_res.steps_executed
                    total_tokens_used = inv_res.total_tokens_used
                    model_mode_val = inv_res.model_mode.value
                    is_fallback = inv_res.is_fallback
                    step_traces = list(inv_res.step_traces)
                    decision_summary = inv_res.decision_summary
                else:
                    run_id = f"RUN-CORR-{uuid.uuid4().hex[:8].upper()}"
                    decision_summary = (
                        f"Đã gom nhóm {len(incident_ids)} sự cố chung theo bằng chứng nhân quả "
                        "và phân công Taskforce điều tra."
                    )
            elif claimed_case_id:
                inv_started_at = datetime.now(UTC)
                inv_res = _run_case_with_honest_fallback(
                    exec_db,
                    case_id=claimed_case_id,
                    session_id=claimed_session_id,
                )
                run_id = inv_res.run_id
                steps_executed = inv_res.steps_executed
                total_tokens_used = inv_res.total_tokens_used
                model_mode_val = inv_res.model_mode.value
                is_fallback = inv_res.is_fallback
                step_traces = list(inv_res.step_traces)
                decision_summary = inv_res.decision_summary

                # Prompt 14.3 Fix 2: Fence proposals strictly to current run & active job
                recovery_note = _run_commerce_post_investigation_recovery(
                    exec_db,
                    case_id=claimed_case_id,
                    session_id=claimed_session_id,
                    run_id=run_id,
                    job_id=claimed_job_id,
                    expected_world_epoch=claimed_world_epoch,
                    run_started_at=inv_started_at,
                    is_fallback_mode=is_fallback or (model_mode_val == "DEMO_RULES"),
                    anomaly_id=(
                        str(claimed_payload.get("anomaly_id"))
                        if claimed_payload.get("anomaly_id")
                        else None
                    ),
                    commerce_transport=commerce_transport,
                )
                if recovery_note:
                    decision_summary = f"{decision_summary} | {recovery_note}"
    except JobCancelledError as exc:
        terminal_outcome = "CANCELLED"
        exec_error = str(exc)
    except JobSupersededError as exc:
        terminal_outcome = "SUPERSEDED"
        exec_error = str(exc)
    except BudgetExhaustedError as exc:
        terminal_outcome = "BUDGET_EXHAUSTED"
        exec_error = str(exc)
    except Exception as exc:  # noqa: BLE001
        terminal_outcome = "FAILED"
        exec_error = str(exc)
    finally:
        hb_thread.stop()
        hb_thread.join(timeout=2.0)
        reset_execution_context(ctx_tok)

    # Phase 3: Fenced commit verifying claim_token, world_epoch, session, and cancellation
    with maker() as fin_db:
        upsert_worker_heartbeat(
            fin_db,
            worker_id=eff_worker,
            worker_mode=worker_mode,
            status="IDLE",
            active_job_id=None,
        )
        job = fin_db.scalar(
            select(SupervisorJob).where(SupervisorJob.id == claimed_job_id).with_for_update()
        )
        state = ensure_supervisor_state(fin_db, claimed_session_id)
        sess = fin_db.get(DemoSession, claimed_session_id)
        if job is None:
            fin_db.commit()
            return None

        # Fencing check: stale worker MUST NEVER overwrite a newer attempt!
        if job.claim_token != claim_token:
            fin_db.commit()
            return {"job_id": job.id, "status": "FENCED_STALE_WORKER"}

        now = datetime.now(UTC)
        if (
            terminal_outcome == "SUPERSEDED"
            or sess is None
            or not sess.is_active
            or state.world_epoch != claimed_world_epoch
            or job.status == SupervisorJobStatus.SUPERSEDED.value
        ):
            job.status = SupervisorJobStatus.SUPERSEDED.value
            job.claim_token = None
            job.error_message = exec_error or "Superseded by session reset or expiry"
            job.completed_at = now
            job.updated_at = now
            recompute_supervisor_idle_status(fin_db, state)
            fin_db.commit()
            return {"job_id": job.id, "status": "SUPERSEDED"}

        if (
            terminal_outcome == "CANCELLED"
            or job.status
            in (SupervisorJobStatus.CANCELLING.value, SupervisorJobStatus.CANCELLED.value)
            or job.cancel_requested_at is not None
        ):
            job.status = SupervisorJobStatus.CANCELLED.value
            job.claim_token = None
            job.error_message = exec_error or "Cancelled cooperatively"
            job.completed_at = now
            job.updated_at = now
            if claimed_challenge_id:
                ch_cancel = fin_db.scalar(
                    select(ChallengeRun).where(
                        ChallengeRun.id == claimed_challenge_id,
                        ChallengeRun.session_id == claimed_session_id,
                    )
                )
                if ch_cancel is not None and ch_cancel.status in (
                    ChallengeStatus.QUEUED.value,
                    ChallengeStatus.RUNNING.value,
                    ChallengeStatus.CANCELLING.value,
                ):
                    ch_cancel.status = ChallengeStatus.CANCELLED.value
                    ch_cancel.safety_verdict = "CANCELLED_BY_USER"
                    ch_cancel.outcome_json = {
                        **(ch_cancel.outcome_json or {}),
                        "summary_vi": (
                            "Worker đã xác nhận dừng hợp tác (CANCELLED); "
                            "kết quả đến muộn đã bị loại bỏ."
                        ),
                        "cancelled_completed_at": now.isoformat(),
                    }
                    ch_cancel.updated_at = now
            record_activity(
                fin_db,
                session_id=claimed_session_id,
                world_epoch=state.world_epoch,
                challenge_id=claimed_challenge_id,
                case_id=claimed_case_id,
                job_id=job.id,
                kind="JOB_CANCELLED",
                title_vi=f"Tác vụ {job.id} đã dừng an toàn theo yêu cầu hủy (CANCELLED)",
                detail_vi="Kết quả đến muộn bị loại bỏ; không ghi đè thành COMPLETED.",
                metadata_json={"job_id": job.id, "status": "CANCELLED"},
            )
            recompute_supervisor_idle_status(fin_db, state)
            fin_db.commit()
            return {"job_id": job.id, "status": "CANCELLED"}

        if terminal_outcome in ("FAILED", "BUDGET_EXHAUSTED"):
            job.status = SupervisorJobStatus.FAILED.value
            job.claim_token = None
            job.error_message = (exec_error or terminal_outcome)[:1000]
            job.completed_at = now
            job.updated_at = now
            state.status = SupervisorStatus.DEGRADED.value
            if claimed_challenge_id:
                ch_fail = fin_db.scalar(
                    select(ChallengeRun).where(
                        ChallengeRun.id == claimed_challenge_id,
                        ChallengeRun.session_id == claimed_session_id,
                    )
                )
                if ch_fail is not None and ch_fail.status in (
                    ChallengeStatus.QUEUED.value,
                    ChallengeStatus.RUNNING.value,
                    ChallengeStatus.CANCELLING.value,
                ):
                    ch_fail.status = ChallengeStatus.FAILED.value
                    ch_fail.safety_verdict = (
                        "BUDGET_EXHAUSTED"
                        if terminal_outcome == "BUDGET_EXHAUSTED"
                        else "EXECUTION_FAILED"
                    )
                    ch_fail.outcome_json = {
                        **(ch_fail.outcome_json or {}),
                        "summary_vi": (
                            "Đã đạt giới hạn ngân sách LLM — dừng điều tra an toàn."
                            if terminal_outcome == "BUDGET_EXHAUSTED"
                            else f"Tác vụ thất bại kỹ thuật: {(exec_error or '')[:240]}"
                        ),
                        "error_message": (exec_error or terminal_outcome)[:400],
                    }
                    ch_fail.updated_at = now
            record_activity(
                fin_db,
                session_id=claimed_session_id,
                world_epoch=state.world_epoch,
                challenge_id=claimed_challenge_id,
                case_id=claimed_case_id,
                job_id=job.id,
                kind="INVESTIGATION_FAILED",
                title_vi=(
                    "Đã đạt giới hạn ngân sách LLM — dừng điều tra an toàn"
                    if terminal_outcome == "BUDGET_EXHAUSTED"
                    else "Tác vụ điều tra gặp lỗi kỹ thuật — đã dừng an toàn"
                ),
                detail_vi=(exec_error or "")[:400],
                metadata_json={"job_id": job.id, "outcome": terminal_outcome},
            )
            fin_db.commit()
            return {"job_id": job.id, "status": "FAILED", "error": exec_error}

        job.status = SupervisorJobStatus.COMPLETED.value
        job.claim_token = None
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

        if claimed_job_type != SupervisorJobType.EXECUTE_APPROVED_ACTION.value:
            _finalize_challenge_and_case_outcome(
                fin_db,
                job=job,
                state=state,
                run_id=run_id,
                steps_executed=steps_executed,
                total_tokens_used=total_tokens_used,
                model_mode_val=model_mode_val,
                is_fallback=is_fallback,
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


def _run_case_with_honest_fallback(
    db: Session,
    *,
    case_id: str,
    session_id: str,
) -> Any:
    """Run BoundedAgentRuntime; if external LLM fails on step 0, transparently fallback to
    DeterministicDemoAdapter while keeping `LLM_CALL_FAILED` recorded on SupervisorState."""
    rt = BoundedAgentRuntime(db, max_steps=6, manage_transactions=True)
    inv_res = rt.investigate_case(case_id=case_id, session_id=session_id)
    if inv_res.stop_reason == "MAX_REPLANS_EXCEEDED" and inv_res.steps_executed == 0:
        c_retry = db.get(Case, case_id)
        if c_retry is not None and c_retry.state == CaseState.ESCALATED.value:
            c_retry.state = CaseState.NEW.value
            c_retry.version += 1
            db.commit()
            fb_rt = BoundedAgentRuntime(
                db,
                adapter=DeterministicDemoAdapter(),
                max_steps=6,
                manage_transactions=True,
            )
            inv_res = fb_rt.investigate_case(case_id=case_id, session_id=session_id)
    return inv_res


def _run_commerce_post_investigation_recovery(
    db: Session,
    *,
    case_id: str,
    session_id: str,
    run_id: str = "RUN-SUP",
    job_id: str | None = None,
    expected_world_epoch: int | None = None,
    run_started_at: datetime | None = None,
    is_fallback_mode: bool = False,
    anomaly_id: str | None = None,
    commerce_transport: Any = None,
) -> str | None:
    """Consume the Agent's structured recovery proposal from the CURRENT investigation run
    (or explicitly labeled fallback), validate fencing & evidence provenance, authorize via
    PolicyEngine, execute atomically, and verify.
    """
    from app.models.cases import CaseEvent, Evidence
    from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
    from app.monitoring.recovery_planner import (
        StructuredAgentRecoveryProposal,
        build_evidence_driven_recovery_plan,
        validate_and_execute_agent_proposal,
    )
    from app.monitoring.service import build_connector_for_binding

    case_obj = db.scalar(select(Case).where(Case.id == case_id, Case.session_id == session_id))
    if (
        case_obj is None
        or case_obj.source_system != "COMMERCE_HTTP"
        or not case_obj.source_binding_id
    ):
        return None

    obs_ev_rows = list(
        db.scalars(
            select(Evidence).where(
                Evidence.case_id == case_obj.id,
                Evidence.session_id == session_id,
                Evidence.kind == EvidenceKind.OBSERVED_FACT.value,
            )
        ).all()
    )
    if not obs_ev_rows:
        return None
    obs_ev_ids = [e.id for e in obs_ev_rows]

    binding = db.scalar(
        select(CommerceSourceBinding).where(
            CommerceSourceBinding.id == case_obj.source_binding_id,
            CommerceSourceBinding.session_id == session_id,
            CommerceSourceBinding.is_enabled.is_(True),
        )
    )
    if binding is None:
        return None

    obs: AnomalyObservation | None = None
    if anomaly_id:
        obs = db.scalar(
            select(AnomalyObservation).where(
                AnomalyObservation.id == anomaly_id,
                AnomalyObservation.session_id == session_id,
            )
        )
    if obs is None:
        obs = db.scalar(
            select(AnomalyObservation)
            .where(
                AnomalyObservation.linked_case_id == case_obj.id,
                AnomalyObservation.session_id == session_id,
            )
            .order_by(AnomalyObservation.first_observed_at.desc())
            .limit(1)
        )
    if obs is None or obs.lifecycle_state == "RECOVERED":
        return None

    # Prompt 14.3 Fix 2: Fence ActionProposal strictly to PROPOSED status AND current run window
    prop_filters: list[Any] = [
        ActionProposal.case_id == case_obj.id,
        ActionProposal.session_id == session_id,
        ActionProposal.status == "PROPOSED",
    ]
    if run_started_at is not None:
        prop_filters.append(ActionProposal.created_at >= run_started_at)

    agent_action_prop = db.scalar(
        select(ActionProposal)
        .where(*prop_filters)
        .order_by(ActionProposal.created_at.desc())
        .limit(1)
    )
    reader = build_connector_for_binding(binding, transport=commerce_transport)
    safety_plan = build_evidence_driven_recovery_plan(
        db, binding=binding, obs=obs, connector=reader
    )

    now = datetime.now(UTC)
    if agent_action_prop is not None:
        act_map = {
            "ORDER_SYNC": "REQUEST_ORDER_SYNC",
            "REQUEST_ORDER_SYNC": "REQUEST_ORDER_SYNC",
            "REQUEST_EVENT_REDELIVERY": "REQUEST_EVENT_REDELIVERY",
            "REQUEST_PAYMENT_RECONCILIATION": "REQUEST_PAYMENT_RECONCILIATION",
            "WAIT_FOR_OBSERVATION": "WAIT_FOR_OBSERVATION",
            "REQUEST_MANUAL_REVIEW": "REQUEST_MANUAL_REVIEW",
        }
        proposed_cmd = act_map.get(agent_action_prop.action_type.upper(), "REQUEST_MANUAL_REVIEW")
        proposal_source: Any = (
            "DETERMINISTIC_FALLBACK" if is_fallback_mode else "AGENT_STRUCTURED_PROPOSAL"
        )
        action_params = dict(agent_action_prop.params_json or {})
        raw_ev_refs = action_params.get("evidence_refs")
        supporting_ev_ids = (
            [str(x) for x in raw_ev_refs]
            if isinstance(raw_ev_refs, list) and len(raw_ev_refs) > 0
            else obs_ev_ids
        )
        # Preserve Agent's exact expected_version (never silently refresh stale version)
        raw_exp_ver = action_params.get("expected_version")
        prop_exp_ver: int | None = int(raw_exp_ver) if raw_exp_ver is not None else None
        prop_id = agent_action_prop.id
        target_ord = (
            str(action_params["order_id"])
            if action_params.get("order_id") is not None
            else obs.affected_order_id
        )
        target_pay = (
            str(action_params["payment_id"])
            if action_params.get("payment_id") is not None
            else obs.affected_payment_id
        )
    elif not is_fallback_mode:
        proposed_cmd = "REQUEST_MANUAL_REVIEW"
        proposal_source = "AGENT_STRUCTURED_PROPOSAL"
        action_params = {}
        supporting_ev_ids = obs_ev_ids
        prop_exp_ver = None
        prop_id = f"PROP-NOACT-{uuid.uuid4().hex[:10].upper()}"
        target_ord = obs.affected_order_id
        target_pay = obs.affected_payment_id
    else:
        proposed_cmd = (
            safety_plan.proposed_command
            if (not safety_plan.requires_human_approval and safety_plan.preconditions_met)
            else "REQUEST_MANUAL_REVIEW"
        )
        proposal_source = "DETERMINISTIC_FALLBACK"
        action_params = (
            {"expected_version": safety_plan.expected_version}
            if safety_plan.expected_version is not None
            else {}
        )
        supporting_ev_ids = obs_ev_ids
        prop_exp_ver = safety_plan.expected_version
        prop_id = f"PROP-FB-{uuid.uuid4().hex[:10].upper()}"
        target_ord = obs.affected_order_id
        target_pay = obs.affected_payment_id

    structured_prop = StructuredAgentRecoveryProposal(
        proposal_id=prop_id,
        case_id=case_obj.id,
        run_id=run_id,
        supervisor_job_id=job_id,
        anomaly_id=obs.id,
        anomaly_occurrence=int(obs.occurrence_count or 1),
        target_order_id=target_ord,
        target_payment_id=target_pay,
        expected_version=prop_exp_ver,
        affected_entities=dict(obs.affected_entities_json or {}),
        observed_symptoms=[obs.rationale],
        candidate_causes=[h.get("cause", obs.anomaly_type) for h in safety_plan.hypotheses],
        supporting_evidence_ids=supporting_ev_ids,
        missing_information=list(safety_plan.missing_evidence),
        proposed_command=proposed_cmd,
        action_params=action_params,
        expected_preconditions=[
            "PolicyEngine deterministic authorization",
            "Authoritative state match",
        ],
        expected_postconditions=list(safety_plan.expected_postconditions),
        policy_references=list(safety_plan.policy_references)
        if hasattr(safety_plan, "policy_references")
        else list(safety_plan.policy_ids),
        uncertainty_summary=safety_plan.uncertainty_note,
        alternative_safe_outcomes=["REQUEST_MANUAL_REVIEW", "WAIT_FOR_OBSERVATION"],
        proposal_source=proposal_source,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        lifecycle_state="PROPOSED",
    )

    db.add(
        CaseEvent(
            id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
            case_id=case_obj.id,
            session_id=session_id,
            type="AUTONOMOUS_RECOVERY_PLANNED",
            payload_json={
                "proposal_id": structured_prop.proposal_id,
                "run_id": structured_prop.run_id,
                "supervisor_job_id": structured_prop.supervisor_job_id,
                "anomaly_id": obs.id,
                "proposed_command": structured_prop.proposed_command,
                "proposal_source": structured_prop.proposal_source,
                "policy_ids": structured_prop.policy_references,
                "supporting_evidence_ids": structured_prop.supporting_evidence_ids,
                "uncertainty_summary": structured_prop.uncertainty_summary,
            },
            created_at=now,
        )
    )
    db.flush()

    _, exec_res = validate_and_execute_agent_proposal(
        db,
        proposal=structured_prop,
        binding=binding,
        obs=obs,
        expected_run_id=run_id,
        expected_job_id=job_id,
        expected_world_epoch=expected_world_epoch,
        transport=commerce_transport,
    )

    if exec_res is None:
        if agent_action_prop is not None:
            agent_action_prop.status = "ESCALATED"
        db.add(
            CaseEvent(
                id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
                case_id=case_obj.id,
                session_id=session_id,
                type="AUTONOMOUS_RECOVERY_ESCALATED",
                payload_json={
                    "proposal_id": structured_prop.proposal_id,
                    "anomaly_id": obs.id,
                    "proposed_command": structured_prop.proposed_command,
                    "proposal_source": structured_prop.proposal_source,
                    "uncertainty_summary": structured_prop.uncertainty_summary,
                },
                created_at=datetime.now(UTC),
            )
        )
        db.commit()
        return (
            f"Escalated safely ({structured_prop.proposed_command}, "
            f"source={structured_prop.proposal_source})"
        )

    if agent_action_prop is not None and exec_res.executed:
        agent_action_prop.status = "EXECUTED"

    if exec_res.verified_recovery:
        db.add(
            CaseEvent(
                id=f"CEV-{uuid.uuid4().hex[:10].upper()}",
                case_id=case_obj.id,
                session_id=session_id,
                type="AUTONOMOUS_RECOVERY_VERIFIED",
                payload_json={
                    "command_id": exec_res.command_id,
                    "command": exec_res.command,
                    "proposal_source": structured_prop.proposal_source,
                    "verdict": exec_res.verdict,
                    "recovery_source": exec_res.recovery_source,
                    "detail": exec_res.detail,
                },
                created_at=datetime.now(UTC),
            )
        )
        case_obj.state = CaseState.RESOLVED.value
        case_obj.updated_at = datetime.now(UTC)
        db.commit()
        return (
            f"Autonomous recovery verified ({exec_res.command} -> {exec_res.verdict}, "
            f"source={structured_prop.proposal_source})"
        )

    db.commit()
    return f"Recovery attempted ({exec_res.command} -> {exec_res.verdict})"


def drain_session_supervisor_jobs(
    session_id: str,
    *,
    max_jobs: int = 5,
    force_when_paused: bool = False,
    worker_id: str | None = None,
    commerce_transport: Any = None,
) -> list[dict[str, Any]]:
    """Process up to `max_jobs` queued Supervisor jobs (for standalone worker & tests)."""
    results: list[dict[str, Any]] = []
    for _ in range(max(1, max_jobs)):
        res = process_next_supervisor_job(
            session_id=session_id,
            worker_id=worker_id,
            force_when_paused=force_when_paused,
            commerce_transport=commerce_transport,
        )
        if res is None:
            break
        results.append(res)
    return results
