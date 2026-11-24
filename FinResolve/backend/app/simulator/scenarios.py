"""ScenarioRegistry and Mini Mega Live Simulator (Prompt 06).

Hard Domain Invariants:
1. Synthetic MOCK World Only: No real-world payment/platform calls; uses fake IDs and
   synthetic Vietnamese customer profiles explicitly labeled `source_mode="MOCK"`.
2. Deterministic & Resettable (QA I05): Same `(scenario_id, seed, order_count)` reproduces
   exact entity IDs, amounts, fault injections, and correlation outcomes. Sessions can be
   cleanly reset and re-run.
3. True World State vs. Observable API:
   - Hidden fault labels and expected ground truth live ONLY in `SimScenarioRun.oracle_truth_json`
     (for test-only Oracle scoring) and are NEVER written to `Evidence`, `Incident`, `AgentTask`,
     or returned by `ReadToolRegistry`.
4. Real DB Fault Injection & Zero Bulk LLM Calls:
   - Faults mutate actual PostgreSQL rows (`SimOrder`, `SimPayment`, `SimPaymentEvent`,
     `SimRefund`, `InventoryLevel`, `InventoryReservation`), not UI-only counters.
   - Normal healthy orders (45-85+ out of 60-100) are processed deterministically in batch code
     without LLM calls; only selected exceptions create `Case` records.
"""

from __future__ import annotations

import random
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.ai_core.model_adapter import ModelAdapter
from app.ai_core.runtime import BoundedAgentRuntime
from app.cases.state_machine import can_transition, transition_case
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.db.session import get_sessionmaker
from app.incidents.correlator import IncidentCorrelator
from app.incidents.taskforce import TaskforceManager
from app.models.actions import ActionProposal
from app.models.cases import Case, Evidence
from app.models.enums import (
    ActionProposalStatus,
    ActionType,
    CaseState,
    EvidenceKind,
    FulfillmentStatus,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
    RiskLevel,
)
from app.models.incidents import AgentTask, Incident, IncidentCase
from app.models.scenario_runs import SimScenarioRun
from app.models.session import DemoSession
from app.models.simulator import (
    InventoryLevel,
    InventoryMovement,
    InventoryReservation,
    OrderItem,
    Product,
    SimOrder,
    SimPayment,
    SimPaymentEvent,
    SimRefund,
    Warehouse,
)
from app.risk.engine import RiskEngine
from app.simulator.services import process_provider_notification, reserve_inventory

SCENARIO_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "scenario_id": "payment_sync",
        "control_label": "Demo paid order mismatch",
        "title_vi": "Đơn hàng đã thanh toán MoMo nhưng chưa đồng bộ trạng thái",
        "description_vi": (
            "Mô phỏng giao dịch MoMo đã xác thực SUCCESS nhưng đơn hàng vẫn ở trạng thái PENDING "
            "(QA T01). AI điều tra, đề xuất ORDER_SYNC, phê duyệt, thực thi và kiểm chứng."
        ),
        "default_order_count": 1,
        "min_order_count": 1,
        "max_order_count": 1,
        "source_mode": "MOCK",
    },
    {
        "scenario_id": "refund_risk",
        "control_label": "Demo duplicate refund",
        "title_vi": "Hoàn tiền một phần hợp lệ & chặn hoàn tiền trùng lặp",
        "description_vi": (
            "Mô phỏng 2 Case đối lập (QA T07 & T10): một yêu cầu hoàn tiền một phần hợp lệ đủ "
            "số dư và một yêu cầu hoàn tiền lặp bị RiskEngine chặn (CONFIRMED_POLICY_VIOLATION)."
        ),
        "default_order_count": 2,
        "min_order_count": 2,
        "max_order_count": 2,
        "source_mode": "MOCK",
    },
    {
        "scenario_id": "mega_live",
        "control_label": "Mini Mega Live (50–100 orders)",
        "title_vi": "Phiên Mega Live mô phỏng 50–100 đơn hàng & sự cố đồng bộ hàng loạt",
        "description_vi": (
            "Sinh 50–100 đơn hàng tổng hợp theo seed cố định cùng hỗn hợp sự cố thực trong DB: "
            "10 đơn nghẽn đồng bộ worker (tạo Incident + Taskforce), 1 IPN trùng lặp (T05), "
            "1 yêu cầu hoàn tiền lặp (T07), 1 tranh chấp tồn kho (T14) và 2 lookalike (I02)."
        ),
        "default_order_count": 60,
        "min_order_count": 50,
        "max_order_count": 100,
        "source_mode": "MOCK",
    },
)

ALLOWED_SCENARIO_IDS: frozenset[str] = frozenset({"payment_sync", "refund_risk", "mega_live"})
SCENARIO_EXPECTED_CASES: dict[str, int] = {
    "payment_sync": 1,
    "refund_risk": 2,
    "mega_live": 13,
}


def recover_stale_runs(
    db: Session,
    session_id: str,
    *,
    stale_after_seconds: int | None = None,
) -> list[str]:
    """Mark stale `RUNNING` scenario runs as `INTERRUPTED` so crashes never wedge a session."""
    timeout_s = (
        stale_after_seconds
        if stale_after_seconds is not None
        else get_settings().demo_stale_run_timeout_seconds
    )
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=max(1, timeout_s))
    stale_runs = list(
        db.scalars(
            select(SimScenarioRun)
            .where(
                SimScenarioRun.session_id == session_id,
                SimScenarioRun.status == "RUNNING",
                SimScenarioRun.updated_at <= cutoff,
            )
            .with_for_update()
        ).all()
    )
    recovered: list[str] = []
    for run in stale_runs:
        run.status = "INTERRUPTED"
        run.error_message = (
            "Background worker interrupted before completion; safe to reset or re-run."
        )
        run.updated_at = now
        recovered.append(run.id)
    if recovered:
        db.flush()
    return recovered


def _supersede_prior_runs(
    db: Session,
    session_id: str,
    *,
    exclude_run_id: str | None = None,
    reason: str = "Superseded by session reset",
) -> int:
    """Mark prior non-superseded runs as SUPERSEDED instead of deleting their audit rows."""
    now = datetime.now(UTC)
    stmt = select(SimScenarioRun).where(
        SimScenarioRun.session_id == session_id,
        SimScenarioRun.status != "SUPERSEDED",
    )
    if exclude_run_id:
        stmt = stmt.where(SimScenarioRun.id != exclude_run_id)
    prior_runs = list(db.scalars(stmt.with_for_update()).all())
    for prior in prior_runs:
        prior.status = "SUPERSEDED"
        if not prior.error_message:
            prior.error_message = reason
        prior.updated_at = now
    if prior_runs:
        db.flush()
    return len(prior_runs)


