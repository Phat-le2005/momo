"""Action Proposal inspection, Approval Gateway, Demo Simulation, Execution, and Recovery routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import AppError, ErrorCode
from app.core.session import (
    ApproverPrincipal,
    get_current_approver_principal,
    get_current_session,
)
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
    SimulateDemoApprovalRequest,
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
    summary="Approve or reject a proposed action using server-validated manager capability",
)
def approve_action_proposal(
    proposal_id: str,
    payload: ApproveActionRequest | None = None,
    current_session: DemoSession = Depends(get_current_session),
    principal: ApproverPrincipal = Depends(get_current_approver_principal),
    db: Session = Depends(get_db),
) -> ApprovalResponse:
    req = payload or ApproveActionRequest()
    gateway = ApprovalGateway(db, current_session.id)
    approval, proposal, case_obj = gateway.decide_proposal(
        proposal_id=proposal_id,
        principal=principal,
        decision=req.decision,
        reason=req.reason,
        expiry_minutes=req.expiry_minutes,
        claimed_role=req.approver_role,
        claimed_id=req.approver_id,
    )
    contract = get_action_contract(proposal.action_type)

    return ApprovalResponse(
        approval_id=approval.id,
        proposal_id=proposal.id,
        case_id=proposal.case_id,
        action_type=contract.action_type,
        decision=approval.decision,
        approval_mode="AUTHENTICATED_MANAGER",
        is_simulated_demo_approval=False,
        approver_role=approval.approver_role,
        approver_id=approval.approver_id,
        reason=req.reason,
        digest=approval.digest,
        policy_version=approval.policy_version,
        expiry=approval.expiry,
        proposal_status=proposal.status,
        case_state=case_obj.state if case_obj else None,
        case_version=case_obj.version if case_obj else None,
        created_at=approval.created_at,
    )


@router.post(
    "/{proposal_id}/simulate-demo-approval",
    response_model=ApprovalResponse,
    status_code=status.HTTP_200_OK,
    summary="Self-serve MOCK demonstration approval labeled SIMULATED_DEMO_APPROVAL",
)
def simulate_demo_action_approval(
    proposal_id: str,
    payload: SimulateDemoApprovalRequest | None = None,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> ApprovalResponse:
    req = payload or SimulateDemoApprovalRequest()
    gateway = ApprovalGateway(db, current_session.id)
    approval, proposal, case_obj = gateway.simulate_demo_approval(
        proposal_id=proposal_id,
        reason=req.reason,
        expiry_minutes=req.expiry_minutes,
    )
    contract = get_action_contract(proposal.action_type)
    from app.supervisor.service import ensure_supervisor_state, record_activity

    sup_state = ensure_supervisor_state(db, current_session.id)
    record_activity(
        db,
        session_id=current_session.id,
        world_epoch=sup_state.world_epoch,
        case_id=proposal.case_id,
        kind="WAITING_APPROVAL",
        title_vi=f"Người vận hành đã phê duyệt đề xuất {contract.action_type} ({proposal.id})",
        detail_vi=req.reason,
        metadata_json={"proposal_id": proposal.id, "approval_id": approval.id},
    )
    db.commit()

    return ApprovalResponse(
        approval_id=approval.id,
        proposal_id=proposal.id,
        case_id=proposal.case_id,
        action_type=contract.action_type,
        decision=approval.decision,
        approval_mode="SIMULATED_DEMO_APPROVAL",
        is_simulated_demo_approval=True,
        approver_role=approval.approver_role,
        approver_id=approval.approver_id,
        reason=req.reason,
        digest=approval.digest,
        policy_version=approval.policy_version,
        expiry=approval.expiry,
        proposal_status=proposal.status,
        case_state=case_obj.state if case_obj else None,
        case_version=case_obj.version if case_obj else None,
        created_at=approval.created_at,
    )


@router.post(
    "/{proposal_id}/simulate-demo-reject",
    response_model=ApprovalResponse,
    status_code=status.HTTP_200_OK,
    summary="Self-serve MOCK demonstration rejection of a proposed action",
)
def simulate_demo_action_rejection(
    proposal_id: str,
    payload: SimulateDemoApprovalRequest | None = None,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> ApprovalResponse:
    from app.models.enums import ApprovalDecision
    from app.supervisor.service import (
        ensure_supervisor_state,
        recompute_supervisor_idle_status,
        record_activity,
    )

    req = payload or SimulateDemoApprovalRequest(
        reason="Người vận hành từ chối đề xuất từ Hộp thư phê duyệt Mission Control"
    )
    principal = ApproverPrincipal(
        session_id=current_session.id,
        role="DEMO_MANAGER",
        principal_id=f"mgr_demo_{current_session.id[-8:]}",
        source_mode="MOCK",
    )
    gateway = ApprovalGateway(db, current_session.id)
    approval, proposal, case_obj = gateway.decide_proposal(
        proposal_id=proposal_id,
        principal=principal,
        decision=ApprovalDecision.REJECTED,
        reason=req.reason,
        expiry_minutes=req.expiry_minutes,
    )
    contract = get_action_contract(proposal.action_type)
    sup_state = ensure_supervisor_state(db, current_session.id)
    record_activity(
        db,
        session_id=current_session.id,
        world_epoch=sup_state.world_epoch,
        case_id=proposal.case_id,
        kind="DECISION_FINALIZED",
        title_vi=f"Người vận hành đã từ chối đề xuất {contract.action_type} ({proposal.id})",
        detail_vi=req.reason,
        metadata_json={"proposal_id": proposal.id, "decision": "REJECTED"},
    )
    recompute_supervisor_idle_status(db, sup_state)
    db.commit()
    return ApprovalResponse(
        approval_id=approval.id,
        proposal_id=proposal.id,
        case_id=proposal.case_id,
        action_type=contract.action_type,
        decision=approval.decision,
        approval_mode="SIMULATED_DEMO_APPROVAL",
        is_simulated_demo_approval=True,
        approver_role=approval.approver_role,
        approver_id=approval.approver_id,
        reason=req.reason,
        digest=approval.digest,
        policy_version=approval.policy_version,
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
    from app.models.supervisor import ChallengeRun
    from app.supervisor.service import (
        ensure_supervisor_state,
        recompute_supervisor_idle_status,
        record_activity,
    )

    sup_state = ensure_supervisor_state(db, current_session.id)
    verdict_val = outcome.verification.verdict if outcome.verification else "NONE"
    case_state_val = outcome.case.state if outcome.case else None
    record_activity(
        db,
        session_id=current_session.id,
        world_epoch=sup_state.world_epoch,
        case_id=outcome.proposal.case_id,
        kind="DECISION_FINALIZED",
        title_vi=(
            f"Thực thi & Kiểm chứng độc lập {outcome.proposal.action_type}: "
            f"verdict={verdict_val} (Case -> {case_state_val})"
        ),
        detail_vi=(
            "Verifier đọc lại trạng thái cơ sở dữ liệu sau commit để đảm bảo "
            "hậu điều kiện đạt 100%."
        ),
        metadata_json={
            "proposal_id": outcome.proposal.id,
            "execution_state": outcome.execution.state,
            "verification_verdict": verdict_val,
            "case_state": case_state_val,
        },
    )
    if outcome.proposal.case_id and verdict_val == "PASSED":
        challenges = list(
            db.scalars(
                select(ChallengeRun).where(
                    ChallengeRun.session_id == current_session.id,
                    ChallengeRun.world_epoch == sup_state.world_epoch,
                )
            ).all()
        )
        for ch in challenges:
            if outcome.proposal.case_id in (ch.linked_case_ids_json or []):
                ch.status = "COMPLETED"
                ch.safety_verdict = "VERIFIED_RESOLVED"
                ch.outcome_json = {
                    **(ch.outcome_json or {}),
                    "postconditions_verified": True,
                    "requires_human_approval": False,
                    "case_state": case_state_val,
                    "summary_vi": (
                        f"Đã phê duyệt, thực thi {outcome.proposal.action_type} và Verifier "
                        "kiểm chứng hậu điều kiện thành công (PASSED) — hồ sơ chuyển sang RESOLVED."
                    ),
                }
    recompute_supervisor_idle_status(db, sup_state)
    db.commit()
    return _to_execute_response(outcome)


@router.post(
    "/{proposal_id}/verify",
    response_model=ExecuteActionResponse,
    status_code=status.HTTP_200_OK,
    summary="Recover post-commit verification for a committed execution stuck in VERIFYING",
)
def recover_action_verification(
    proposal_id: str,
    current_session: DemoSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> ExecuteActionResponse:
    executor = ActionExecutor(db, current_session.id)
    outcome = executor.recover_verification(proposal_id=proposal_id)
    return _to_execute_response(outcome)


def _to_execute_response(outcome: object) -> ExecuteActionResponse:
    from app.policy.executor import ExecutionOutcome

    assert isinstance(outcome, ExecutionOutcome)  # noqa: S101
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
