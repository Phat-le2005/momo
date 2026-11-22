"""Demo session authentication and tenant isolation dependency."""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.db.session import get_db
from app.models.enums import SessionStatus
from app.models.session import DemoSession

SESSION_HEADER = "X-Demo-Session"


def get_current_session(
    request: Request,
    db: Session = Depends(get_db),
) -> DemoSession:
    """Extract and validate demo session from X-Demo-Session header.

    Invariants:
    - Only accepts the secret session token (never public session ID).
    - Rejects unauthenticated requests with 401 UNAUTHORIZED.
    - Rejects expired sessions with 401 UNAUTHORIZED.
    - Rejects revoked/inactive sessions with 403 FORBIDDEN.
    - Guarantees server-side ownership: callers never trust client-supplied session/tenant IDs.
    """
    token = request.headers.get(SESSION_HEADER, "").strip()
    if not token:
        # Fallback to Authorization: Bearer <token>
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

    if not token:
        raise AppError(
            ErrorCode.UNAUTHORIZED,
            f"Missing required session header: {SESSION_HEADER}",
            status_code=401,
        )

    # Hardening: Only accept secret session token. Never accept public session ID.
    stmt = select(DemoSession).where(DemoSession.token == token)
    session = db.scalar(stmt)
    if session is None:
        raise AppError(
            ErrorCode.UNAUTHORIZED,
            "Invalid or unknown demo session",
            status_code=401,
        )

    if session.is_expired:
        raise AppError(
            ErrorCode.UNAUTHORIZED,
            "Demo session has expired. Please create a new session.",
            status_code=401,
            details={"session_id": session.id, "expired_at": session.expires_at.isoformat()},
        )

    if session.status != SessionStatus.ACTIVE.value:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"Demo session is {session.status.lower()}",
            status_code=403,
            details={"session_id": session.id, "status": session.status},
        )

    return session
