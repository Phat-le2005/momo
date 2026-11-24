"""Demo session authentication, tenant isolation, and server-validated approver capability."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import SourceMode, get_settings
from app.core.errors import AppError, ErrorCode
from app.db.session import get_db
from app.models.enums import SessionStatus
from app.models.session import DemoSession

SESSION_HEADER = "X-Demo-Session"
MANAGER_TOKEN_HEADER = "X-Demo-Manager-Token"  # noqa: S105

ALLOWED_APPROVER_ROLES: frozenset[str] = frozenset({"DEMO_MANAGER", "OPS_MANAGER", "FINANCE_LEAD"})


@dataclass(frozen=True)
class ApproverPrincipal:
    """Server-validated, session-scoped approver identity and role capability.

    Invariants:
    - Never constructed from untrusted request JSON fields (`approver_role` / `approver_id`).
    - Bound cryptographically to a specific `DemoSession` (`session.id` and `session.token`).
    - Strictly restricted to `source_mode == "MOCK"`.
    """

    session_id: str
    role: str
    principal_id: str
    source_mode: str = "MOCK"


def _compute_manager_token_hmac(
    *,
    session_id: str,
    session_token: str,
    role: str,
    principal_id: str,
) -> str:
    secret = get_settings().demo_approval_secret.get_secret_value().encode()
    msg = f"v1|MOCK|{session_id}|{session_token}|{role}|{principal_id}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def issue_demo_manager_token(
    session: DemoSession,
    *,
    role: str = "DEMO_MANAGER",
    principal_id: str | None = None,
) -> str:
    """Issue a server-signed, session-scoped demo-manager capability token for MOCK mode."""
    settings = get_settings()
    if settings.source_mode != SourceMode.MOCK:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "Demo manager capability tokens can only be issued in SOURCE_MODE=MOCK",
            status_code=403,
        )

    norm_role = role.strip().upper()
    if norm_role not in ALLOWED_APPROVER_ROLES:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"Cannot issue demo capability token for unauthorized role '{role}'",
            status_code=403,
        )

    clean_id = (principal_id or f"mgr_{norm_role.lower()}_{session.id[-8:]}").strip()
    if not clean_id or "." in clean_id or len(clean_id) > 64:
        raise AppError(
            ErrorCode.BAD_REQUEST,
            "Invalid principal_id for demo manager token",
            status_code=400,
        )

    sig = _compute_manager_token_hmac(
        session_id=session.id,
        session_token=session.token,
        role=norm_role,
        principal_id=clean_id,
    )
    return f"dmk_{norm_role}.{clean_id}.{sig}"


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
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()

    if not token:
        raise AppError(
            ErrorCode.UNAUTHORIZED,
            f"Missing required session header: {SESSION_HEADER}",
            status_code=401,
        )

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


def get_current_approver_principal(
    request: Request,
    current_session: DemoSession = Depends(get_current_session),
) -> ApproverPrincipal:
    """Validate `X-Demo-Manager-Token` and derive a session-scoped `ApproverPrincipal`.

    Hard Invariants:
    1. Guest token (`X-Demo-Session`) alone has role `GUEST` and is NEVER allowed to self-assert
       `DEMO_MANAGER`, `OPS_MANAGER`, or `FINANCE_LEAD`.
    2. `X-Demo-Manager-Token` must be a valid server-signed HMAC capability bound to
       `current_session.id` and `current_session.token`. A capability from another session
       is rejected with 403 FORBIDDEN.
    3. Strictly restricted to `SOURCE_MODE == MOCK`.
    """
    settings = get_settings()
    if settings.source_mode != SourceMode.MOCK:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "Demo manager approval privilege is restricted strictly to SOURCE_MODE=MOCK",
            status_code=403,
        )

    raw_cap = request.headers.get(MANAGER_TOKEN_HEADER, "").strip()
    if not raw_cap:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "APPROVAL_IMPERSONATION_BLOCKED: Guest session token alone cannot approve actions "
            "or self-assert DEMO_MANAGER, OPS_MANAGER, or FINANCE_LEAD. "
            f"A valid server-issued {MANAGER_TOKEN_HEADER} capability is required.",
            status_code=403,
        )

    if raw_cap == current_session.token or not raw_cap.startswith("dmk_"):
        raise AppError(
            ErrorCode.FORBIDDEN,
            "APPROVAL_IMPERSONATION_BLOCKED: Invalid manager capability token format; "
            "guest session token cannot be used as manager capability",
            status_code=403,
        )

    parts = raw_cap[4:].split(".")
    if len(parts) != 3:
        raise AppError(
            ErrorCode.FORBIDDEN,
            "APPROVAL_IMPERSONATION_BLOCKED: Malformed manager capability token",
            status_code=403,
        )

    role_part, principal_id_part, sig_part = parts
    norm_role = role_part.strip().upper()
    clean_id = principal_id_part.strip()

    if norm_role not in ALLOWED_APPROVER_ROLES or not clean_id:
        raise AppError(
            ErrorCode.FORBIDDEN,
            f"APPROVAL_IMPERSONATION_BLOCKED: Unauthorized capability role '{norm_role}'",
            status_code=403,
        )

    expected_sig = _compute_manager_token_hmac(
        session_id=current_session.id,
        session_token=current_session.token,
        role=norm_role,
        principal_id=clean_id,
    )
    if not hmac.compare_digest(sig_part, expected_sig):
        raise AppError(
            ErrorCode.FORBIDDEN,
            "CROSS_SESSION_OR_FORGED_MANAGER_TOKEN: Manager capability signature mismatch "
            "or token belongs to a different demo session",
            status_code=403,
        )

    return ApproverPrincipal(
        session_id=current_session.id,
        role=norm_role,
        principal_id=clean_id,
        source_mode=SourceMode.MOCK.value,
    )
