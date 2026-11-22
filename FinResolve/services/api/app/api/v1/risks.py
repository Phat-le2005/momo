"""Risk Center routes: scoped risk signal listing, rules catalog, evaluation, and seeding."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.session import get_current_session
from app.db.session import get_db
from app.models.enums import RiskResponse
from app.models.risk import RiskSignal
from app.models.session import DemoSession
from app.policy.contracts import POLICY_VERSION
from app.risk.engine import RISK_RULES_CATALOG, RiskEngine, seed_risk_refund_cases
from app.schemas.risk import (
    EvaluateRefundRiskRequest,
    RefundBalanceLedgerResponse,
    RefundRiskEvaluationResponse,
    RiskCenterListResponse,
    RiskRuleDocResponse,
    RiskSignalDetailResponse,
    SeededRiskScenariosResponse,
)

router = APIRouter(prefix="/risks", tags=["risks"])


@router.get(
    "",
    response_model=RiskCenterListResponse,
    summary="List session-scoped RiskSignals, summary counts, and rule/blind-spot catalog",
)
def list_risk_signals(
    case_id: str | None = Query(default=None, description="Filter by Case ID"),
    category: str | None = Query(default=None, description="Filter by RiskCategory"),
    response: str | None = Query(default=None, description="Filter by RiskResponse"),
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> RiskCenterListResponse:
    stmt = (
        select(RiskSignal)
        .where(RiskSignal.session_id == current_session.id)
        .order_by(RiskSignal.created_at.desc())
    )
    if case_id:
        stmt = stmt.where(RiskSignal.case_id == case_id.strip())
    if category:
        stmt = stmt.where(RiskSignal.category == category.strip().upper())
    if response:
        stmt = stmt.where(RiskSignal.response == response.strip().upper())

    signals = list(db.scalars(stmt).all())

    items = [
        RiskSignalDetailResponse(
            id=s.id,
            case_id=s.case_id,
            session_id=s.session_id,
            category=s.category,
            level=s.level,
            response=s.response,
            rule_code=str(s.details_json.get("rule_code"))
            if s.details_json.get("rule_code")
            else None,
            explanation_vi=str(s.details_json.get("explanation_vi"))
            if s.details_json.get("explanation_vi")
            else None,
            evidence_refs=s.evidence_refs,
            source_refs=[str(r) for r in (s.details_json.get("source_refs") or [])],
            details_json=s.details_json,
            created_at=s.created_at,
        )
        for s in signals
    ]

    hold_count = sum(1 for s in signals if s.response == RiskResponse.HOLD_ACTION.value)
    review_count = sum(1 for s in signals if s.response == RiskResponse.REVIEW.value)
    observe_count = sum(1 for s in signals if s.response == RiskResponse.OBSERVE.value)

    return RiskCenterListResponse(
        session_id=current_session.id,
        source_mode="MOCK",
        policy_version=POLICY_VERSION,
        total_count=len(items),
        hold_action_count=hold_count,
        review_count=review_count,
        observe_count=observe_count,
        items=items,
        rules_catalog=[RiskRuleDocResponse(**rule) for rule in RISK_RULES_CATALOG],
    )


@router.post(
    "/evaluate-refund",
    response_model=RefundRiskEvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="Deterministically evaluate a refund request against ledger balance and risk rules",
)
def evaluate_refund_risk(
    payload: EvaluateRefundRiskRequest,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> RefundRiskEvaluationResponse:
    engine = RiskEngine(db, current_session.id)
    eval_res = engine.evaluate_refund_request(
        requested_amount=payload.requested_amount,
        payment_id=payload.payment_id,
        order_id=payload.order_id,
        expected_merchant_id=payload.merchant_id,
        expected_currency=payload.currency,
        request_id=payload.request_id,
        case_id=payload.case_id,
        has_ambiguity=payload.has_ambiguity,
        persist_signals=bool(payload.case_id),
    )
    if payload.case_id:
        db.commit()

    return RefundRiskEvaluationResponse(
        allowed=eval_res.allowed,
        recommended_response=eval_res.recommended_response,
        requested_amount=eval_res.requested_amount,
        explanation_vi=eval_res.explanation_vi,
        ledger=RefundBalanceLedgerResponse(
            payment_id=eval_res.ledger.payment_id,
            order_id=eval_res.ledger.order_id,
            currency=eval_res.ledger.currency,
            payment_status=eval_res.ledger.payment_status,
            payment_verified=eval_res.ledger.payment_verified,
            eligible_amount=eval_res.ledger.eligible_amount,
            settled_refunds=eval_res.ledger.settled_refunds,
            pending_or_held_refunds=eval_res.ledger.pending_or_held_refunds,
            refundable_remainder=eval_res.ledger.refundable_remainder,
            refund_records_count=eval_res.ledger.refund_records_count,
            source_refs=eval_res.ledger.source_refs,
        ),
        signals=[
            {
                "signal_id": sig.persisted_signal_id,
                "category": sig.category,
                "level": sig.level,
                "response": sig.response,
                "rule_code": sig.rule_code,
                "explanation_vi": sig.explanation_vi,
                "source_refs": sig.source_refs,
                "details": sig.details,
            }
            for sig in eval_res.signals
        ],
    )


@router.post(
    "/scenarios/seed",
    response_model=SeededRiskScenariosResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Seed the two Prompt 04 refund scenarios (valid partial & blocked duplicate refund)",
)
def seed_refund_risk_scenarios(
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> SeededRiskScenariosResponse:
    seeded = seed_risk_refund_cases(db, current_session.id)
    return SeededRiskScenariosResponse(
        session_id=current_session.id,
        source_mode="MOCK",
        **seeded,
    )
