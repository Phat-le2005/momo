"""Unified Demo Experience Session Orchestrator & Read-Only Command Center API (Prompt 15).

Security & Isolation Invariants:
- Zero secrets, API keys, service keys, manager tokens, or chaos admin keys are ever returned.
- Bootstrap creates or restores an isolated guest DemoSession idempotently and provisions the
  server-registered `default_ministore` CommerceSourceBinding internally (never accepts user URLs).
- Overview is strictly read-only, scoped to `(session_id, world_epoch)`, never invokes the LLM,
  and never fabricates metrics when Commerce is unreachable (returns `null` + `DISCONNECTED`).
- Reset fences ONLY the caller's session by incrementing its `world_epoch`.
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.core.config import LOCAL_DEV_ENVIRONMENTS, get_settings
from app.core.rate_limit import enforce_session_creation_rate_limit
from app.core.session import get_current_session
from app.db.session import get_db
from app.models.actions import ActionProposal
from app.models.cases import Case, CaseEvent
from app.models.enums import ActionProposalStatus, CaseState, SessionStatus
from app.models.monitoring import AnomalyObservation, CommerceSourceBinding
from app.models.open_world import BehavioralBaseline, RecoveryCommand
from app.models.session import DemoSession
from app.models.supervisor import (
    NormalizedEvent,
    SupervisorActivity,
    SupervisorJob,
    SupervisorState,
)
from app.monitoring.service import ensure_commerce_binding, run_binding_scan
from app.supervisor.service import ensure_supervisor_state, get_worker_health

router = APIRouter(prefix="/experience", tags=["unified-experience"])

HUMAN_EVENT_LABELS_VI: dict[str, str] = {
    "EVENT_RECEIVED": "Đã nhận thông tin mới từ hệ thống kinh doanh",
    "ANOMALY_CASE_CREATED_BY_MONITOR": "AI Guardian phát hiện bất thường mới",
    "JOB_CLAIMED": "AI Guardian tiếp nhận hồ sơ điều tra",
    "INVESTIGATION_STARTED": "AI bắt đầu kiểm tra sự cố",
    "TOOL_INVOKED": "Đang kiểm tra dữ liệu nghiệp vụ",
    "TOOL_READ_COMPLETED": "Đã đọc dữ liệu xác thực từ hệ thống",
    "PLAN_REVISED": "AI điều chỉnh hướng điều tra",
    "AI_PLAN_GENERATED": "AI hoàn tất phân tích bước điều tra",
    "ACTION_PROPOSED": "Đã tìm được phương án xử lý an toàn",
    "AUTONOMOUS_RECOVERY_PLANNED": "Đã lập kế hoạch phục hồi có kiểm soát",
    "WAITING_APPROVAL": "Cần bạn xác nhận phê duyệt",
    "ACTION_EXECUTED": "Đã thực hiện hành động khắc phục",
    "COMMERCE_RECOVERY_COMMAND_EXECUTED": "Đã gửi lệnh phục hồi có chữ ký bảo mật",
    "AUTONOMOUS_RECOVERY_VERIFIED": "Đã xác minh kết quả phục hồi thành công",
    "VERIFICATION_COMPLETED": "Đã kiểm tra kết quả sau xử lý",
    "MONITOR_RECOVERY_VERIFIED": "Hệ thống đã trở lại trạng thái ổn định",
    "AUTONOMOUS_RECOVERY_ESCALATED": "Chuyển chuyên viên xem xét để đảm bảo an toàn",
    "UNSAFE_PROPOSAL_BLOCKED": "Policy Engine đã chặn hành động chưa đủ bằng chứng",
}


class ExperienceBootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_minutes: int = Field(default=60, ge=5, le=180)


class ExperienceBootstrapResponse(BaseModel):
    status: str  # READY | DEGRADED | PROVISIONING | FAILED | EXPIRED
    restored: bool
    session: dict[str, Any]
    sandbox: dict[str, Any]
    guardian: dict[str, Any]


_SENSITIVE_TRACE_SUBSTRINGS: tuple[str, ...] = (
    "secret",
    "token",
    "api_key",
    "write_key",
    "service_key",
    "password",
    "authorization",
    "cookie",
    "credential",
)


def _scrub_sensitive_trace(value: Any) -> Any:
    """Recursively redact sensitive keys from technical_trace payloads before sending to browser."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for k, v in value.items():
            kl = str(k).lower()
            if any(sub in kl for sub in _SENSITIVE_TRACE_SUBSTRINGS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _scrub_sensitive_trace(v)
        return cleaned
    if isinstance(value, list):
        return [_scrub_sensitive_trace(item) for item in value]
    return value


def _effective_connectivity_status(binding: CommerceSourceBinding | None) -> str:
    """An initialized binding without a verified scan MUST NOT claim CONNECTED."""
    if binding is None:
        return "UNCONFIGURED"
    if binding.connectivity_status == "CONNECTED" and binding.last_successful_scan_at is None:
        return "UNVERIFIED"
    return binding.connectivity_status


def _sandbox_isolation_mode(commerce_session_id: str) -> str:
    """Honest sandbox ownership label: custom session ID alone does NOT imply dedicated sandbox."""
    settings = get_settings()
    if settings.sandbox_dedicated_mode and commerce_session_id != "CSESS_FINSHOP_DEFAULT_01":
        return "DEDICATED_SANDBOX"
    return "SHARED_DEMO_SANDBOX"


def _find_active_shared_sandbox_owner(
    db: Session,
    *,
    merchant_id: str,
    commerce_session_id: str,
    commerce_world_epoch: int,
    exclude_session_id: str | None = None,
) -> CommerceSourceBinding | None:
    """Return another active non-expired workspace holding control of a shared sandbox."""
    now = datetime.now(UTC)
    stmt = (
        select(CommerceSourceBinding)
        .join(DemoSession, CommerceSourceBinding.session_id == DemoSession.id)
        .where(
            CommerceSourceBinding.merchant_id == merchant_id,
            CommerceSourceBinding.commerce_session_id == commerce_session_id,
            CommerceSourceBinding.commerce_world_epoch == commerce_world_epoch,
            CommerceSourceBinding.is_enabled.is_(True),
            DemoSession.status == SessionStatus.ACTIVE.value,
            DemoSession.expires_at > now,
        )
        .order_by(CommerceSourceBinding.created_at.asc())
    )
    if exclude_session_id:
        stmt = stmt.where(CommerceSourceBinding.session_id != exclude_session_id)
    return db.scalar(stmt.limit(1))


def _count_unresolved_guards_for_scope(
    db: Session,
    *,
    merchant_id: str,
    commerce_session_id: str,
    commerce_world_epoch: int,
) -> int:
    """Count unresolved uncertain RecoveryCommands on the canonical Commerce business scope."""
    return int(
        db.scalar(
            select(func.count(RecoveryCommand.id)).where(
                RecoveryCommand.merchant_id == merchant_id,
                RecoveryCommand.commerce_session_id == commerce_session_id,
                RecoveryCommand.world_epoch == commerce_world_epoch,
                or_(
                    RecoveryCommand.state.in_(["EXECUTING", "UNKNOWN_OUTCOME"]),
                    and_(
                        RecoveryCommand.state == "MANUAL_REVIEW",
                        RecoveryCommand.execution_certainty.in_(
                            ["UNKNOWN", "COMMAND_EXECUTION_UNKNOWN"]
                        ),
                        RecoveryCommand.uncertainty_resolved_at.is_(None),
                    ),
                ),
            )
        )
        or 0
    )


def _is_session_active(sess: DemoSession | None) -> bool:
    if sess is None or sess.status != SessionStatus.ACTIVE.value:
        return False
    exp = sess.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return exp > datetime.now(UTC)


def _require_active_controller_binding(
    db: Session,
    *,
    current_session: DemoSession,
    sup_state: SupervisorState,
) -> CommerceSourceBinding | None:
    """Fail-closed controller authorization gate for mutating experience endpoints.

    When Commerce sandbox is configured, the caller MUST already hold an active enabled
    CommerceSourceBinding for `(current_session.id, sup_state.world_epoch)` and an active,
    unexpired DemoSession. READ_ONLY_PREVIEW guests can NEVER self-elevate via `/reset`,
    `/scan`, or `/release`.
    """
    if not _is_session_active(current_session):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="SESSION_EXPIRED_OR_INACTIVE",
        )
    settings = get_settings()
    if not settings.commerce_service_base_url.strip():
        return None

    binding = db.scalar(
        select(CommerceSourceBinding).where(
            CommerceSourceBinding.session_id == current_session.id,
            CommerceSourceBinding.world_epoch == sup_state.world_epoch,
            CommerceSourceBinding.is_enabled.is_(True),
        )
    )
    if binding is not None:
        return binding

    other_owner = _find_active_shared_sandbox_owner(
        db,
        merchant_id=settings.commerce_merchant_id,
        commerce_session_id=settings.commerce_session_id,
        commerce_world_epoch=settings.commerce_world_epoch,
        exclude_session_id=current_session.id,
    )
    if other_owner is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SHARED_SANDBOX_CONTROLLED_BY_ANOTHER_WORKSPACE",
        )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="CONTROLLER_AUTHORIZATION_REQUIRED",
    )


