"""Prompt 06 / 06.1 Simulator & Demo Scenario routes: catalog, run, status, oracle report, reset."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.core.session import get_current_session
from app.db.session import get_db
from app.models.scenario_runs import SimScenarioRun
from app.models.session import DemoSession
from app.schemas.simulator import (
    RunScenarioRequest,
    ScenarioCatalogItemResponse,
    ScenarioOracleReportResponse,
    ScenarioRunResponse,
    SessionResetResponse,
)
from app.simulator.oracle import evaluate_scenario_run_oracle
from app.simulator.scenarios import (
    ScenarioRegistry,
    recover_stale_runs,
    reset_demo_session_world,
    run_scenario_background_task,
)

router = APIRouter(prefix="/demo", tags=["demo-scenarios"])


def _to_run_response(run: SimScenarioRun) -> ScenarioRunResponse:
    metrics = dict(run.metrics_json or {})
    rec_ids = list(metrics.get("recommended_investigation_case_ids") or [])
    inv_ids = list(metrics.get("investigated_case_ids") or [])
    return ScenarioRunResponse(
        run_id=run.id,
        session_id=run.session_id,
        scenario_id=run.scenario_id,
        seed=run.seed,
        status=run.status,
        progress_pct=run.progress_pct,
        orders_generated=run.orders_generated,
        case_ids=list(run.case_ids_json or []),
        incident_ids=list(run.incident_ids_json or []),
        recommended_investigation_case_ids=rec_ids,
        investigated_case_ids=inv_ids,
        source_mode=run.source_mode,
        metrics=metrics,
        error_message=run.error_message,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


@router.get(
    "/scenarios",
    response_model=list[ScenarioCatalogItemResponse],
    summary="List deterministic synthetic scenarios (payment_sync, refund_risk, mega_live)",
)
def list_demo_scenarios() -> list[ScenarioCatalogItemResponse]:
    return [ScenarioCatalogItemResponse(**item) for item in ScenarioRegistry.list_scenarios()]


@router.post(
    "/scenarios/{scenario_id}/run",
    response_model=ScenarioRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Run a deterministic synthetic scenario synchronously or asynchronously (MOCK only)",
)
def run_demo_scenario(
    scenario_id: str,
    response: Response,
    background_tasks: BackgroundTasks,
    payload: RunScenarioRequest | None = Body(default=None),
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> ScenarioRunResponse:
    req = payload or RunScenarioRequest()
    clean_id, eff_order_count = ScenarioRegistry.validate_scenario_request(
        scenario_id, req.order_count
    )
    registry = ScenarioRegistry()
    run = registry.create_run_record(
        db,
        session_id=current_session.id,
        scenario_id=clean_id,
        seed=req.seed,
        reset_before_run=req.reset_before_run,
    )

    if req.run_async:
        db.commit()
        db.refresh(run)
        background_tasks.add_task(
            run_scenario_background_task,
            run_id=run.id,
            order_count=eff_order_count,
            reset_before_run=req.reset_before_run,
            investigate_selected_exception=req.investigate_selected_exception,
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return _to_run_response(run)

    completed_run = registry.execute_run(
        db,
        run=run,
        order_count=eff_order_count,
        reset_before_run=req.reset_before_run,
        investigate_selected_exception=req.investigate_selected_exception,
    )
    return _to_run_response(completed_run)


@router.get(
    "/runs/{run_id}/status",
    response_model=ScenarioRunResponse,
    summary="Poll status and progress of a synthetic scenario run",
)
def get_scenario_run_status(
    run_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> ScenarioRunResponse:
    if recover_stale_runs(db, current_session.id):
        db.commit()
    run = db.scalar(
        select(SimScenarioRun).where(
            SimScenarioRun.id == run_id.strip(),
            SimScenarioRun.session_id == current_session.id,
        )
    )
    if run is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"Scenario run '{run_id}' not found in current session",
            status_code=404,
        )
    return _to_run_response(run)


@router.get(
    "/runs/{run_id}/report",
    response_model=ScenarioOracleReportResponse,
    summary="Get honest verified outcome report from test-only Oracle scorer (labeled MOCK)",
)
def get_scenario_run_report(
    run_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> ScenarioOracleReportResponse:
    if recover_stale_runs(db, current_session.id):
        db.commit()
    report = evaluate_scenario_run_oracle(
        db,
        session_id=current_session.id,
        run_id=run_id,
    )
    return ScenarioOracleReportResponse(**report)


@router.post(
    "/sessions/reset",
    response_model=SessionResetResponse,
    status_code=status.HTTP_200_OK,
    summary="Reset current guest DemoSession synthetic world for repeatable deterministic runs",
)
def reset_current_demo_session(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> SessionResetResponse:
    deleted = reset_demo_session_world(db, current_session.id)
    db.commit()
    return SessionResetResponse(
        session_id=current_session.id,
        source_mode="MOCK",
        reset_completed=True,
        deleted_counts=deleted,
    )
