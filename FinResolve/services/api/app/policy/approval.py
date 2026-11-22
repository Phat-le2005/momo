"""Scoped ApprovalGateway with cryptographic digest binding for FinResolve AI.

Hard Domain Invariants:
1. Digest Binding: Every Approval digest is cryptographically bound to:
   `(session_id, proposal_id, case_id, action_type, canonical_params_json, sorted_evidence_refs,
     expected_version, policy_version, approver_role)`.
   Any change to order_id, amount, parameters, or evidence invalidates the digest (QA T11).
2. Scoped Role Authorization: Only authorized roles in `contract.required_approver_roles`
   (`OPS_MANAGER`, `FINANCE_LEAD`, `DEMO_MANAGER`) within an authenticated demo session can
   approve proposals. Unprivileged roles (`GUEST`, `CUSTOMER`, `AI_AGENT`) are rejected with 403.
3. No Approval Replay: A proposal can only be decided once while in `PROPOSED` status.
4. Policy Pre-Check: `PolicyEngine` validates live preconditions before an `APPROVED` decision
   can be recorded.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cases.state_machine import can_transition, transition_case
from app.core.errors import AppError, ErrorCode
from app.models.actions import ActionProposal, Approval
from app.models.cases import Case, CaseEvent
from app.models.enums import ActionProposalStatus, ApprovalDecision, CaseState
from app.policy.contracts import POLICY_VERSION, get_action_contract
from app.policy.engine import PolicyEngine


def compute_approval_digest(
    *,
    session_id: str,
    proposal_id: str,
    case_id: str | None,
    action_type: str,
    params_json: dict[str, Any],
    evidence_refs: list[str],
    expected_version: int,
    policy_version: str = POLICY_VERSION,
    approver_role: str,
) -> str:
    """Compute canonical SHA-256 digest binding an Approval to exact action parameters & policy."""
    canonical_params = json.dumps(params_json, sort_keys=True, separators=(",", ":"))
    canonical_evidence = json.dumps(sorted(str(e) for e in evidence_refs), separators=(",", ":"))
    payload = (
        f"v1|{session_id}|{proposal_id}|{case_id or ''}|{action_type}|"
        f"{canonical_params}|{canonical_evidence}|{expected_version}|"
        f"{policy_version}|{approver_role.strip().upper()}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_legacy_approval_digest(proposal: ActionProposal) -> str:
    """Backward-compatible digest format used by simulator unit tests in P01."""
    digest_src = (
        f"{proposal.id}:{proposal.action_type}:{proposal.params_json}:{proposal.expected_version}"
    )
    return hashlib.sha256(digest_src.encode("utf-8")).hexdigest()


class ApprovalGateway:
    """Scoped human/demo-manager approval gateway enforcing policy and cryptographic binding."""

    def __init__(self, db: Session, session_id: str) -> None:
        self._db = db
        self._session_id = session_id

    def decide_proposal(
        self,
        *,
        proposal_id: str,
        approver_role: str,
        approver_id: str = "demo_manager_01",
        decision: ApprovalDecision | str = ApprovalDecision.APPROVED,
        reason: str = "Phê duyệt đề xuất hành động sau khi kiểm tra bằng chứng",
        expiry_minutes: int = 120,
    ) -> tuple[Approval, ActionProposal, Case | None]:
        """Record an authenticated, policy-validated approval or rejection on an ActionProposal."""
        decision_str = (
            decision.value if isinstance(decision, ApprovalDecision) else str(decision).upper()
        )
        if decision_str not in (
            ApprovalDecision.APPROVED.value,
            ApprovalDecision.REJECTED.value,
        ):
            raise AppError(
                ErrorCode.BAD_REQUEST,
                f"Invalid approval decision '{decision_str}'",
                status_code=400,
            )

        norm_role = approver_role.strip().upper()
        if not norm_role or norm_role in ("GUEST", "CUSTOMER", "AI", "AI_AGENT", "ANONYMOUS"):
            raise AppError(
                ErrorCode.FORBIDDEN,
                f"Role '{approver_role}' is not permitted to approve or reject business actions",
                status_code=403,
            )

        # 1. Lock ActionProposal row scoped to current session
        proposal = self._db.scalar(
            select(ActionProposal)
            .where(
                ActionProposal.id == proposal_id,
                ActionProposal.session_id == self._session_id,
            )
            .with_for_update()
        )
        if proposal is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"ActionProposal '{proposal_id}' not found in current session",
                status_code=404,
            )

        # 2. Prevent duplicate approval / replay
        if proposal.status != ActionProposalStatus.PROPOSED.value:
            raise AppError(
                ErrorCode.CONFLICT,
                f"ActionProposal '{proposal_id}' cannot be decided because its status is "
                f"'{proposal.status}' (expected 'PROPOSED')",
                status_code=409,
                details={"proposal_id": proposal.id, "current_status": proposal.status},
            )

        contract = get_action_contract(proposal.action_type)
        if norm_role not in contract.required_approver_roles:
            raise AppError(
                ErrorCode.FORBIDDEN,
                f"Approver role '{norm_role}' is not authorized for action "
                f"'{contract.action_type}'. Required: {sorted(contract.required_approver_roles)}",
                status_code=403,
            )

        # 3. If approving, run deterministic PolicyEngine check against current DB state
        policy_engine = PolicyEngine(self._db, self._session_id)
        if decision_str == ApprovalDecision.APPROVED.value:
            eval_res = policy_engine.evaluate(
                proposal,
                approver_role=norm_role,
                lock_rows=True,
            )
            if not eval_res.allowed:
                raise AppError(
                    ErrorCode.CONFLICT,
                    f"PolicyEngine blocked approval ({eval_res.violation_code}): "
                    f"{eval_res.violation_message}",
                    status_code=409,
                    details={
                        "violation_code": eval_res.violation_code,
                        "checked_preconditions": eval_res.checked_preconditions,
                        "context_snapshot": eval_res.context_snapshot,
                    },
                )

        now = datetime.now(UTC)
        expiry = now + timedelta(minutes=expiry_minutes)
        digest = compute_approval_digest(
            session_id=self._session_id,
            proposal_id=proposal.id,
            case_id=proposal.case_id,
            action_type=contract.action_type,
            params_json=proposal.params_json,
            evidence_refs=proposal.evidence_refs,
            expected_version=proposal.expected_version,
            policy_version=contract.policy_version,
            approver_role=norm_role,
        )

        approval = Approval(
            id=f"APPR-{uuid.uuid4().hex[:10].upper()}",
            proposal_id=proposal.id,
            approver_role=norm_role,
            decision=decision_str,
            digest=digest,
            expiry=expiry,
            created_at=now,
        )
        self._db.add(approval)

        if decision_str == ApprovalDecision.APPROVED.value:
            proposal.status = ActionProposalStatus.APPROVED.value
        else:
            proposal.status = ActionProposalStatus.REJECTED.value

        case_obj: Case | None = None
        if proposal.case_id is not None:
            case_obj = self._db.scalar(
                select(Case)
                .where(
                    Case.id == proposal.case_id,
                    Case.session_id == self._session_id,
                )
                .with_for_update()
            )
            if case_obj is not None:
                evt_type = (
                    "ACTION_APPROVED"
                    if decision_str == ApprovalDecision.APPROVED.value
                    else "ACTION_REJECTED"
                )
                evt = CaseEvent(
                    id=f"EVT-{uuid.uuid4().hex[:12].upper()}",
                    case_id=case_obj.id,
                    session_id=self._session_id,
                    type=evt_type,
                    payload_json={
                        "approval_id": approval.id,
                        "proposal_id": proposal.id,
                        "action_type": contract.action_type,
                        "decision": decision_str,
                        "approver_role": norm_role,
                        "approver_id": approver_id,
                        "reason": reason,
                        "digest": digest,
                        "policy_version": contract.policy_version,
                        "expiry": expiry.isoformat(),
                    },
                    created_at=now,
                )
                self._db.add(evt)

                if decision_str == ApprovalDecision.REJECTED.value:
                    curr_state = CaseState(case_obj.state)
                    if can_transition(curr_state, CaseState.ESCALATED):
                        case_obj = transition_case(
                            self._db,
                            case_obj,
                            CaseState.ESCALATED,
                            reason=f"Đề xuất {proposal.id} bị từ chối phê duyệt: {reason}",
                            extra_payload={"approval_id": approval.id, "proposal_id": proposal.id},
                        )

        self._db.commit()
        self._db.refresh(approval)
        self._db.refresh(proposal)
        if case_obj is not None:
            self._db.refresh(case_obj)

        return approval, proposal, case_obj
