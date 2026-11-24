"""Demo session creation and management routes."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.rate_limit import enforce_session_creation_rate_limit
from app.db.seed import seed_demo_session
from app.db.session import get_db
from app.models.enums import SessionStatus
from app.models.session import DemoSession
from app.schemas.session import CreateSessionRequest, DemoSessionResponse

router = APIRouter(prefix="/demo", tags=["demo"])


@router.post(
    "/sessions",
    response_model=DemoSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new isolated guest demo session (never returns manager capability)",
)
def create_demo_session(
    payload: CreateSessionRequest | None = None,
    _rate_limit: None = Depends(enforce_session_creation_rate_limit),
    db: Session = Depends(get_db),
) -> DemoSessionResponse:
    if payload is None:
        payload = CreateSessionRequest()

    now = datetime.now(UTC)
    session_id = f"sess_{uuid.uuid4().hex[:16]}"
    token = f"dsk_{secrets.token_urlsafe(24)}"
    expires_at = now + timedelta(minutes=payload.ttl_minutes)

    demo_session = DemoSession(
        id=session_id,
        token=token,
        status=SessionStatus.ACTIVE.value,
        max_cases=payload.max_cases,
        case_count=0,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )
    db.add(demo_session)
    db.flush()

    if payload.seed_fixtures:
        seed_demo_session(db, session_id)

    db.commit()
    db.refresh(demo_session)

    return DemoSessionResponse(
        session_id=demo_session.id,
        token=demo_session.token,
        status=demo_session.status,
        max_cases=demo_session.max_cases,
        case_count=demo_session.case_count,
        expires_at=demo_session.expires_at,
        created_at=demo_session.created_at,
    )