def _resolve_existing_active_session(
    db: Session,
    session_token: str | None,
) -> DemoSession | None:
    if not session_token or not session_token.strip():
        return None
    sess = db.scalar(select(DemoSession).where(DemoSession.token == session_token.strip()))
    if not _is_session_active(sess):
        return None
    return sess


@router.post(
    "/bootstrap",
    response_model=ExperienceBootstrapResponse,
    status_code=status.HTTP_200_OK,
    summary="Idempotent server-side Unified Experience session & sandbox bootstrap (zero secrets)",
)
def bootstrap_experience(
    payload: ExperienceBootstrapRequest | None = None,
    x_demo_session: str | None = Header(default=None, alias="X-Demo-Session"),
    x_operator_token: str | None = Header(default=None, alias="X-Operator-Token"),
    _rl: None = Depends(enforce_session_creation_rate_limit),
    db: Session = Depends(get_db),
) -> ExperienceBootstrapResponse:
    """Create or restore an isolated experience session and bind the server-registered sandbox.

    Never exposes manager tokens, signing secrets, chaos keys, or LLM keys to the browser.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    req = payload or ExperienceBootstrapRequest()

    sess = _resolve_existing_active_session(db, x_demo_session)
    restored = sess is not None
    if sess is None:
        sess = DemoSession(
            id=f"sess_{uuid.uuid4().hex[:16]}",
            token=f"dsk_{secrets.token_urlsafe(24)}",
            status=SessionStatus.ACTIVE.value,
            max_cases=40,
            case_count=0,
            expires_at=now + timedelta(minutes=req.ttl_minutes),
            created_at=now,
            updated_at=now,
        )
        db.add(sess)
        db.flush()

    sup_state = ensure_supervisor_state(db, sess.id)
    binding: CommerceSourceBinding | None = None
    workspace_role = "CONTROLLER"
    eff_csess = settings.commerce_session_id
    eff_merchant = settings.commerce_merchant_id
    eff_epoch = settings.commerce_world_epoch

    if settings.commerce_service_base_url.strip():
        # Serialize shared sandbox ownership claims via PostgreSQL advisory transaction lock
        owner_lock_key = f"sandbox_owner:{eff_merchant}:{eff_csess}:{eff_epoch}"
        db.execute(sa_text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": owner_lock_key})

        is_shared = _sandbox_isolation_mode(eff_csess) == "SHARED_DEMO_SANDBOX"
        existing_own_binding = (
            db.scalar(
                select(CommerceSourceBinding).where(
                    CommerceSourceBinding.session_id == sess.id,
                    CommerceSourceBinding.world_epoch == sup_state.world_epoch,
                    CommerceSourceBinding.is_enabled.is_(True),
                )
            )
            if restored
            else None
        )
        other_owner = (
            _find_active_shared_sandbox_owner(
                db,
                merchant_id=eff_merchant,
                commerce_session_id=eff_csess,
                commerce_world_epoch=eff_epoch,
                exclude_session_id=sess.id,
            )
            if is_shared
            else None
        )

        # Prompt 14.9 Fix P1: Require server-side operator authorization before claiming
        # shared sandbox control when operator token is configured or in non-local envs.
        expected_op = settings.experience_operator_token.get_secret_value().strip()
        require_op_auth = bool(expected_op) or (
            settings.app_env.strip().lower() not in LOCAL_DEV_ENVIRONMENTS
        )
        op_authorized = True
        if existing_own_binding is None and require_op_auth:
            provided_op = (x_operator_token or "").strip()
            op_authorized = bool(expected_op) and hmac.compare_digest(provided_op, expected_op)

        # Prevent unsafe handoff to a new controller while unresolved uncertain guards exist
        unresolved_scope_guards = (
            _count_unresolved_guards_for_scope(
                db,
                merchant_id=eff_merchant,
                commerce_session_id=eff_csess,
                commerce_world_epoch=eff_epoch,
            )
            if existing_own_binding is None
            else 0
        )

        if other_owner is not None or not op_authorized or unresolved_scope_guards > 0:
            workspace_role = "READ_ONLY_PREVIEW"
            binding = None
        else:
            binding = ensure_commerce_binding(
                db,
                session_id=sess.id,
                connector_id=settings.commerce_connector_id or "default_ministore",
                merchant_id=eff_merchant,
                commerce_session_id=eff_csess,
                commerce_world_epoch=eff_epoch,
                is_enabled=True,
            )

    db.commit()
    worker_health = get_worker_health(db)
    worker_ok = bool(worker_health.get("worker_healthy", False))
    conn_status = (
        "READ_ONLY_PREVIEW"
        if workspace_role == "READ_ONLY_PREVIEW"
        else _effective_connectivity_status(binding)
    )
    overall = "READY" if (worker_ok and conn_status == "CONNECTED") else "DEGRADED"

    # Configurable public browser-visible MiniStore URL (never internal docker hostname)
    storefront_url = settings.commerce_public_storefront_url.strip() or "http://localhost:3100"

    return ExperienceBootstrapResponse(
        status=overall,
        restored=restored,
        session={
            "session_id": sess.id,
            "token": sess.token,
            "status": sess.status,
            "expires_at": sess.expires_at.isoformat(),
            "world_epoch": sup_state.world_epoch,
            "workspace_role": workspace_role,
        },
        sandbox={
            "environment_name": "FinShop LIVE Sandbox (MOCK)",
            "sandbox_isolation_mode": _sandbox_isolation_mode(eff_csess),
            "workspace_role": workspace_role,
            "merchant_id": binding.merchant_id if binding else eff_merchant,
            "commerce_session_id": eff_csess,
            "commerce_world_epoch": binding.commerce_world_epoch if binding else eff_epoch,
            "binding_id": binding.id if binding else None,
            "connectivity_status": conn_status,
            "storefront_url": storefront_url,
            "vnpay_enabled": False,
        },
        guardian={
            "worker_healthy": worker_ok,
            "supervisor_status": sup_state.status,
            "model_mode": settings.model_mode.value,
            "model_provider": settings.model_provider or "DEMO_RULES",
            "model_name": settings.model_name or "deterministic-demo",
            "autopilot_enabled": bool(sup_state.autopilot_enabled),
        },
    )


@router.get(
    "/overview",
    summary="Read-only session-scoped Guardian Command Center overview (real telemetry & events)",
)
def get_experience_overview(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Aggregate real session-scoped KPIs, business health, anomalies, approvals & timeline."""
    settings = get_settings()
    sid = current_session.id
    sup_state = ensure_supervisor_state(db, sid)
    epoch = sup_state.world_epoch
    worker_health = get_worker_health(db)

    binding = db.scalar(
        select(CommerceSourceBinding)
        .where(
            CommerceSourceBinding.session_id == sid,
            CommerceSourceBinding.world_epoch == epoch,
            CommerceSourceBinding.is_enabled.is_(True),
        )
        .order_by(CommerceSourceBinding.created_at.desc())
        .limit(1)
    )
    is_read_only_preview = binding is None and bool(settings.commerce_service_base_url.strip())
    if is_read_only_preview:
        other_owner_present = (
            _sandbox_isolation_mode(settings.commerce_session_id) == "SHARED_DEMO_SANDBOX"
            and _find_active_shared_sandbox_owner(
                db,
                merchant_id=settings.commerce_merchant_id,
                commerce_session_id=settings.commerce_session_id,
                commerce_world_epoch=settings.commerce_world_epoch,
                exclude_session_id=sid,
            )
            is not None
        )
        preview_msg_vi = (
            "Sandbox dùng chung đang được điều khiển bởi phiên khác (chế độ chỉ xem)."
            if other_owner_present
            else "Phiên hiện tại đang ở chế độ chỉ xem (READ_ONLY_PREVIEW). Cần xác thực Operator để điều khiển."
        )
        return {
            "session_id": sid,
            "world_epoch": epoch,
            "workspace_role": "READ_ONLY_PREVIEW",
            "sandbox_isolation_mode": _sandbox_isolation_mode(settings.commerce_session_id),
            "unresolved_uncertainty_guards": 0,
            "updated_at": datetime.now(UTC).isoformat(),
            "guardian": {
                "visual_state": "DISCONNECTED",
                "supervisor_status": "READ_ONLY_PREVIEW",
                "worker_healthy": bool(worker_health.get("worker_healthy", False)),
                "connectivity_status": "READ_ONLY_PREVIEW",
                "model_mode": settings.model_mode.value,
                "model_name": settings.model_name or "deterministic-demo",
                "queued_jobs": 0,
                "last_scan_at": None,
            },
            "kpis": {
                "orders_observed": None,
                "payments_observed": None,
                "active_anomalies": 0,
                "verified_recoveries": 0,
                "pending_approvals": 0,
                "total_anomalies_detected": 0,
                "total_cases": 0,
            },
            "business_health": {
                "telemetry_available": False,
                "has_samples": False,
                "window_id": None,
                "rps": None,
                "p95_latency_ms": None,
                "error_rate": None,
                "outbox_lag": None,
                "unsynced_payments": None,
                "baseline_status": "WARMING_UP",
                "baseline_message_vi": preview_msg_vi,
                "baselines": [],
            },
            "anomalies": [],
            "approvals": [],
            "recovery_commands": [],
            "recent_business_events": [],
            "timeline": [],
        }

    anomalies = list(
        db.scalars(
            select(AnomalyObservation)
            .where(
                AnomalyObservation.session_id == sid,
                AnomalyObservation.world_epoch == epoch,
            )
            .order_by(AnomalyObservation.last_observed_at.desc())
            .limit(25)
        ).all()
    )

    active_anoms = [
        a
        for a in anomalies
        if a.lifecycle_state in ("OBSERVED", "INVESTIGATING", "RECOVERY_PENDING")
    ]
    verified_recs = [
        a
        for a in anomalies
        if a.lifecycle_state == "RECOVERED" and a.verification_verdict == "VERIFIED_RECOVERY"
    ]

    # Prompt 14.7 Fix 4: Derive exact active-world Case IDs so approvals, timeline, and KPIs
    # NEVER leak obsolete incidents/proposals from previous world epochs after Reset!
    epoch_case_id_set: set[str] = {a.linked_case_id for a in anomalies if a.linked_case_id}
    job_case_ids = db.scalars(
        select(SupervisorJob.target_case_id).where(
            SupervisorJob.session_id == sid,
            SupervisorJob.world_epoch == epoch,
            SupervisorJob.target_case_id.is_not(None),
        )
    ).all()
    for cid in job_case_ids:
        if cid:
            epoch_case_id_set.add(cid)
    epoch_case_ids = list(epoch_case_id_set)

    pending_props = (
        list(
            db.scalars(
                select(ActionProposal)
                .where(
                    ActionProposal.session_id == sid,
                    ActionProposal.case_id.in_(epoch_case_ids),
                    ActionProposal.status == ActionProposalStatus.PROPOSED.value,
                )
                .order_by(ActionProposal.created_at.desc())
                .limit(20)
            ).all()
        )
        if epoch_case_ids
        else []
    )

    queued_jobs = int(
        db.scalar(
            select(func.count(SupervisorJob.id)).where(
                SupervisorJob.session_id == sid,
                SupervisorJob.world_epoch == epoch,
                SupervisorJob.status.in_(["QUEUED", "RUNNING"]),
            )
        )
        or 0
    )

    # Real baselines for this session — explicitly distinguish historical vs current-world samples
    baselines = list(
        db.scalars(
            select(BehavioralBaseline)
            .where(BehavioralBaseline.session_id == sid)
            .order_by(BehavioralBaseline.last_updated_at.desc())
            .limit(12)
        ).all()
    )
    sup_upd = sup_state.updated_at
    if sup_upd.tzinfo is None:
        sup_upd = sup_upd.replace(tzinfo=UTC)
    current_world_baselines = [
        b
        for b in baselines
        if epoch == 1
        or (
            (
                b.last_updated_at.replace(tzinfo=UTC)
                if b.last_updated_at.tzinfo is None
                else b.last_updated_at
            )
            >= sup_upd
        )
    ]
    baseline_warm = any(b.sample_count >= 5 for b in current_world_baselines)

    cov = dict(binding.last_scan_coverage_json or {}) if binding else {}
    raw_ow = cov.get("open_world")
    ow: dict[str, Any] = raw_ow if isinstance(raw_ow, dict) else {}
    raw_sig = ow.get("signals")
    sig: dict[str, Any] = raw_sig if isinstance(raw_sig, dict) else {}

    # Real business events observed from MiniStore in active world epoch
    norm_events = list(
        db.scalars(
            select(NormalizedEvent)
            .where(
                NormalizedEvent.session_id == sid,
                NormalizedEvent.world_epoch == epoch,
            )
            .order_by(NormalizedEvent.occurred_at.desc())
            .limit(20)
        ).all()
    )

    # Human-readable Guardian timeline scoped strictly to active world epoch
    activities = list(
        db.scalars(
            select(SupervisorActivity)
            .where(
                SupervisorActivity.session_id == sid,
                SupervisorActivity.world_epoch == epoch,
            )
            .order_by(SupervisorActivity.created_at.desc())
            .limit(25)
        ).all()
    )
    case_events = (
        list(
            db.scalars(
                select(CaseEvent)
                .where(
                    CaseEvent.session_id == sid,
                    CaseEvent.case_id.in_(epoch_case_ids),
                )
                .order_by(CaseEvent.created_at.desc())
                .limit(25)
            ).all()
        )
        if epoch_case_ids
        else []
    )

    timeline_items: list[dict[str, Any]] = []
    for act in activities:
        timeline_items.append(
            {
                "id": act.id,
                "kind": act.kind,
                "label_vi": HUMAN_EVENT_LABELS_VI.get(act.kind, act.title_vi or act.kind),
                "detail_vi": act.detail_vi,
                "case_id": act.case_id,
                "job_id": act.job_id,
                "timestamp": act.created_at.isoformat(),
                "technical_trace": _scrub_sensitive_trace(act.metadata_json or {}),
            }
        )
    for cev in case_events:
        if cev.type in HUMAN_EVENT_LABELS_VI:
            timeline_items.append(
                {
                    "id": cev.id,
                    "kind": cev.type,
                    "label_vi": HUMAN_EVENT_LABELS_VI[cev.type],
                    "detail_vi": str(
                        (cev.payload_json or {}).get("decision_summary")
                        or (cev.payload_json or {}).get("detail")
                        or (cev.payload_json or {}).get("reason")
                        or ""
                    )[:240],
                    "case_id": cev.case_id,
                    "job_id": (cev.payload_json or {}).get("supervisor_job_id"),
                    "timestamp": cev.created_at.isoformat(),
                    "technical_trace": _scrub_sensitive_trace(cev.payload_json or {}),
                }
            )
    timeline_items.sort(key=lambda x: str(x["timestamp"]), reverse=True)

    recovery_cmds = list(
        db.scalars(
            select(RecoveryCommand)
            .where(
                RecoveryCommand.session_id == sid,
                RecoveryCommand.world_epoch == epoch,
            )
            .order_by(RecoveryCommand.updated_at.desc())
            .limit(15)
        ).all()
    )
    unresolved_guards = int(
        db.scalar(
            select(func.count(RecoveryCommand.id)).where(
                RecoveryCommand.session_id == sid,
                or_(
                    RecoveryCommand.state.in_(["EXECUTING", "UNKNOWN_OUTCOME"]),
                    and_(
                        RecoveryCommand.state == "MANUAL_REVIEW",
                        RecoveryCommand.execution_certainty.in_(
                            ["UNKNOWN", "COMMAND_EXECUTION_UNKNOWN"]
                        ),
                        RecoveryCommand.uncertainty_resolved_at.is_(None),
                    ),
                ),
            )
        )
        or 0
    )

    # Derive truthful Guardian visual state (NEVER fake "Monitoring" when disconnected!)
    worker_ok = bool(worker_health.get("worker_healthy", False))
    conn_status = _effective_connectivity_status(binding)
    if not worker_ok and conn_status != "CONNECTED":
        guardian_visual_state = "DISCONNECTED"
    elif pending_props:
        guardian_visual_state = "WAITING_APPROVAL"
    elif any(c.state in ("EXECUTING", "VERIFYING") for c in recovery_cmds):
        guardian_visual_state = "RECOVERING"
    elif queued_jobs > 0 or sup_state.status == "INVESTIGATING":
        guardian_visual_state = "INVESTIGATING"
    elif verified_recs and not active_anoms:
        guardian_visual_state = "VERIFIED"
    else:
        guardian_visual_state = "MONITORING"

    total_cases = (
        int(
            db.scalar(
                select(func.count(Case.id)).where(
                    Case.session_id == sid,
                    Case.id.in_(epoch_case_ids),
                )
            )
            or 0
        )
        if epoch_case_ids
        else 0
    )
    waiting_approval_cases = (
        int(
            db.scalar(
                select(func.count(Case.id)).where(
                    Case.session_id == sid,
                    Case.id.in_(epoch_case_ids),
                    Case.state == CaseState.WAITING_APPROVAL.value,
                )
            )
            or 0
        )
        if epoch_case_ids
        else 0
    )

    return {
        "session_id": sid,
        "world_epoch": epoch,
        "workspace_role": "CONTROLLER",
        "sandbox_isolation_mode": _sandbox_isolation_mode(
            binding.commerce_session_id if binding else settings.commerce_session_id
        ),
        "unresolved_uncertainty_guards": unresolved_guards,
        "updated_at": datetime.now(UTC).isoformat(),
        "guardian": {
            "visual_state": guardian_visual_state,
            "supervisor_status": sup_state.status,
            "worker_healthy": worker_ok,
            "connectivity_status": conn_status,
            "model_mode": settings.model_mode.value,
            "model_name": settings.model_name or "deterministic-demo",
            "queued_jobs": queued_jobs,
            "last_scan_at": (
                binding.last_successful_scan_at.isoformat()
                if (binding and binding.last_successful_scan_at)
                else None
            ),
        },
        "kpis": {
            "orders_observed": cov.get("orders_scanned"),
            "payments_observed": cov.get("payments_scanned"),
            "active_anomalies": len(active_anoms),
            "verified_recoveries": len(verified_recs),
            "pending_approvals": max(len(pending_props), waiting_approval_cases),
            "total_anomalies_detected": len(anomalies),
            "total_cases": total_cases,
        },
        "business_health": {
            "telemetry_available": bool(ow.get("telemetry_available", False)),
            "has_samples": bool(ow.get("has_samples", False)),
            "window_id": ow.get("window_id"),
            "rps": sig.get("rps"),
            "p95_latency_ms": sig.get("p95_latency_ms"),
            "error_rate": sig.get("error_rate"),
            "outbox_lag": sig.get("outbox_lag"),
            "unsynced_payments": sig.get("unsynced_payments"),
            "baseline_status": "ESTABLISHED" if baseline_warm else "WARMING_UP",
            "baseline_message_vi": (
                "Baseline vận hành bình thường đã được thiết lập từ dữ liệu thực."
                if baseline_warm
                else "Guardian đang thiết lập trạng thái hoạt động bình thường."
            ),
            "baselines": [
                {
                    "metric_name": b.metric_name,
                    "sample_count": b.sample_count,
                    "median": b.median,
                    "mad": b.mad,
                    "updated_at": b.last_updated_at.isoformat(),
                }
                for b in baselines
            ],
        },
        "anomalies": [
            {
                "id": a.id,
                "detector_code": a.detector_code,
                "anomaly_type": a.anomaly_type,
                "severity": a.severity,
                "lifecycle_state": a.lifecycle_state,
                "affected_order_id": a.affected_order_id,
                "affected_payment_id": a.affected_payment_id,
                "linked_case_id": a.linked_case_id,
                "rationale": a.rationale,
                "verification_verdict": a.verification_verdict,
                "recovery_source": a.recovery_source,
                "first_observed_at": a.first_observed_at.isoformat(),
                "last_observed_at": a.last_observed_at.isoformat(),
                "evidence_refs": a.evidence_refs_json or [],
            }
            for a in anomalies
        ],
        "approvals": [
            {
                "proposal_id": p.id,
                "case_id": p.case_id,
                "investigation_run_id": p.investigation_run_id,
                "action_type": p.action_type,
                "risk_level": p.risk_level,
                "status": p.status,
                "params": p.params_json or {},
                "created_at": p.created_at.isoformat(),
            }
            for p in pending_props
        ],
        "recovery_commands": [
            {
                "id": c.id,
                "command": c.command,
                "state": c.state,
                "execution_certainty": c.execution_certainty,
                "policy_decision": c.policy_decision,
                "target_order_id": c.target_order_id,
                "updated_at": c.updated_at.isoformat(),
            }
            for c in recovery_cmds
        ],
        "recent_business_events": [
            {
                "event_id": ev.event_id,
                "event_type": ev.event_type,
                "order_id": ev.order_id,
                "payment_id": ev.payment_id,
                "occurred_at": ev.occurred_at.isoformat(),
            }
            for ev in norm_events
        ],
        "timeline": timeline_items[:30],
    }