def reset_demo_session_world(db: Session, session_id: str) -> dict[str, int]:
    """Cleanly delete synthetic world entities for `session_id` and mark prior runs SUPERSEDED."""
    demo_session = db.scalar(
        select(DemoSession).where(DemoSession.id == session_id).with_for_update()
    )
    if demo_session is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"DemoSession '{session_id}' not found",
            status_code=404,
        )

    recover_stale_runs(db, session_id)
    active_run = db.scalar(
        select(SimScenarioRun).where(
            SimScenarioRun.session_id == session_id,
            SimScenarioRun.status == "RUNNING",
        )
    )
    if active_run is not None:
        raise AppError(
            ErrorCode.CONFLICT,
            f"Cannot reset session while scenario run '{active_run.id}' is actively RUNNING",
            status_code=409,
            details={"active_run_id": active_run.id},
        )

    counts: dict[str, int] = {}
    counts["superseded_runs"] = _supersede_prior_runs(
        db, session_id, reason="Superseded by session reset"
    )
    for label, model in (
        ("agent_tasks", AgentTask),
        ("incident_cases", IncidentCase),
        ("incidents", Incident),
        ("cases", Case),
        ("refunds", SimRefund),
        ("payment_events", SimPaymentEvent),
        ("payments", SimPayment),
        ("reservations", InventoryReservation),
        ("movements", InventoryMovement),
        ("inventory_levels", InventoryLevel),
        ("order_items", OrderItem),
        ("orders", SimOrder),
        ("products", Product),
        ("warehouses", Warehouse),
    ):
        res = db.execute(delete(model).where(model.session_id == session_id))
        counts[label] = int(getattr(res, "rowcount", 0) or 0)

    demo_session.case_count = 0
    from app.supervisor.service import bump_world_epoch_on_reset

    counts["world_epoch"] = bump_world_epoch_on_reset(db, session_id)
    db.flush()
    return counts


