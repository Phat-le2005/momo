"""Action Proposal inspection, Approval Gateway, and Action Execution routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import AppError, ErrorCode
from app.core.session import get_current_session
from app.db.session import get_db
from app.models.actions import ActionProposal
from app.models.session import DemoSession
from app.policy.approval import ApprovalGateway
from app.policy.contracts import get_action_contract
from app.policy.executor import ActionExecutor
from app.schemas.actions import (
    ActionContractSummaryResponse,
    ActionProposalDetailResponse,
    ApprovalResponse,
    ApproveActionRequest,
    ExecuteActionRequest,
    ExecuteActionResponse,
    VerificationResponse,
)

router = APIRouter(prefix="/actions", tags=["actions"])


@router.get(
    "/{proposal_id}",
    response_model=ActionProposalDetailResponse,
    summary="Inspect an ActionProposal, its BusinessActionContract, approval, and execution status",
)
def get_action_proposal(
    proposal_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> ActionProposalDetailResponse:
    proposal = db.scalar(
        select(ActionProposal)
        .options(
            selectinload(ActionProposal.approval),
            selectinload(ActionProposal.executions),
        )
        .where(
            ActionProposal.id == proposal_id,
            ActionProposal.session_id == current_session.id,
        )
    )
    if proposal is None:
        raise AppError(
            ErrorCode.NOT_FOUND,
            f"ActionProposal '{proposal_id}' not found",
            status_code=404,
        )

    contract = get_action_contract(proposal.action_type)
    appr = proposal.approval
    execs = list(proposal.executions) if proposal.executions else []

    return ActionProposalDetailResponse(
        id=proposal.id,
        case_id=proposal.case_id,
        session_id=proposal.session_id,
        action_type=proposal.action_type,
        params_json=proposal.params_json,
        evidence_refs=proposal.evidence_refs,
        expected_version=proposal.expected_version,
        risk_level=proposal.risk_level,
        status=proposal.status,
        created_at=proposal.created_at,
        contract=ActionContractSummaryResponse(
            action_type=contract.action_type,
            policy_version=contract.policy_version,
            required_evidence_sources=sorted(contract.required_evidence_sources),
            preconditions=list(contract.preconditions),
            required_approver_roles=sorted(contract.required_approver_roles),
            default_risk_level=contract.default_risk_level.value,
            allowed_source_modes=sorted(contract.allowed_source_modes),
            side_effects=list(contract.side_effects),
            expected_postconditions=list(contract.expected_postconditions),
        ),
        approval_id=appr.id if appr else None,
        approval_decision=appr.decision if appr else None,
        approval_role=appr.approver_role if appr else None,
        approval_digest=appr.digest if appr else None,
        approval_expiry=appr.expiry if appr else None,
        executions_count=len(execs),
        latest_execution_state=execs[-1].state if execs else None,
    )


@router.post(
    "/{proposal_id}/approve",
    response_model=ApprovalResponse,
    status_code=status.HTTP_200_OK,
    summary="Approve or reject a proposed business action with cryptographic digest binding",
)
def approve_action_proposal(
    proposal_id: str,
    payload: ApproveActionRequest | None = None,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> ApprovalResponse:
    req = payload or ApproveActionRequest()
    gateway = ApprovalGateway(db, current_session.id)
    approval, proposal, case_obj = gateway.decide_proposal(
        proposal_id=proposal_id,
        approver_role=req.approver_role,
        approver_id=req.approver_id,
        decision=req.decision,
        reason=req.reason,
        expiry_minutes=req.expiry_minutes,
    )
    contract = get_action_contract(proposal.action_type)

    return ApprovalResponse(
        approval_id=approval.id,
        proposal_id=proposal.id,
        case_id=proposal.case_id,
        action_type=contract.action_type,
        decision=approval.decision,
        approver_role=approval.approver_role,
        approver_id=req.approver_id,
        reason=req.reason,
        digest=approval.digest,
        policy_version=contract.policy_version,
        expiry=approval.expiry,
        proposal_status=proposal.status,
        case_state=case_obj.state if case_obj else None,
        case_version=case_obj.version if case_obj else None,
        created_at=approval.created_at,
    )


@router.post(
    "/{proposal_id}/execute",
    response_model=ExecuteActionResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute an approved ActionProposal with policy recheck and read-back verification",
)
def execute_action_proposal(
    proposal_id: str,
    payload: ExecuteActionRequest | None = None,
    idempotency_header: str | None = Header(default=None, alias="X-Idempotency-Key"),
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> ExecuteActionResponse:
    req = payload or ExecuteActionRequest()
    effective_key = idempotency_header or req.idempotency_key or f"IDEMP-EXEC-{proposal_id}"

    executor = ActionExecutor(db, current_session.id)
    outcome = executor.execute_proposal(
        proposal_id=proposal_id,
        idempotency_key=effective_key,
        simulate_timeout=req.simulate_timeout,
        simulate_verification_failure=req.simulate_verification_failure,
    )

    verif_resp: VerificationResponse | None = None
    if outcome.verification is not None:
        v = outcome.verification
        verif_resp = VerificationResponse(
            id=v.id,
            case_id=v.case_id,
            action_execution_id=v.action_execution_id,
            case_version=v.case_version,
            verdict=v.verdict,
            postconditions_json=v.postconditions_json,
            checked_at=v.checked_at,
        )

    return ExecuteActionResponse(
        execution_id=outcome.execution.id,
        proposal_id=outcome.proposal.id,
        case_id=outcome.proposal.case_id,
        action_type=outcome.proposal.action_type,
        idempotency_key=outcome.execution.idempotency_key,
        execution_state=outcome.execution.state,
        proposal_status=outcome.proposal.status,
        case_state=outcome.case.state if outcome.case else None,
        case_version=outcome.case.version if outcome.case else None,
        idempotent_replay=outcome.idempotent_replay,
        result_json=outcome.execution.result_json,
        verification=verif_resp,
        executed_at=outcome.execution.created_at,
    )
