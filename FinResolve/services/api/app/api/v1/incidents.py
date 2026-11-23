"""Read-only Incident & Taskforce APIs plus scenario seeding/correlation routes (Prompt 05)."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.session import get_current_session
from app.db.session import get_db
from app.incidents.capabilities import CapabilityRegistry
from app.incidents.correlator import (
    IncidentCorrelator,
    sanitize_no_oracle_leak,
    seed_incident_taskforce_scenario,
)
from app.incidents.taskforce import TaskforceManager
from app.models.cases import Case
from app.models.incidents import AgentTask, Incident
from app.models.session import DemoSession
from app.schemas.incidents import (
    AgentTaskItemResponse,
    IncidentCaseItemResponse,
    IncidentDetailResponse,
    IncidentListResponse,
    IncidentSummaryResponse,
    RoleCapabilityResponse,
    SeededIncidentScenarioResponse,
    SeedIncidentScenarioRequest,
)

router = APIRouter(prefix="/incidents", tags=["incidents"])


def _build_capabilities_catalog() -> list[RoleCapabilityResponse]:
    return [
        RoleCapabilityResponse(
            role=spec.role,
            title_vi=spec.title_vi,
            responsibility_vi=spec.responsibility_vi,
            allowed_read_tools=sorted(spec.allowed_read_tools),
            can_propose_actions=spec.can_propose_actions,
            can_execute_writes=spec.can_execute_writes,
        )
        for spec in CapabilityRegistry.list_specs()
    ]


def _to_task_response(task: AgentTask) -> AgentTaskItemResponse:
    return AgentTaskItemResponse(
        id=task.id,
        incident_id=task.incident_id,
        case_id=task.case_id,
        role=task.role,
        objective=task.objective,
        state=task.state,
        allowed_tools=list(task.allowed_tools or []),
        evidence_refs=list(task.evidence_refs or []),
        handoff_json=sanitize_no_oracle_leak(dict(task.handoff_json or {})),
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _active_incident_tasks(tasks: list[AgentTask] | None) -> list[AgentTask]:
    """Return latest non-superseded tasks for an Incident (falling back to all if none active)."""
    all_tasks = list(tasks or [])
    active = [t for t in all_tasks if not (t.handoff_json or {}).get("superseded")]
    return active if active else all_tasks


def _to_incident_detail_response(db: Session, incident: Incident) -> IncidentDetailResponse:
    settings = get_settings()
    case_ids = [ic.case_id for ic in (incident.incident_cases or [])]
    cases_by_id: dict[str, Case] = {}
    if case_ids:
        loaded_cases = list(
            db.scalars(
                select(Case).where(
                    Case.session_id == incident.session_id,
                    Case.id.in_(case_ids),
                )
            ).all()
        )
        cases_by_id = {c.id: c for c in loaded_cases}

    linked_items: list[IncidentCaseItemResponse] = []
    for ic in incident.incident_cases or []:
        c_obj = cases_by_id.get(ic.case_id)
        linked_items.append(
            IncidentCaseItemResponse(
                id=ic.id,
                incident_id=ic.incident_id,
                case_id=ic.case_id,
                case_state=c_obj.state if c_obj is not None else "NEW",
                case_version=c_obj.version if c_obj is not None else 1,
                link_reason=ic.link_reason,
                shared_evidence_refs=list(ic.shared_evidence_refs or []),
                causal_factors_json=sanitize_no_oracle_leak(dict(ic.causal_factors_json or {})),
                is_auto_resolved=False,
                created_at=ic.created_at,
            )
        )

    safe_factors = sanitize_no_oracle_leak(dict(incident.shared_factors_json or {}))
    rejected = list(safe_factors.get("rejected_lookalikes") or [])

    return IncidentDetailResponse(
        id=incident.id,
        session_id=incident.session_id,
        source_mode="MOCK",
        model_mode="DEMO_RULES",
        configured_model_mode=settings.model_mode.value,
        taskforce_execution_mode="DETERMINISTIC_TASKFORCE_RULES",
        llm_invoked=False,
        title=incident.title,
        hypothesis=incident.hypothesis,
        status=incident.status,
        correlation_key=incident.correlation_key,
        correlation_trace=incident.correlation_trace,
        confidence=incident.confidence,
        shared_factors_json=safe_factors,
        evidence_refs=list(incident.evidence_refs or []),
        linked_cases=linked_items,
        assigned_tasks=[_to_task_response(t) for t in _active_incident_tasks(incident.tasks)],
        rejected_lookalikes=rejected,
        created_at=incident.created_at,
        updated_at=incident.updated_at,
    )


@router.get(
    "",
    response_model=IncidentListResponse,
    summary="List session-scoped correlated Incidents and Taskforce role capabilities",
)
def list_incidents(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> IncidentListResponse:
    settings = get_settings()
    incidents = list(
        db.scalars(
            select(Incident)
            .options(
                selectinload(Incident.incident_cases),
                selectinload(Incident.tasks),
            )
            .where(Incident.session_id == current_session.id)
            .order_by(Incident.created_at.desc())
        ).all()
    )

    items = [
        IncidentSummaryResponse(
            id=inc.id,
            session_id=inc.session_id,
            title=inc.title,
            hypothesis=inc.hypothesis,
            status=inc.status,
            correlation_key=inc.correlation_key,
            correlation_trace=inc.correlation_trace,
            linked_cases_count=len(inc.incident_cases or []),
            assigned_tasks_count=len(_active_incident_tasks(inc.tasks)),
            confidence=inc.confidence,
            shared_factors_json=sanitize_no_oracle_leak(dict(inc.shared_factors_json or {})),
            evidence_refs=list(inc.evidence_refs or []),
            created_at=inc.created_at,
            updated_at=inc.updated_at,
        )
        for inc in incidents
    ]

    return IncidentListResponse(
        session_id=current_session.id,
        source_mode="MOCK",
        model_mode="DEMO_RULES",
        configured_model_mode=settings.model_mode.value,
        taskforce_execution_mode="DETERMINISTIC_TASKFORCE_RULES",
        llm_invoked=False,
        total_count=len(items),
        items=items,
        capabilities_catalog=_build_capabilities_catalog(),
    )


@router.get(
    "/capabilities",
    response_model=list[RoleCapabilityResponse],
    summary="Inspect CapabilityRegistry logical role read-tool permissions",
)
def get_taskforce_capabilities(
    current_session: DemoSession = Depends(get_current_session),
) -> list[RoleCapabilityResponse]:
    _ = current_session
    return _build_capabilities_catalog()


@router.get(
    "/{incident_id}",
    response_model=IncidentDetailResponse,
    summary="Get full Incident details, linked Cases with independent states, and AgentTasks",
)
def get_incident_detail(
    incident_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> IncidentDetailResponse:
    incident = db.scalar(
        select(Incident)
        .options(
            selectinload(Incident.incident_cases),
            selectinload(Incident.tasks),
        )
        .where(
            Incident.id == incident_id.strip(),
            Incident.session_id == current_session.id,
        )
    )
    if incident is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Incident '{incident_id}' not found in current session",
            status_code=404,
        )
    return _to_incident_detail_response(db, incident)


@router.get(
    "/{incident_id}/tasks",
    response_model=list[AgentTaskItemResponse],
    summary="List bounded Taskforce AgentTasks and typed JSON handoffs for an Incident",
)
def list_incident_tasks(
    incident_id: str,
    include_superseded: bool = False,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> list[AgentTaskItemResponse]:
    incident = db.scalar(
        select(Incident)
        .options(selectinload(Incident.tasks))
        .where(
            Incident.id == incident_id.strip(),
            Incident.session_id == current_session.id,
        )
    )
    if incident is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Incident '{incident_id}' not found in current session",
            status_code=404,
        )
    tasks = (
        list(incident.tasks or []) if include_superseded else _active_incident_tasks(incident.tasks)
    )
    return [_to_task_response(t) for t in tasks]


@router.post(
    "/correlate",
    response_model=IncidentListResponse,
    status_code=status.HTTP_200_OK,
    summary="Run IncidentCorrelator and TaskforceManager over current session Cases",
)
def correlate_session_incidents(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> IncidentListResponse:
    correlator = IncidentCorrelator(db, current_session.id)
    outcome = correlator.correlate_session_cases()
    tf_manager = TaskforceManager(db, current_session.id)
    for inc in outcome.incidents:
        tf_manager.assign_and_run_taskforce(inc, force_rerun=True)
    db.commit()
    return list_incidents(current_session=current_session, db=db)


@router.post(
    "/scenarios/seed",
    response_model=SeededIncidentScenarioResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Seed 12 correlated Cases + 2 unrelated lookalikes and run Incident Taskforce",
)
def seed_incident_scenario(
    response: Response,
    payload: SeedIncidentScenarioRequest | None = Body(default=None),
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> SeededIncidentScenarioResponse:
    force_new = payload.force_new if payload is not None else False
    seeded = seed_incident_taskforce_scenario(db, current_session.id, force_new=force_new)
    if seeded.get("idempotent_replay"):
        response.status_code = status.HTTP_200_OK
    return SeededIncidentScenarioResponse(
        session_id=current_session.id,
        source_mode="MOCK",
        model_mode="DEMO_RULES",
        configured_model_mode=get_settings().model_mode.value,
        taskforce_execution_mode="DETERMINISTIC_TASKFORCE_RULES",
        llm_invoked=False,
        **seeded,
    )