class ScenarioRegistry:
    """Executes deterministic synthetic scenarios (`payment_sync`, `refund_risk`, `mega_live`)."""

    @staticmethod
    def list_scenarios() -> list[dict[str, Any]]:
        return [dict(item) for item in SCENARIO_CATALOG]

    @staticmethod
    def validate_scenario_request(scenario_id: str, order_count: int | None) -> tuple[str, int]:
        clean_id = scenario_id.strip().lower()
        if clean_id not in ALLOWED_SCENARIO_IDS:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"Unknown scenario_id '{scenario_id}'. Allowed: {sorted(ALLOWED_SCENARIO_IDS)}",
                status_code=404,
            )
        if clean_id == "mega_live":
            eff_count = 60 if order_count is None else order_count
            if eff_count < 50 or eff_count > 100:
                raise AppError(
                    ErrorCode.BAD_REQUEST,
                    "mega_live order_count must be between 50 and 100",
                    status_code=400,
                )
            return clean_id, eff_count
        if clean_id == "refund_risk":
            return clean_id, 2
        return clean_id, 1

    def create_run_record(
        self,
        db: Session,
        *,
        session_id: str,
        scenario_id: str,
        seed: int,
        reset_before_run: bool = False,
    ) -> SimScenarioRun:
        demo_session = db.scalar(
            select(DemoSession).where(DemoSession.id == session_id).with_for_update()
        )
        if demo_session is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"Session '{session_id}' not found",
                status_code=404,
            )

        # 1. Recover any stale/interrupted background runs before checking active lock
        recover_stale_runs(db, session_id)

        # 2. Active-run protection: only one RUNNING scenario per session at a time
        active_run = db.scalar(
            select(SimScenarioRun).where(
                SimScenarioRun.session_id == session_id,
                SimScenarioRun.status == "RUNNING",
            )
        )
        if active_run is not None:
            raise AppError(
                ErrorCode.CONFLICT,
                f"Another scenario run ('{active_run.id}') is currently RUNNING in this session",
                status_code=409,
                details={"active_run_id": active_run.id},
            )

        # 3. Per-session scenario run quota
        max_runs = get_settings().demo_max_scenario_runs_per_session
        total_runs = (
            db.scalar(
                select(func.count(SimScenarioRun.id)).where(SimScenarioRun.session_id == session_id)
            )
            or 0
        )
        if total_runs >= max_runs:
            raise AppError(
                ErrorCode.RATE_LIMITED,
                f"Per-session scenario run quota exceeded ({total_runs}/{max_runs}). "
                "Create a new demo session to continue.",
                status_code=429,
                details={"total_runs": total_runs, "max_runs": max_runs},
            )

        # 4. Per-session Case quota (never silently expand max_cases)
        expected_cases = SCENARIO_EXPECTED_CASES.get(scenario_id, 1)
        effective_case_count = 0 if reset_before_run else demo_session.case_count
        if effective_case_count + expected_cases > demo_session.max_cases:
            raise AppError(
                ErrorCode.RATE_LIMITED,
                f"Session case quota exceeded ({effective_case_count} + {expected_cases} > "
                f"{demo_session.max_cases}). Pass reset_before_run=true or call "
                "POST /api/v1/demo/sessions/reset.",
                status_code=429,
                details={
                    "case_count": effective_case_count,
                    "expected_new_cases": expected_cases,
                    "max_cases": demo_session.max_cases,
                },
            )

        # 5. Safe repeated-run protection: prevent deterministic PK collisions when not resetting
        if not reset_before_run:
            dup_run = db.scalar(
                select(SimScenarioRun).where(
                    SimScenarioRun.session_id == session_id,
                    SimScenarioRun.scenario_id == scenario_id,
                    SimScenarioRun.seed == seed,
                    SimScenarioRun.status == "COMPLETED",
                )
            )
            if dup_run is not None:
                raise AppError(
                    ErrorCode.CONFLICT,
                    f"Scenario '{scenario_id}' with seed {seed} already exists in this session "
                    f"(run '{dup_run.id}'). Pass reset_before_run=true or choose a different seed.",
                    status_code=409,
                    details={
                        "existing_run_id": dup_run.id,
                        "scenario_id": scenario_id,
                        "seed": seed,
                    },
                )

        now = datetime.now(UTC)
        run = SimScenarioRun(
            id=f"RUN-{scenario_id.upper()}-{uuid.uuid4().hex[:8].upper()}",
            session_id=session_id,
            scenario_id=scenario_id,
            seed=seed,
            status="RUNNING",
            progress_pct=5,
            orders_generated=0,
            case_ids_json=[],
            incident_ids_json=[],
            metrics_json={},
            oracle_truth_json={},
            source_mode="MOCK",
            created_at=now,
            updated_at=now,
        )
        db.add(run)
        db.flush()
        return run

    def execute_run(
        self,
        db: Session,
        *,
        run: SimScenarioRun,
        order_count: int,
        reset_before_run: bool = False,
        investigate_selected_exception: bool = False,
        adapter: ModelAdapter | None = None,
    ) -> SimScenarioRun:
        """Execute a scenario run in isolated short-transaction stages (Prompt 08.3):
        1. GENERATE & COMMIT: Create synthetic MOCK orders/payments/Cases and commit BEFORE
           any external LLM invocation (zero PostgreSQL row locks held during LLM I/O).
        2. COORDINATE: Run IncidentCorrelator & TaskforceManager outside long write locks,
           re-checking run status so superseded/interrupted runs are never overwritten.
        3. INVESTIGATE: Run BoundedAgentRuntime in a brand-new independent DB session with
           short transactions (`manage_transactions=True`).
        4. FINALIZE: Persist final metrics and mark `COMPLETED` only if all required stages
           succeeded; record honest partial progress (`FAILED`) if AI stage fails post-commit.
        """
        t0 = time.perf_counter_ns()
        run_id = run.id
        session_id = run.session_id
        scenario_id = run.scenario_id
        seed = run.seed
        mock_committed = False
        metrics: dict[str, Any] = {}

        # --- STAGE 1 & 2: GENERATE SYNTHETIC MOCK DATA & COMMIT ---
        try:
            demo_session = db.scalar(
                select(DemoSession).where(DemoSession.id == session_id).with_for_update()
            )
            if demo_session is None:
                raise AppError(
                    ErrorCode.NOT_FOUND, f"Session '{session_id}' not found", status_code=404
                )

            if reset_before_run:
                _supersede_prior_runs(
                    db,
                    session_id,
                    exclude_run_id=run_id,
                    reason=f"Superseded by reset_before_run for '{run_id}'",
                )
                for model in (
                    AgentTask,
                    IncidentCase,
                    Incident,
                    Case,
                    SimRefund,
                    SimPaymentEvent,
                    SimPayment,
                    InventoryReservation,
                    InventoryMovement,
                    InventoryLevel,
                    OrderItem,
                    SimOrder,
                    Product,
                    Warehouse,
                ):
                    db.execute(delete(model).where(model.session_id == session_id))
                demo_session.case_count = 0
                db.flush()

            if scenario_id == "payment_sync":
                result = self._run_payment_sync(db, session_id=session_id, seed=seed)
            elif scenario_id == "refund_risk":
                result = self._run_refund_risk(db, session_id=session_id, seed=seed)
            else:
                result = self._run_mega_live_generate(
                    db,
                    session_id=session_id,
                    seed=seed,
                    order_count=order_count,
                )

            demo_session.case_count += len(result["case_ids"])
            metrics = dict(result["metrics"])
            recommended_ids = list(result.get("recommended_investigation_case_ids") or [])
            metrics["recommended_investigation_case_ids"] = recommended_ids
            metrics["investigated_case_ids"] = []
            metrics["llm_calls_for_selected_exceptions"] = 0
            metrics["stage"] = "MOCK_DATA_COMMITTED"
            metrics["partial_progress"] = False

            run.progress_pct = 50
            run.orders_generated = int(result["orders_generated"])
            run.case_ids_json = list(result["case_ids"])
            run.incident_ids_json = list(result["incident_ids"])
            run.metrics_json = metrics
            run.oracle_truth_json = dict(result["oracle_truth"])
            run.updated_at = datetime.now(UTC)
            # Commit MOCK data BEFORE any external LLM call (releases DemoSession FOR UPDATE lock!)
            db.commit()
            mock_committed = True
        except Exception as exc:
            db.rollback()
            failed_run = db.get(SimScenarioRun, run_id)
            if failed_run is not None and failed_run.status == "RUNNING":
                failed_run.status = "FAILED"
                failed_run.error_message = str(exc)[:500]
                failed_run.updated_at = datetime.now(UTC)
                db.commit()
            raise

        # --- STAGE 3 & 4: COORDINATE (CORRELATOR/TASKFORCE) & INVESTIGATE (INDEPENDENT SESSION) ---
        incident_ids: list[str] = list(result["incident_ids"])
        try:
            # Verify run was not superseded/interrupted before starting AI stages
            current_run = db.get(SimScenarioRun, run_id)
            if current_run is None or current_run.status != "RUNNING":
                return current_run or run

            if scenario_id == "mega_live":
                t_corr = time.perf_counter_ns()
                # Strictly observable causal correlation — NEVER read oracle_truth for routing!
                correlator = IncidentCorrelator(db, session_id, adapter=adapter)
                outcome = correlator.correlate_session_cases()
                db.commit()

                tf_manager = TaskforceManager(db, session_id, adapter=adapter)
                taskforce_mode = "DETERMINISTIC_TASKFORCE"
                incident_ids = []
                observable_incident_case_ids: list[str] = []
                for inc in outcome.incidents:
                    linked_ids = list(
                        db.scalars(
                            select(IncidentCase.case_id)
                            .where(
                                IncidentCase.session_id == session_id,
                                IncidentCase.incident_id == inc.id,
                            )
                            .order_by(IncidentCase.created_at.asc(), IncidentCase.case_id.asc())
                        ).all()
                    )
                    if not linked_ids:
                        continue
                    for cid in linked_ids:
                        if cid not in observable_incident_case_ids:
                            observable_incident_case_ids.append(cid)
                    tf_tasks = tf_manager.assign_and_run_taskforce(inc)
                    db.commit()
                    refreshed_inc = db.get(Incident, inc.id)
                    if refreshed_inc is not None:
                        sf = refreshed_inc.shared_factors_json or {}
                        if sf.get("llm_output_validated") is True:
                            taskforce_mode = str(
                                sf.get("taskforce_execution_mode") or "LLM_API_ADAPTIVE_TASKFORCE"
                            )
                        else:
                            taskforce_mode = "DETERMINISTIC_TASKFORCE"
                    elif tf_tasks:
                        first_mode = (tf_tasks[0].handoff_json or {}).get("reasoning_mode")
                        if isinstance(first_mode, str) and first_mode:
                            taskforce_mode = first_mode
                    incident_ids.append(inc.id)
                if observable_incident_case_ids:
                    recommended_ids = observable_incident_case_ids[:1]
                    metrics["recommended_investigation_case_ids"] = recommended_ids
                corr_ms = max(1, int((time.perf_counter_ns() - t_corr) / 1_000_000))
                metrics["correlation_ms"] = corr_ms
                metrics["taskforce_mode"] = taskforce_mode
                metrics["stage"] = "COORDINATED"

                # Update progress in a short transaction (guarding against supersede)
                run_after_corr = db.scalar(
                    select(SimScenarioRun).where(SimScenarioRun.id == run_id).with_for_update()
                )
                if run_after_corr is None or run_after_corr.status != "RUNNING":
                    db.rollback()
                    return run_after_corr or run
                run_after_corr.incident_ids_json = list(incident_ids)
                run_after_corr.progress_pct = 80
                run_after_corr.metrics_json = dict(metrics)
                run_after_corr.updated_at = datetime.now(UTC)
                db.commit()

            # Stage 4: Optional selected-exception AI investigation in an INDEPENDENT DB session
            if investigate_selected_exception and recommended_ids:
                check_run = db.get(SimScenarioRun, run_id)
                if check_run is None or check_run.status != "RUNNING":
                    return check_run or run
                selected_case_id = recommended_ids[0]
                with get_sessionmaker()() as inv_db:
                    runtime = BoundedAgentRuntime(
                        inv_db,
                        adapter=adapter,
                        max_steps=6,
                        manage_transactions=True,
                    )
                    inv_result = runtime.investigate_case(
                        case_id=selected_case_id,
                        session_id=session_id,
                    )
                metrics["investigated_case_ids"] = [selected_case_id]
                metrics["selected_exception_ai_mode"] = inv_result.model_mode.value
                metrics["selected_exception_adapter"] = inv_result.adapter_name
                metrics["llm_calls_for_selected_exceptions"] = (
                    1 if inv_result.model_mode.value == "LLM_API" else 0
                )
                metrics["stage"] = "INVESTIGATED"

            # --- STAGE 5: FINALIZE ---
            total_ms = max(1, int((time.perf_counter_ns() - t0) / 1_000_000))
            metrics["total_run_ms"] = total_ms
            metrics["stage"] = "COMPLETED"

            final_run = db.scalar(
                select(SimScenarioRun).where(SimScenarioRun.id == run_id).with_for_update()
            )
            if final_run is None:
                db.rollback()
                return run
            if final_run.status != "RUNNING":
                # Never overwrite a run that was superseded or interrupted mid-flight!
                db.rollback()
                return final_run

            final_run.status = "COMPLETED"
            final_run.progress_pct = 100
            final_run.incident_ids_json = list(incident_ids)
            final_run.metrics_json = metrics
            final_run.updated_at = datetime.now(UTC)
            db.commit()
            db.refresh(final_run)
            return final_run
        except Exception as exc:
            db.rollback()
            failed_run = db.scalar(
                select(SimScenarioRun).where(SimScenarioRun.id == run_id).with_for_update()
            )
            if failed_run is not None and failed_run.status == "RUNNING":
                failed_run.status = "FAILED"
                failed_run.error_message = str(exc)[:500]
                partial_metrics = dict(metrics or failed_run.metrics_json or {})
                if mock_committed:
                    partial_metrics["partial_progress"] = True
                    partial_metrics["stage"] = "AI_STAGE_FAILED_AFTER_MOCK_COMMIT"
                    partial_metrics["ai_stage_error"] = str(exc)[:300]
                failed_run.metrics_json = partial_metrics
                failed_run.incident_ids_json = list(incident_ids)
                failed_run.updated_at = datetime.now(UTC)
                db.commit()
            raise

    def _run_payment_sync(
        self,
        db: Session,
        *,
        session_id: str,
        seed: int,
    ) -> dict[str, Any]:
        t_gen = time.perf_counter_ns()
        rng = random.Random(seed)  # noqa: S311 - deterministic synthetic demo seed
        sess_tag = session_id[-6:].upper()
        tag = f"S{seed}-{sess_tag}-{rng.randint(1000, 9999)}"
        base_ts = datetime(2026, 11, 22, 8, 30, 0, tzinfo=UTC)
        now = datetime.now(UTC)

        ord_id = f"ORD-SYNC-{tag}"
        pay_id = f"PAY-SYNC-{tag}"
        case_id = f"CASE-SYNC-{tag}"

        order = SimOrder(
            id=ord_id,
            session_id=session_id,
            customer_name="Nguyễn Văn Minh (SAMPLE)",
            customer_phone="0901234567",
            amount=250000,
            currency="VND",
            subtotal=250000,
            discount=0,
            tax=0,
            shipping=0,
            grand_total=250000,
            status=OrderStatus.PENDING.value,
            payment_status=OrderPaymentStatus.UNPAID.value,
            fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
            version=1,
            source_mode="MOCK",
            created_at=base_ts,
            updated_at=base_ts,
        )
        payment = SimPayment(
            id=pay_id,
            session_id=session_id,
            order_id=ord_id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref=f"MOMO_SYNC_{tag}",
            provider_order_id=f"MOMO_ORD_{tag}",
            request_id=f"REQ_SYNC_{tag}",
            result_code=0,
            amount=250000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=base_ts,
            observed_at=base_ts,
            verification_source="MOCK_MOMO_ADAPTER_VERIFIED",
            source_mode="MOCK",
            created_at=base_ts,
        )
        case_obj = Case(
            id=case_id,
            session_id=session_id,
            type="PAYMENT_DISPUTE",
            goal=f"Kiểm tra đồng bộ thanh toán cho đơn {ord_id}",
            state=CaseState.NEW.value,
            version=1,
            owner="guest",
            created_at=now,
            updated_at=now,
        )
        db.add_all([order, payment, case_obj])
        db.flush()

        ev_claim = Evidence(
            id=f"EVD-SYNC-CLM-{tag}",
            case_id=case_id,
            session_id=session_id,
            kind=EvidenceKind.CLAIM.value,
            source="CUSTOMER_MESSAGE",
            source_ref="intake_text",
            payload_json={
                "text": (
                    f"Tôi đã thanh toán 250.000đ qua MoMo (mã GD MOMO_SYNC_{tag}) "
                    f"cho đơn hàng {ord_id} nhưng đơn vẫn báo chờ thanh toán."
                )
            },
            observed_at=base_ts,
        )
        db.add(ev_claim)
        db.flush()

        gen_ms = max(1, int((time.perf_counter_ns() - t_gen) / 1_000_000))
        return {
            "orders_generated": 1,
            "case_ids": [case_id],
            "incident_ids": [],
            "recommended_investigation_case_ids": [case_id],
            "metrics": {
                "order_count": 1,
                "generation_ms": gen_ms,
                "correlation_ms": 0,
                "llm_calls_for_bulk_orders": 0,
            },
            "oracle_truth": {
                "scenario_id": "payment_sync",
                "seed": seed,
                "scoped_order_ids": [ord_id],
                "scoped_payment_ids": [pay_id],
                "scoped_inventory_level_ids": [],
                "expected_sync_order_ids": [ord_id],
                "expected_sync_case_ids": [case_id],
                "expected_actionable_case_ids": [case_id],
                "expected_blocked_refund_case_ids": [],
                "expected_lookalike_case_ids": [],
                "expected_incident_case_ids": [],
                "stock_contention_sku": None,
                "expects_duplicate_ipn_check": False,
                "duplicate_ipn_deduplicated": False,
            },
        }

    def _run_refund_risk(
        self,
        db: Session,
        *,
        session_id: str,
        seed: int,
    ) -> dict[str, Any]:
        t_gen = time.perf_counter_ns()
        rng = random.Random(seed)  # noqa: S311 - deterministic synthetic demo seed
        sess_tag = session_id[-6:].upper()
        tag = f"S{seed}-{sess_tag}-{rng.randint(1000, 9999)}"
        base_ts = datetime(2026, 11, 22, 8, 45, 0, tzinfo=UTC)
        now = datetime.now(UTC)

        # 1. Valid partial refund (200k / 500k VND)
        elig_ord_id = f"ORD-REF-ELIG-{tag}"
        elig_pay_id = f"PAY-REF-ELIG-{tag}"
        elig_case_id = f"CASE-REF-ELIG-{tag}"

        elig_order = SimOrder(
            id=elig_ord_id,
            session_id=session_id,
            customer_name="Phạm Hoàng Yến (SAMPLE)",
            customer_phone="0934567890",
            amount=500000,
            currency="VND",
            subtotal=500000,
            grand_total=500000,
            status=OrderStatus.PAID.value,
            payment_status=OrderPaymentStatus.PAID.value,
            fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
            version=1,
            source_mode="MOCK",
            created_at=base_ts,
            updated_at=base_ts,
        )
        elig_pay = SimPayment(
            id=elig_pay_id,
            session_id=session_id,
            order_id=elig_ord_id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref=f"MOMO_REF_ELIG_{tag}",
            provider_order_id=f"MOMO_ORD_ELIG_{tag}",
            request_id=f"REQ_ELIG_{tag}",
            result_code=0,
            amount=500000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=base_ts,
            observed_at=base_ts,
            verification_source="MOCK_MOMO_ADAPTER_VERIFIED",
            source_mode="MOCK",
            created_at=base_ts,
        )
        elig_case = Case(
            id=elig_case_id,
            session_id=session_id,
            type="REFUND_REQUEST",
            goal=f"Hoàn tiền một phần 200.000 VND cho đơn {elig_ord_id}",
            state=CaseState.NEW.value,
            version=1,
            owner="guest",
            created_at=now,
            updated_at=now,
        )

        # 2. Already-refunded order -> duplicate refund blocked by RiskEngine
        dup_ord_id = f"ORD-REF-DUP-{tag}"
        dup_pay_id = f"PAY-REF-DUP-{tag}"
        dup_case_id = f"CASE-REF-DUP-{tag}"

        dup_order = SimOrder(
            id=dup_ord_id,
            session_id=session_id,
            customer_name="Trần Quốc Tuấn (SAMPLE)",
            customer_phone="0934999888",
            amount=500000,
            currency="VND",
            subtotal=500000,
            grand_total=500000,
            status=OrderStatus.REFUNDED.value,
            payment_status=OrderPaymentStatus.REFUNDED.value,
            fulfillment_status=FulfillmentStatus.CANCELLED.value,
            version=2,
            source_mode="MOCK",
            created_at=base_ts,
            updated_at=base_ts,
        )
        dup_pay = SimPayment(
            id=dup_pay_id,
            session_id=session_id,
            order_id=dup_ord_id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref=f"MOMO_REF_DUP_{tag}",
            provider_order_id=f"MOMO_ORD_DUP_{tag}",
            request_id=f"REQ_DUP_{tag}",
            result_code=0,
            amount=500000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=base_ts,
            observed_at=base_ts,
            verification_source="MOCK_MOMO_ADAPTER_VERIFIED",
            source_mode="MOCK",
            created_at=base_ts,
        )
        prior_refund = SimRefund(
            id=f"REF-PRIOR-{tag}",
            session_id=session_id,
            order_id=dup_ord_id,
            payment_id=dup_pay_id,
            amount=500000,
            requested_amount=500000,
            approved_amount=500000,
            completed_amount=500000,
            currency="VND",
            request_id=f"REQ-PRIOR-REF-{tag}",
            status=RefundStatus.COMPLETED.value,
            reason="Đã hoàn tiền trước đó",
            source_mode="MOCK",
            created_at=base_ts,
            updated_at=base_ts,
        )
        dup_case = Case(
            id=dup_case_id,
            session_id=session_id,
            type="REFUND_REQUEST",
            goal=f"Kiểm tra khiếu nại hoàn tiền lặp cho đơn {dup_ord_id}",
            state=CaseState.NEW.value,
            version=1,
            owner="guest",
            created_at=now,
            updated_at=now,
        )
        db.add_all([elig_order, elig_pay, elig_case, dup_order, dup_pay, prior_refund, dup_case])
        db.flush()

        ev_elig_ord = Evidence(
            id=f"EVD-ELIG-ORD-{tag}",
            case_id=elig_case_id,
            session_id=session_id,
            kind=EvidenceKind.OBSERVED_FACT.value,
            source="SIM_ORDERS_DB",
            source_ref=f"sim_orders:{elig_ord_id}",
            payload_json={"order_id": elig_ord_id, "amount": 500000, "status": "PAID"},
            confidence=1.0,
            observed_at=now,
        )
        ev_elig_pay = Evidence(
            id=f"EVD-ELIG-PAY-{tag}",
            case_id=elig_case_id,
            session_id=session_id,
            kind=EvidenceKind.OBSERVED_FACT.value,
            source="PROVIDER_LEDGER_MOCK",
            source_ref=f"sim_payments:{elig_pay_id}",
            payload_json={
                "payment_id": elig_pay_id,
                "order_id": elig_ord_id,
                "amount": 500000,
                "status": "SUCCESS",
                "authoritative_success": True,
            },
            confidence=1.0,
            observed_at=now,
        )
        db.add_all([ev_elig_ord, ev_elig_pay])
        db.flush()

        transition_case(db, elig_case, CaseState.INVESTIGATING, reason="Điều tra hoàn tiền hợp lệ")
        elig_prop = ActionProposal(
            id=f"PROP-REF-ELIG-{tag}",
            case_id=elig_case_id,
            session_id=session_id,
            action_type=ActionType.REFUND_EXECUTE.value,
            params_json={
                "payment_id": elig_pay_id,
                "order_id": elig_ord_id,
                "merchant_id": "MERCHANT_MOCK_01",
                "amount": 200000,
                "currency": "VND",
                "reason": f"Hoàn tiền một phần 200.000 VND cho đơn {elig_ord_id}",
            },
            evidence_refs=[ev_elig_ord.id, ev_elig_pay.id],
            expected_version=elig_case.version,
            risk_level=RiskLevel.MEDIUM.value,
            status=ActionProposalStatus.PROPOSED.value,
            created_at=now,
        )
        db.add(elig_prop)
        db.flush()
        transition_case(
            db,
            elig_case,
            CaseState.WAITING_APPROVAL,
            reason="Đủ hạn mức hoàn tiền; trình phê duyệt hoàn 200.000 VND",
            extra_payload={"proposal_id": elig_prop.id},
        )

        transition_case(db, dup_case, CaseState.INVESTIGATING, reason="Đánh giá rủi ro hoàn lặp")
        risk_engine = RiskEngine(db, session_id)
        eval_dup = risk_engine.evaluate_refund_request(
            requested_amount=500000,
            payment_id=dup_pay_id,
            order_id=dup_ord_id,
            case_id=dup_case_id,
            persist_signals=True,
        )
        if can_transition(CaseState(dup_case.state), CaseState.ESCALATED):
            transition_case(db, dup_case, CaseState.ESCALATED, reason=eval_dup.explanation_vi)

        gen_ms = max(1, int((time.perf_counter_ns() - t_gen) / 1_000_000))
        return {
            "orders_generated": 2,
            "case_ids": [elig_case_id, dup_case_id],
            "incident_ids": [],
            "recommended_investigation_case_ids": [],
            "metrics": {
                "order_count": 2,
                "generation_ms": gen_ms,
                "correlation_ms": 0,
                "llm_calls_for_bulk_orders": 0,
            },
            "oracle_truth": {
                "scenario_id": "refund_risk",
                "seed": seed,
                "scoped_order_ids": [elig_ord_id, dup_ord_id],
                "scoped_payment_ids": [elig_pay_id, dup_pay_id],
                "scoped_inventory_level_ids": [],
                "expected_sync_order_ids": [],
                "expected_sync_case_ids": [],
                "expected_actionable_case_ids": [elig_case_id],
                "expected_valid_partial_refund_case_id": elig_case_id,
                "expected_valid_partial_refund_proposal_id": elig_prop.id,
                "expected_blocked_refund_case_ids": [dup_case_id],
                "expected_lookalike_case_ids": [],
                "expected_incident_case_ids": [],
                "stock_contention_sku": None,
                "expects_duplicate_ipn_check": False,
                "duplicate_ipn_deduplicated": False,
            },
        }

    def _run_mega_live_generate(
        self,
        db: Session,
        *,
        session_id: str,
        seed: int,
        order_count: int,
    ) -> dict[str, Any]:
        """Generate 50-100 synthetic orders in PostgreSQL with deterministic fault injection:
        - 10 dropped order-sync faults sharing worker/batch/error/time-window -> Incident
        - 1 duplicate IPN event (deduplicated safely -> QA T05)
        - 1 duplicate refund attempt on already-refunded order (blocked by RiskEngine -> QA T07)
        - 1 stock contention on hot SKU with available=1 (1 succeeds, 1 rejected -> QA T14)
        - 2 unrelated lookalike Cases with similar Vietnamese complaint text (excluded -> QA I02)
        - Remaining healthy orders processed in code with 0 LLM calls.
        """
        t_gen = time.perf_counter_ns()
        rng = random.Random(seed)  # noqa: S311 - deterministic synthetic demo seed
        sess_tag = session_id[-6:].upper()
        tag = f"ML{seed}-{sess_tag}"
        base_ts = datetime(2026, 11, 22, 9, 0, 0, tzinfo=UTC)
        now = datetime.now(UTC)

        # 1. Seed synthetic warehouse, 2 products (including 1 hot flash-sale SKU), and inventory
        wh_id = f"WH-{tag}"
        wh = Warehouse(
            id=wh_id,
            session_id=session_id,
            code=f"HCM-{tag}",
            name="Kho Mega Live HCM (SAMPLE)",
        )
        prod_hot = Product(
            id=f"PROD-HOT-{tag}",
            session_id=session_id,
            sku=f"SKU-LIVE-HOT-{tag}",
            name="Tai nghe Flash Sale Mega Live (SAMPLE)",
            price=250000,
            currency="VND",
        )
        prod_std = Product(
            id=f"PROD-STD-{tag}",
            session_id=session_id,
            sku=f"SKU-LIVE-STD-{tag}",
            name="Bình giữ nhiệt Mega Live (SAMPLE)",
            price=190000,
            currency="VND",
        )
        db.add_all([wh, prod_hot, prod_std])
        db.flush()

        inv_hot = InventoryLevel(
            id=f"INV-HOT-{tag}",
            session_id=session_id,
            warehouse_id=wh_id,
            product_id=prod_hot.id,
            on_hand=1,
            reserved=0,
            version=1,
        )
        inv_std = InventoryLevel(
            id=f"INV-STD-{tag}",
            session_id=session_id,
            warehouse_id=wh_id,
            product_id=prod_std.id,
            on_hand=500,
            reserved=0,
            version=1,
        )
        db.add_all([inv_hot, inv_std])
        db.flush()

        shared_worker = "SYNC_WORKER_LIVE_07"
        shared_batch = f"BATCH_LIVE_{seed}"
        shared_error = "ERR_DROPPED_WEBHOOK_SYNC"

        dropped_sync_case_ids: list[str] = []
        dropped_sync_order_ids: list[str] = []
        lookalike_case_ids: list[str] = []
        all_case_ids: list[str] = []
        all_order_ids: list[str] = []
        all_payment_ids: list[str] = []
        dup_ipn_order_id = ""
        dup_ipn_payment_id = ""
        dup_ipn_request_id = f"REQ_IPN_{tag}"
        duplicate_ipn_dedup_ok = False
        stock_contention_rejected = False

        # 2. Generate `order_count` synthetic orders deterministically
        # Indices:
        #   1..10: dropped order-sync (10 correlated Cases)
        #   11:    duplicate IPN order (QA T05)
        #   12:    duplicate refund order (QA T07)
        #   13..14: 2 stock contention orders competing for SKU-LIVE-HOT (available=1, QA T14)
        #   15..16: 2 unrelated lookalike Cases (QA I02)
        #   17..order_count: normal healthy PAID orders (0 LLM calls!)
        for idx in range(1, order_count + 1):
            ord_id = f"ORD-{tag}-{idx:03d}"
            pay_id = f"PAY-{tag}-{idx:03d}"
            all_order_ids.append(ord_id)
            all_payment_ids.append(pay_id)
            amount = 250000 if idx <= 16 else rng.choice([190000, 250000, 320000, 450000])
            ev_ts = base_ts + timedelta(seconds=idx * 20)
            cust_name = f"Khách MegaLive #{idx:03d} (SAMPLE)"
            cust_phone = f"0988{seed % 1000:03d}{idx:03d}"

            if 1 <= idx <= 10:
                # Dropped order-sync fault
                sim_ord = SimOrder(
                    id=ord_id,
                    session_id=session_id,
                    customer_name=cust_name,
                    customer_phone=cust_phone,
                    amount=amount,
                    currency="VND",
                    subtotal=amount,
                    grand_total=amount,
                    status=OrderStatus.PENDING.value,
                    payment_status=OrderPaymentStatus.UNPAID.value,
                    fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
                    version=1,
                    source_mode="MOCK",
                    created_at=ev_ts,
                    updated_at=ev_ts,
                )
                sim_pay = SimPayment(
                    id=pay_id,
                    session_id=session_id,
                    order_id=ord_id,
                    merchant_id="MERCHANT_MOCK_01",
                    provider="MOCK_MOMO",
                    provider_ref=f"MOMO_{tag}_{idx:03d}",
                    provider_order_id=f"MOMO_ORD_{tag}_{idx:03d}",
                    request_id=f"REQ_{tag}_{idx:03d}",
                    result_code=0,
                    amount=amount,
                    currency="VND",
                    status=PaymentStatus.SUCCESS.value,
                    verified_at=ev_ts,
                    observed_at=ev_ts,
                    verification_source="MOCK_MOMO_ADAPTER_VERIFIED",
                    source_mode="MOCK",
                    created_at=ev_ts,
                )
                case_id = f"CASE-{tag}-SYNC-{idx:02d}"
                case_obj = Case(
                    id=case_id,
                    session_id=session_id,
                    type="PAYMENT_DISPUTE",
                    goal=f"Đồng bộ thanh toán đơn Mega Live {ord_id}",
                    state=CaseState.NEW.value,
                    version=1,
                    owner="guest",
                    created_at=now,
                    updated_at=now,
                )
                db.add_all([sim_ord, sim_pay, case_obj])
                db.flush()
                db.add_all(
                    [
                        Evidence(
                            id=f"EVD-{tag}-CLM-{idx:02d}",
                            case_id=case_id,
                            session_id=session_id,
                            kind=EvidenceKind.CLAIM.value,
                            source="CUSTOMER_MESSAGE",
                            source_ref="intake_text",
                            payload_json={
                                "text": (
                                    f"Tôi đã thanh toán {amount:,}đ qua MoMo cho đơn hàng "
                                    f"{ord_id} nhưng vẫn báo chờ thanh toán!"
                                )
                            },
                            observed_at=ev_ts,
                        ),
                        Evidence(
                            id=f"EVD-{tag}-FCT-{idx:02d}",
                            case_id=case_id,
                            session_id=session_id,
                            kind=EvidenceKind.OBSERVED_FACT.value,
                            source="ORDER_EVENT_LOG_DB",
                            source_ref=f"sim_orders:{ord_id}",
                            payload_json={
                                "order_id": ord_id,
                                "payment_id": pay_id,
                                "provider": "MOCK_MOMO",
                                "payment_status": "SUCCESS",
                                "order_status": "PENDING",
                                "worker_id": shared_worker,
                                "batch_id": shared_batch,
                                "error_code": shared_error,
                                "event_timestamp": ev_ts.isoformat(),
                                "amount": amount,
                                "currency": "VND",
                            },
                            confidence=1.0,
                            observed_at=ev_ts,
                        ),
                    ]
                )
                dropped_sync_case_ids.append(case_id)
                dropped_sync_order_ids.append(ord_id)
                all_case_ids.append(case_id)

            elif idx == 11:
                # Duplicate IPN event fault (QA T05)
                sim_ord = SimOrder(
                    id=ord_id,
                    session_id=session_id,
                    customer_name=cust_name,
                    customer_phone=cust_phone,
                    amount=amount,
                    currency="VND",
                    subtotal=amount,
                    grand_total=amount,
                    status=OrderStatus.PENDING.value,
                    payment_status=OrderPaymentStatus.UNPAID.value,
                    fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
                    version=1,
                    source_mode="MOCK",
                    created_at=ev_ts,
                    updated_at=ev_ts,
                )
                sim_pay = SimPayment(
                    id=pay_id,
                    session_id=session_id,
                    order_id=ord_id,
                    merchant_id="MERCHANT_MOCK_01",
                    provider="MOCK_MOMO",
                    provider_ref=f"MOMO_DUP_IPN_{tag}",
                    provider_order_id=f"MOMO_ORD_IPN_{tag}",
                    request_id=f"REQ_IPN_{tag}",
                    result_code=None,
                    amount=amount,
                    currency="VND",
                    status=PaymentStatus.PENDING.value,
                    verified_at=None,
                    source_mode="MOCK",
                    created_at=ev_ts,
                )
                db.add_all([sim_ord, sim_pay])
                db.flush()

                # Send identical verified IPN webhook twice -> second must deduplicate!
                _, is_new_1 = process_provider_notification(
                    db,
                    session_id=session_id,
                    provider="MOCK_MOMO",
                    provider_ref=f"MOMO_DUP_IPN_{tag}",
                    amount=amount,
                    status=PaymentStatus.SUCCESS,
                    currency="VND",
                    merchant_id="MERCHANT_MOCK_01",
                    request_id=f"REQ_IPN_{tag}",
                    order_id=ord_id,
                    is_trusted=True,
                    adapter_verified=True,
                )
                _, is_new_2 = process_provider_notification(
                    db,
                    session_id=session_id,
                    provider="MOCK_MOMO",
                    provider_ref=f"MOMO_DUP_IPN_{tag}",
                    amount=amount,
                    status=PaymentStatus.SUCCESS,
                    currency="VND",
                    merchant_id="MERCHANT_MOCK_01",
                    request_id=f"REQ_IPN_{tag}",
                    order_id=ord_id,
                    is_trusted=True,
                    adapter_verified=True,
                )
                duplicate_ipn_dedup_ok = is_new_1 is True and is_new_2 is False
                dup_ipn_order_id = ord_id
                dup_ipn_payment_id = pay_id

            elif idx == 12:
                # Duplicate refund attempt on already-refunded order (QA T07)
                sim_ord = SimOrder(
                    id=ord_id,
                    session_id=session_id,
                    customer_name=cust_name,
                    customer_phone=cust_phone,
                    amount=amount,
                    currency="VND",
                    subtotal=amount,
                    grand_total=amount,
                    status=OrderStatus.REFUNDED.value,
                    payment_status=OrderPaymentStatus.REFUNDED.value,
                    fulfillment_status=FulfillmentStatus.CANCELLED.value,
                    version=2,
                    source_mode="MOCK",
                    created_at=ev_ts,
                    updated_at=ev_ts,
                )
                sim_pay = SimPayment(
                    id=pay_id,
                    session_id=session_id,
                    order_id=ord_id,
                    merchant_id="MERCHANT_MOCK_01",
                    provider="MOCK_MOMO",
                    provider_ref=f"MOMO_DUP_REF_{tag}",
                    result_code=0,
                    amount=amount,
                    currency="VND",
                    status=PaymentStatus.SUCCESS.value,
                    verified_at=ev_ts,
                    observed_at=ev_ts,
                    verification_source="MOCK_MOMO_ADAPTER_VERIFIED",
                    source_mode="MOCK",
                    created_at=ev_ts,
                )
                prior_ref = SimRefund(
                    id=f"REF-{tag}-PRIOR",
                    session_id=session_id,
                    order_id=ord_id,
                    payment_id=pay_id,
                    amount=amount,
                    requested_amount=amount,
                    approved_amount=amount,
                    completed_amount=amount,
                    currency="VND",
                    request_id=f"REQ-REF-{tag}-PRIOR",
                    status=RefundStatus.COMPLETED.value,
                    reason="Đã hoàn tiền trước đó",
                    source_mode="MOCK",
                    created_at=ev_ts,
                    updated_at=ev_ts,
                )
                dup_ref_case_id = f"CASE-{tag}-DUPREF"
                dup_ref_case = Case(
                    id=dup_ref_case_id,
                    session_id=session_id,
                    type="REFUND_REQUEST",
                    goal=f"Kiểm tra khiếu nại hoàn tiền lặp đơn {ord_id}",
                    state=CaseState.NEW.value,
                    version=1,
                    owner="guest",
                    created_at=now,
                    updated_at=now,
                )
                db.add_all([sim_ord, sim_pay, prior_ref, dup_ref_case])
                db.flush()
                transition_case(db, dup_ref_case, CaseState.INVESTIGATING, reason="Đánh giá rủi ro")
                risk_eval = RiskEngine(db, session_id).evaluate_refund_request(
                    requested_amount=amount,
                    payment_id=pay_id,
                    order_id=ord_id,
                    case_id=dup_ref_case_id,
                    persist_signals=True,
                )
                if can_transition(CaseState(dup_ref_case.state), CaseState.ESCALATED):
                    transition_case(
                        db, dup_ref_case, CaseState.ESCALATED, reason=risk_eval.explanation_vi
                    )
                all_case_ids.append(dup_ref_case_id)

            elif idx in (13, 14):
                # Stock contention orders competing for 1 remaining unit of prod_hot (QA T14)
                sim_ord = SimOrder(
                    id=ord_id,
                    session_id=session_id,
                    customer_name=cust_name,
                    customer_phone=cust_phone,
                    amount=amount,
                    currency="VND",
                    subtotal=amount,
                    grand_total=amount,
                    status=OrderStatus.PAID.value,
                    payment_status=OrderPaymentStatus.PAID.value,
                    fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
                    version=1,
                    source_mode="MOCK",
                    created_at=ev_ts,
                    updated_at=ev_ts,
                )
                sim_pay = SimPayment(
                    id=pay_id,
                    session_id=session_id,
                    order_id=ord_id,
                    merchant_id="MERCHANT_MOCK_01",
                    provider="MOCK_MOMO",
                    provider_ref=f"MOMO_STK_{tag}_{idx:02d}",
                    result_code=0,
                    amount=amount,
                    currency="VND",
                    status=PaymentStatus.SUCCESS.value,
                    verified_at=ev_ts,
                    observed_at=ev_ts,
                    verification_source="MOCK_MOMO_ADAPTER_VERIFIED",
                    source_mode="MOCK",
                    created_at=ev_ts,
                )
                db.add_all([sim_ord, sim_pay])
                db.flush()
                if idx == 13:
                    reserve_inventory(
                        db,
                        session_id=session_id,
                        order_id=ord_id,
                        warehouse_id=wh_id,
                        product_id=prod_hot.id,
                        quantity=1,
                    )
                    stock_contention_rejected = False
                else:
                    try:
                        reserve_inventory(
                            db,
                            session_id=session_id,
                            order_id=ord_id,
                            warehouse_id=wh_id,
                            product_id=prod_hot.id,
                            quantity=1,
                        )
                    except AppError:
                        stock_contention_rejected = True

            elif idx in (15, 16):
                # 2 unrelated lookalike Cases with identical complaint text (QA I02)
                is_first_look = idx == 15
                l_worker = "CHECKOUT_GATEWAY_W02" if is_first_look else "PARTNER_CALLBACK_W19"
                l_batch = f"BATCH_OTHER_{seed}_1" if is_first_look else f"BATCH_OTHER_{seed}_2"
                l_err = (
                    "ERR_USER_OTP_EXPIRED" if is_first_look else "ERR_MERCHANT_SIGNATURE_MISMATCH"
                )
                l_pay_stat = (
                    PaymentStatus.FAILED.value if is_first_look else PaymentStatus.PENDING.value
                )
                l_res_code = 1006 if is_first_look else 7002
                l_ts = base_ts + timedelta(hours=3 if is_first_look else -6)

                sim_ord = SimOrder(
                    id=ord_id,
                    session_id=session_id,
                    customer_name=cust_name,
                    customer_phone=cust_phone,
                    amount=amount,
                    currency="VND",
                    subtotal=amount,
                    grand_total=amount,
                    status=OrderStatus.PENDING.value,
                    payment_status=OrderPaymentStatus.UNPAID.value,
                    fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
                    version=1,
                    source_mode="MOCK",
                    created_at=l_ts,
                    updated_at=l_ts,
                )
                sim_pay = SimPayment(
                    id=pay_id,
                    session_id=session_id,
                    order_id=ord_id,
                    merchant_id="MERCHANT_MOCK_01",
                    provider="MOCK_MOMO",
                    provider_ref=f"MOMO_LOOK_{tag}_{idx:02d}",
                    result_code=l_res_code,
                    amount=amount,
                    currency="VND",
                    status=l_pay_stat,
                    verified_at=None,
                    source_mode="MOCK",
                    created_at=l_ts,
                )
                l_case_id = f"CASE-{tag}-LOOK-{idx - 14:02d}"
                l_case = Case(
                    id=l_case_id,
                    session_id=session_id,
                    type="PAYMENT_DISPUTE",
                    goal=f"Kiểm tra đơn Mega Live {ord_id}",
                    state=CaseState.NEW.value,
                    version=1,
                    owner="guest",
                    created_at=now,
                    updated_at=now,
                )
                db.add_all([sim_ord, sim_pay, l_case])
                db.flush()
                db.add_all(
                    [
                        Evidence(
                            id=f"EVD-{tag}-LCLM-{idx:02d}",
                            case_id=l_case_id,
                            session_id=session_id,
                            kind=EvidenceKind.CLAIM.value,
                            source="CUSTOMER_MESSAGE",
                            source_ref="intake_text",
                            payload_json={
                                "text": (
                                    f"Tôi đã thanh toán {amount:,}đ qua MoMo cho đơn hàng "
                                    f"{ord_id} nhưng vẫn báo chờ thanh toán!"
                                )
                            },
                            observed_at=l_ts,
                        ),
                        Evidence(
                            id=f"EVD-{tag}-LFCT-{idx:02d}",
                            case_id=l_case_id,
                            session_id=session_id,
                            kind=EvidenceKind.OBSERVED_FACT.value,
                            source="ORDER_EVENT_LOG_DB",
                            source_ref=f"sim_orders:{ord_id}",
                            payload_json={
                                "order_id": ord_id,
                                "payment_id": pay_id,
                                "provider": "MOCK_MOMO",
                                "payment_status": l_pay_stat,
                                "order_status": "PENDING",
                                "worker_id": l_worker,
                                "batch_id": l_batch,
                                "error_code": l_err,
                                "event_timestamp": l_ts.isoformat(),
                                "amount": amount,
                                "currency": "VND",
                            },
                            confidence=1.0,
                            observed_at=l_ts,
                        ),
                    ]
                )
                lookalike_case_ids.append(l_case_id)
                all_case_ids.append(l_case_id)

            else:
                # Normal healthy orders (processed in batch code, ZERO LLM calls!)
                sim_ord = SimOrder(
                    id=ord_id,
                    session_id=session_id,
                    customer_name=cust_name,
                    customer_phone=cust_phone,
                    amount=amount,
                    currency="VND",
                    subtotal=amount,
                    grand_total=amount,
                    status=OrderStatus.PAID.value,
                    payment_status=OrderPaymentStatus.PAID.value,
                    fulfillment_status=FulfillmentStatus.FULFILLED.value,
                    version=1,
                    source_mode="MOCK",
                    created_at=ev_ts,
                    updated_at=ev_ts,
                )
                sim_pay = SimPayment(
                    id=pay_id,
                    session_id=session_id,
                    order_id=ord_id,
                    merchant_id="MERCHANT_MOCK_01",
                    provider="MOCK_MOMO",
                    provider_ref=f"MOMO_OK_{tag}_{idx:03d}",
                    result_code=0,
                    amount=amount,
                    currency="VND",
                    status=PaymentStatus.SUCCESS.value,
                    verified_at=ev_ts,
                    observed_at=ev_ts,
                    verification_source="MOCK_MOMO_ADAPTER_VERIFIED",
                    source_mode="MOCK",
                    created_at=ev_ts,
                )
                db.add_all([sim_ord, sim_pay])

        db.flush()
        gen_ms = max(1, int((time.perf_counter_ns() - t_gen) / 1_000_000))

        recommended_ids = dropped_sync_case_ids[:1]
        return {
            "orders_generated": order_count,
            "case_ids": all_case_ids,
            "incident_ids": [],
            "recommended_investigation_case_ids": recommended_ids,
            "metrics": {
                "order_count": order_count,
                "healthy_orders_processed_in_batch": order_count - 16,
                "exception_cases_created": len(all_case_ids),
                "llm_calls_for_bulk_orders": 0,
                "taskforce_mode": "DETERMINISTIC_TASKFORCE_RULES",
                "generation_ms": gen_ms,
                "correlation_ms": 0,
            },
            "oracle_truth": {
                "scenario_id": "mega_live",
                "seed": seed,
                "order_count": order_count,
                "scoped_order_ids": all_order_ids,
                "scoped_payment_ids": all_payment_ids,
                "scoped_inventory_level_ids": [inv_hot.id, inv_std.id],
                "expected_sync_order_ids": dropped_sync_order_ids,
                "expected_sync_case_ids": dropped_sync_case_ids,
                "expected_actionable_case_ids": dropped_sync_case_ids,
                "expected_incident_case_ids": dropped_sync_case_ids,
                "expected_blocked_refund_case_ids": [f"CASE-{tag}-DUPREF"],
                "expected_lookalike_case_ids": lookalike_case_ids,
                "stock_contention_sku": prod_hot.sku,
                "stock_contention_product_id": prod_hot.id,
                "stock_contention_second_rejected": stock_contention_rejected,
                "expects_duplicate_ipn_check": True,
                "duplicate_ipn_deduplicated": duplicate_ipn_dedup_ok,
                "expected_duplicate_ipn_order_id": dup_ipn_order_id,
                "expected_duplicate_ipn_payment_id": dup_ipn_payment_id,
                "expected_duplicate_ipn_request_id": dup_ipn_request_id,
            },
        }


def run_scenario_background_task(
    *,
    run_id: str,
    order_count: int,
    reset_before_run: bool,
    investigate_selected_exception: bool = False,
) -> None:
    """Async background runner executing ScenarioRegistry in a fresh PostgreSQL session."""
    maker = get_sessionmaker()
    with maker() as db:
        run = db.get(SimScenarioRun, run_id)
        if run is None or run.status != "RUNNING":
            return
        ScenarioRegistry().execute_run(
            db,
            run=run,
            order_count=order_count,
            reset_before_run=reset_before_run,
            investigate_selected_exception=investigate_selected_exception,
        )