@router.post(
    "/scan",
    summary="Trigger a safe read-only monitor observation tick for the current experience session",
)
def trigger_experience_scan(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Run one bounded scan for the caller's own active controller binding."""
    sid = current_session.id
    sup_state = ensure_supervisor_state(db, sid)
    binding = _require_active_controller_binding(
        db, current_session=current_session, sup_state=sup_state
    )
    if binding is None:
        return {"scanned": False, "connectivity_status": "UNCONFIGURED"}

    summary = run_binding_scan(db, binding=binding, auto_create_cases=True)
    return {
        "scanned": True,
        "connectivity_status": summary.connectivity_status,
        "events_pulled": summary.events_pulled,
        "anomalies_detected": summary.anomalies_detected,
        "cases_created": summary.cases_created,
        "recoveries_verified": summary.recoveries_verified,
        "error": summary.error,
    }


@router.post(
    "/reset",
    summary="Reset only the caller's experience session via world_epoch fencing",
)
def reset_experience_session(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Increment dashboard world_epoch atomically under row lock without touching other sessions.

    Prompt 14.8 + 14.10 Security Invariants:
    - Fail-closed: caller MUST already hold an active enabled controller binding (READ_ONLY_PREVIEW
      guests can NEVER call /reset to self-elevate to CONTROLLER).
    - Acquires SELECT ... FOR UPDATE on SupervisorState so concurrent resets never lose increments.
    - Cancels old PROPOSED and APPROVED (not yet executed) ActionProposals from prior epochs.
    - Cancels stale QUEUED SupervisorJobs from prior epochs.
    - Preserves unresolved uncertain RecoveryCommand mutation guards intact!
    - Never mutates the external Commerce world epoch.
    """
    sid = current_session.id
    settings = get_settings()

    if settings.commerce_service_base_url.strip():
        owner_lock_key = (
            f"sandbox_owner:{settings.commerce_merchant_id}:"
            f"{settings.commerce_session_id}:{settings.commerce_world_epoch}"
        )
        db.execute(sa_text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": owner_lock_key})

    ensure_supervisor_state(db, sid)
    sup_state = db.scalar(
        select(SupervisorState).where(SupervisorState.session_id == sid).with_for_update()
    )
    if sup_state is None:
        sup_state = ensure_supervisor_state(db, sid)

    # Prompt 14.10 P0 Fix: Fail-closed controller verification BEFORE advancing epoch or binding!
    existing_binding = _require_active_controller_binding(
        db, current_session=current_session, sup_state=sup_state
    )
    prev_epoch = sup_state.world_epoch
    now = datetime.now(UTC)

    # Disable previous-epoch binding cleanly before creating the new epoch binding
    if existing_binding is not None:
        existing_binding.is_enabled = False
        existing_binding.updated_at = now

    # Cancel old PROPOSED and APPROVED (undispatched) ActionProposals before advancing epoch
    old_props = list(
        db.scalars(
            select(ActionProposal).where(
                ActionProposal.session_id == sid,
                ActionProposal.status.in_(
                    [
                        ActionProposalStatus.PROPOSED.value,
                        ActionProposalStatus.APPROVED.value,
                    ]
                ),
            )
        ).all()
    )
    expired_proposals = 0
    for prop in old_props:
        prop.status = ActionProposalStatus.CANCELLED.value
        expired_proposals += 1

    # Cancel stale QUEUED SupervisorJobs from previous world epoch
    old_jobs = list(
        db.scalars(
            select(SupervisorJob).where(
                SupervisorJob.session_id == sid,
                SupervisorJob.world_epoch <= prev_epoch,
                SupervisorJob.status == "QUEUED",
            )
        ).all()
    )
    cancelled_jobs = 0
    for j in old_jobs:
        j.status = "CANCELLED"
        j.updated_at = now
        cancelled_jobs += 1

    # Count unresolved uncertain RecoveryCommand guards (NEVER release on dashboard reset!)
    unresolved_guards = _count_unresolved_guards_for_scope(
        db,
        merchant_id=settings.commerce_merchant_id,
        commerce_session_id=settings.commerce_session_id,
        commerce_world_epoch=settings.commerce_world_epoch,
    )

    sup_state.world_epoch += 1
    sup_state.status = "MONITORING"
    sup_state.updated_at = now

    binding_id: str | None = None
    if settings.commerce_service_base_url.strip():
        b = ensure_commerce_binding(
            db,
            session_id=sid,
            connector_id=settings.commerce_connector_id or "default_ministore",
            merchant_id=settings.commerce_merchant_id,
            commerce_session_id=settings.commerce_session_id,
            commerce_world_epoch=settings.commerce_world_epoch,
            is_enabled=True,
        )
        binding_id = b.id

    db.commit()
    return {
        "session_id": sid,
        "world_epoch": sup_state.world_epoch,
        "dashboard_world_epoch": sup_state.world_epoch,
        "commerce_world_epoch": settings.commerce_world_epoch,
        "binding_id": binding_id,
        "expired_proposals_count": expired_proposals,
        "cancelled_jobs_count": cancelled_jobs,
        "unresolved_guard_count": unresolved_guards,
        "status": "RESET_COMPLETE",
    }


@router.post(
    "/release",
    summary="Safely release shared sandbox control if no unresolved uncertain commands exist",
)
def release_experience_ownership(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Release shared sandbox ownership ONLY for the active controller when zero guards exist."""
    sid = current_session.id
    settings = get_settings()
    sup_state = ensure_supervisor_state(db, sid)
    _require_active_controller_binding(db, current_session=current_session, sup_state=sup_state)
    unresolved = _count_unresolved_guards_for_scope(
        db,
        merchant_id=settings.commerce_merchant_id,
        commerce_session_id=settings.commerce_session_id,
        commerce_world_epoch=settings.commerce_world_epoch,
    )
    if unresolved > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="UNRESOLVED_UNCERTAINTY_GUARD_BLOCKS_RELEASE",
        )
    bindings = list(
        db.scalars(
            select(CommerceSourceBinding).where(
                CommerceSourceBinding.session_id == sid,
                CommerceSourceBinding.is_enabled.is_(True),
            )
        ).all()
    )
    for b in bindings:
        b.is_enabled = False
        b.updated_at = datetime.now(UTC)
    db.commit()
    return {
        "session_id": sid,
        "released_bindings": len(bindings),
        "status": "OWNERSHIP_RELEASED",
    }
