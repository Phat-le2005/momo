"""Isolated Judge-Only Blind Challenge Lab API (Prompt 13 Part D).

Security & Isolation Invariants:
- Protected strictly by `X-Judge-Token` (`settings.judge_admin_secret`).
- Ordinary guest sessions and Agent read tools have ZERO access to these endpoints
  or to `BlindChallengeRun.ground_truth_json`.
"""

from __future__ import annotations

import hmac
import random
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.session import get_current_session
from app.db.session import get_db
from app.judge.eval_harness import run_baseline_comparison_harness
from app.judge.oracle import evaluate_blind_challenge
from app.models.judge import BlindChallengeRun
from app.models.monitoring import CommerceSourceBinding
from app.models.session import DemoSession
from app.monitoring.service import build_connector_for_binding
from app.supervisor.service import ensure_supervisor_state

router = APIRouter(prefix="/judge", tags=["judge-blind-challenge-lab"])

ALLOWED_FAMILIES: tuple[str, ...] = (
    "C01",
    "C02",
    "C03",
    "C04",
    "C05",
    "C06",
    "C07",
    "C08",
    "NEGATIVE_CONTROL",
)


def _require_judge_token(x_judge_token: str = Header(default="")) -> None:
    expected = get_settings().judge_admin_secret.get_secret_value().strip()
    if not expected or not hmac.compare_digest(x_judge_token.strip(), expected):
        raise AppError(
            ErrorCode.UNAUTHORIZED,
            "JUDGE_AUTH_REQUIRED: Invalid or missing X-Judge-Token",
            status_code=401,
        )


class CreateBlindChallengeRequest(BaseModel):
    challenge_family: str | None = Field(default=None, max_length=32)
    random_selection: bool = False
    seed: int = Field(default=42, ge=0, le=2_147_483_647)
    merchant_id: str | None = Field(default=None, max_length=64)
    commerce_session_id: str | None = Field(default=None, max_length=64)
    injection_config: dict[str, Any] = Field(default_factory=dict)
    affected_entities: dict[str, Any] = Field(default_factory=dict)
    ground_truth: dict[str, Any] = Field(default_factory=dict)
    expected_constraints: dict[str, Any] = Field(default_factory=dict)


def _run_to_dict(r: BlindChallengeRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "session_id": r.session_id,
        "world_epoch": r.world_epoch,
        "merchant_id": r.merchant_id,
        "commerce_session_id": r.commerce_session_id,
        "challenge_family": r.challenge_family,
        "seed": r.seed,
        "state": r.state,
        "injection_config": r.injection_config_json,
        "affected_entities": r.affected_entities_json,
        "hidden_ground_truth": r.ground_truth_json,
        "expected_constraints": r.expected_constraints_json,
        "oracle_evaluation": r.evaluation_json,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "evaluated_at": r.evaluated_at.isoformat() if r.evaluated_at else None,
        "created_at": r.created_at.isoformat(),
    }


@router.post("/blind-challenges")
def create_blind_challenge(
    payload: CreateBlindChallengeRequest,
    current_session: DemoSession = Depends(get_current_session),
    _: None = Depends(_require_judge_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    settings = get_settings()
    sup = ensure_supervisor_state(db, current_session.id)
    if payload.random_selection or not payload.challenge_family:
        rng = random.Random(payload.seed)  # noqa: S311
        family = rng.choice(ALLOWED_FAMILIES)
    else:
        family = payload.challenge_family.strip().upper()
        if family not in ALLOWED_FAMILIES:
            raise AppError(
                ErrorCode.BAD_REQUEST,
                f"Invalid challenge_family '{family}'",
                status_code=400,
            )

    now = datetime.now(UTC)
    run = BlindChallengeRun(
        id=f"BCHAL-{uuid.uuid4().hex[:10].upper()}",
        session_id=current_session.id,
        world_epoch=sup.world_epoch,
        merchant_id=(payload.merchant_id or settings.commerce_merchant_id).strip(),
        commerce_session_id=(payload.commerce_session_id or settings.commerce_session_id).strip(),
        challenge_family=family,
        seed=payload.seed,
        state="RUNNING",
        injection_config_json=dict(payload.injection_config),
        affected_entities_json=dict(payload.affected_entities),
        ground_truth_json=dict(payload.ground_truth),
        expected_constraints_json=dict(payload.expected_constraints),
        started_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return _run_to_dict(run)


@router.get("/blind-challenges")
def list_blind_challenges(
    current_session: DemoSession = Depends(get_current_session),
    _: None = Depends(_require_judge_token),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = list(
        db.scalars(
            select(BlindChallengeRun)
            .where(BlindChallengeRun.session_id == current_session.id)
            .order_by(BlindChallengeRun.created_at.desc())
            .limit(50)
        ).all()
    )
    return [_run_to_dict(r) for r in rows]


@router.post("/blind-challenges/{run_id}/evaluate")
def evaluate_challenge_run(
    run_id: str,
    current_session: DemoSession = Depends(get_current_session),
    _: None = Depends(_require_judge_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = db.scalar(
        select(BlindChallengeRun).where(
            BlindChallengeRun.id == run_id,
            BlindChallengeRun.session_id == current_session.id,
        )
    )
    if run is None:
        raise AppError(
            ErrorCode.NOT_FOUND, f"BlindChallengeRun '{run_id}' not found", status_code=404
        )
    binding = db.scalar(
        select(CommerceSourceBinding).where(
            CommerceSourceBinding.session_id == current_session.id,
            CommerceSourceBinding.merchant_id == run.merchant_id,
        )
    )
    conn = build_connector_for_binding(binding) if (binding and binding.base_url) else None
    evaluation = evaluate_blind_challenge(db, run=run, read_connector=conn)
    return {"run": _run_to_dict(run), "evaluation": evaluation}


@router.post("/blind-challenges/{run_id}/abort")
def abort_challenge_run(
    run_id: str,
    current_session: DemoSession = Depends(get_current_session),
    _: None = Depends(_require_judge_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = db.scalar(
        select(BlindChallengeRun).where(
            BlindChallengeRun.id == run_id,
            BlindChallengeRun.session_id == current_session.id,
        )
    )
    if run is None:
        raise AppError(
            ErrorCode.NOT_FOUND, f"BlindChallengeRun '{run_id}' not found", status_code=404
        )
    now = datetime.now(UTC)
    run.state = "ABORTED"
    run.completed_at = now
    run.updated_at = now
    db.commit()
    db.refresh(run)
    return _run_to_dict(run)


@router.post("/evaluation-harness/run")
def api_run_evaluation_harness(
    seed: int = 42,
    current_session: DemoSession = Depends(get_current_session),
    _: None = Depends(_require_judge_token),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return run_baseline_comparison_harness(seed=seed, db=db, session_id=current_session.id)
