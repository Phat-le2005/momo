"""Merchant-Scoped Verified Incident Experience Memory (Prompt 14 Section 21).

Invariants:
- Only VERIFIED incident outcomes are stored as trusted experience (`is_verified=True`).
- Strictly scoped by `(session_id, merchant_id)` to prevent cross-merchant leakage.
- Retrieval explicitly warns that similar symptoms do NOT guarantee identical root cause!
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.open_world import IncidentExperience


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]+", text.lower()) if len(t) >= 2}


def record_verified_experience(
    db: Session,
    *,
    session_id: str,
    merchant_id: str,
    case_id: str | None,
    symptom_summary: str,
    symptom_tags: list[str],
    validated_root_cause: str,
    evidence_refs: list[str],
    actions_attempted: list[str],
    verified_recovery_steps: list[str],
    policy_version: str = "2.0",
) -> IncidentExperience:
    """Persist a verified incident resolution for future merchant-scoped retrieval."""
    row = IncidentExperience(
        id=f"EXP-{uuid.uuid4().hex[:10].upper()}",
        session_id=session_id,
        merchant_id=merchant_id,
        case_id=case_id,
        symptom_summary=symptom_summary[:1000],
        symptom_tags_json=list(symptom_tags[:20]),
        validated_root_cause=validated_root_cause[:255],
        evidence_refs_json=list(evidence_refs[:20]),
        actions_attempted_json=list(actions_attempted[:20]),
        verified_recovery_steps_json=list(verified_recovery_steps[:20]),
        policy_version=policy_version[:32],
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def retrieve_similar_experiences(
    db: Session,
    *,
    session_id: str,
    merchant_id: str,
    symptom_query: str,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Retrieve similar VERIFIED past incidents strictly within the same merchant."""
    q_toks = _tokens(symptom_query)
    rows = list(
        db.scalars(
            select(IncidentExperience)
            .where(
                IncidentExperience.session_id == session_id,
                IncidentExperience.merchant_id == merchant_id,
                IncidentExperience.is_verified.is_(True),
            )
            .order_by(IncidentExperience.created_at.desc())
            .limit(100)
        ).all()
    )

    scored: list[tuple[float, IncidentExperience]] = []
    for r in rows:
        r_toks = _tokens(
            f"{r.symptom_summary} {' '.join(r.symptom_tags_json or [])} {r.validated_root_cause}"
        )
        overlap = len(q_toks.intersection(r_toks))
        if overlap > 0:
            sim = round(overlap / max(1.0, float(len(q_toks | r_toks))), 3)
            scored.append((sim, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            "experience_id": r.id,
            "similarity": sim,
            "symptom_summary": r.symptom_summary,
            "validated_root_cause": r.validated_root_cause,
            "verified_recovery_steps": list(r.verified_recovery_steps_json or []),
            "policy_version": r.policy_version,
            "caution": (
                "Historical similarity is advisory only; verify current evidence before diagnosing."
            ),
        }
        for sim, r in scored[: max(1, min(limit, 10))]
    ]
