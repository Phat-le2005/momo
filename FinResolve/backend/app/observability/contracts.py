"""Canonical Source-Aware Observation Contract (Prompt 14 Sections 7-8).

Safely stores business, operational, security/risk, unstructured, and unknown
observation categories with bounded payload sizes, explicit provenance, freshness,
and strict separation of `OBSERVED_FACT`, `CLAIM`, `DERIVED_STAT`, and `HYPOTHESIS`.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.open_world import BusinessObservation

ALLOWED_OBSERVATION_CATEGORIES: frozenset[str] = frozenset(
    {"BUSINESS", "OPERATIONAL", "SECURITY_RISK", "UNSTRUCTURED", "UNKNOWN"}
)
ALLOWED_EVIDENCE_KINDS: frozenset[str] = frozenset(
    {"OBSERVED_FACT", "CLAIM", "DERIVED_STAT", "HYPOTHESIS"}
)
MAX_ATTRIBUTES_BYTES = 16_384


def record_business_observation(
    db: Session,
    *,
    session_id: str,
    world_epoch: int,
    merchant_id: str,
    commerce_session_id: str,
    source_system: str,
    category: str,
    observation_type: str,
    evidence_kind: str = "OBSERVED_FACT",
    correlation_id: str | None = None,
    entity_refs: dict[str, Any] | None = None,
    attributes: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    verification_status: str = "UNVERIFIED",
    freshness_ms: int = 0,
    observed_at: datetime | None = None,
) -> BusinessObservation:
    """Persist a canonical open-world observation safely (unknown categories -> `UNKNOWN`)."""
    norm_cat = category.strip().upper()
    if norm_cat not in ALLOWED_OBSERVATION_CATEGORIES:
        norm_cat = "UNKNOWN"

    norm_kind = evidence_kind.strip().upper()
    if norm_kind not in ALLOWED_EVIDENCE_KINDS:
        norm_kind = "CLAIM"

    safe_attrs = dict(attributes or {})
    raw_len = len(json.dumps(safe_attrs, default=str).encode("utf-8"))
    if raw_len > MAX_ATTRIBUTES_BYTES:
        safe_attrs = {
            "_truncated": True,
            "_original_bytes": raw_len,
            "summary": str(safe_attrs)[:1000],
        }

    row = BusinessObservation(
        id=f"OBS-{uuid.uuid4().hex[:12].upper()}",
        session_id=session_id,
        world_epoch=world_epoch,
        merchant_id=merchant_id,
        commerce_session_id=commerce_session_id,
        source_system=source_system[:64],
        category=norm_cat,
        observation_type=observation_type[:64],
        evidence_kind=norm_kind,
        correlation_id=correlation_id[:128] if correlation_id else None,
        entity_refs_json=dict(entity_refs or {}),
        attributes_json=safe_attrs,
        provenance_json=dict(provenance or {}),
        verification_status=verification_status[:32],
        freshness_ms=max(0, int(freshness_ms)),
        observed_at=observed_at or datetime.now(UTC),
    )
    db.add(row)
    db.flush()
    return row
