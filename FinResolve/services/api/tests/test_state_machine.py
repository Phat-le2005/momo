"""Tests for Case state machine: transition graph, invariants, and audit events."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.cases.state_machine import can_transition, transition_case
from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.models.actions import Verification
from app.models.cases import Case, CaseEvent
from app.models.enums import CaseState, VerificationVerdict
from app.models.session import DemoSession


@pytest.fixture
def db_session() -> Session:
    maker = get_sessionmaker()
    session = maker()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_case(db_session: Session) -> Case:
    from datetime import UTC, datetime, timedelta

    sess = DemoSession(
        id="sess_sm_test",
        token="dsk_sm_test",
        status="ACTIVE",
        max_cases=20,
        case_count=1,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(sess)

    case = Case(
        id="CASE-SM-001",
        session_id=sess.id,
        type="PAYMENT_DISPUTE",
        goal="Test state transitions",
        state=CaseState.NEW.value,
        version=1,
        owner="guest",
    )
    db_session.add(case)
    db_session.commit()
    return case


def test_allowed_transitions_graph() -> None:
    assert can_transition(CaseState.NEW, CaseState.INVESTIGATING)
    assert can_transition(CaseState.INVESTIGATING, CaseState.EXECUTING)
    assert can_transition(CaseState.EXECUTING, CaseState.VERIFYING)
    assert can_transition(CaseState.VERIFYING, CaseState.RESOLVED)

    # Disallowed jumps
    assert not can_transition(CaseState.NEW, CaseState.RESOLVED)
    assert not can_transition(CaseState.NEW, CaseState.EXECUTING)
    assert not can_transition(CaseState.RESOLVED, CaseState.INVESTIGATING)


def test_valid_transition_pipeline_to_resolved(db_session: Session, test_case: Case) -> None:
    # 1. NEW -> INVESTIGATING
    transition_case(db_session, test_case, CaseState.INVESTIGATING, reason="Bắt đầu điều tra")
    db_session.commit()
    assert test_case.state == CaseState.INVESTIGATING.value
    assert test_case.version == 2

    # 2. INVESTIGATING -> EXECUTING
    transition_case(db_session, test_case, CaseState.EXECUTING, reason="Thực thi đồng bộ đơn")
    db_session.commit()
    assert test_case.state == CaseState.EXECUTING.value
    assert test_case.version == 3

    # 3. EXECUTING -> VERIFYING
    transition_case(db_session, test_case, CaseState.VERIFYING, reason="Kiểm tra hậu điều kiện")
    db_session.commit()
    assert test_case.state == CaseState.VERIFYING.value
    assert test_case.version == 4

    # 4. Attach PASSED verification record matching current case version
    verif = Verification(
        id="VRF-001",
        case_id=test_case.id,
        action_execution_id=None,
        case_version=test_case.version,
        postconditions_json={"order_status": "PAID"},
        verdict=VerificationVerdict.PASSED.value,
    )
    db_session.add(verif)
    db_session.commit()

    # 5. VERIFYING -> RESOLVED
    transition_case(
        db_session, test_case, CaseState.RESOLVED, reason="Xác minh thành công", verification=verif
    )
    db_session.commit()
    assert test_case.state == CaseState.RESOLVED.value
    assert test_case.version == 5

    # Check audit events
    events = (
        db_session.query(CaseEvent)
        .filter(CaseEvent.case_id == test_case.id)
        .order_by(CaseEvent.created_at.asc())
        .all()
    )
    assert len(events) == 4
    assert events[-1].payload_json["to_state"] == CaseState.RESOLVED.value
    assert events[-1].payload_json["version"] == 5


def test_disallowed_direct_transition_new_to_resolved(db_session: Session, test_case: Case) -> None:
    with pytest.raises(AppError) as exc_info:
        transition_case(db_session, test_case, CaseState.RESOLVED, reason="Bỏ qua các bước")
    assert exc_info.value.status_code == 409
    assert "Invalid state transition" in exc_info.value.message


def test_resolved_without_verification_fails(db_session: Session, test_case: Case) -> None:
    # Move to VERIFYING first
    transition_case(db_session, test_case, CaseState.INVESTIGATING, reason="Điều tra")
    transition_case(db_session, test_case, CaseState.EXECUTING, reason="Thực thi")
    transition_case(db_session, test_case, CaseState.VERIFYING, reason="Xác minh")
    db_session.commit()

    # Attempt VERIFYING -> RESOLVED without any verification record
    with pytest.raises(AppError) as exc_info:
        transition_case(
            db_session, test_case, CaseState.RESOLVED, reason="Hoàn tất không cần kiểm chứng"
        )
    assert exc_info.value.status_code == 409
    assert "Cannot mark Case as RESOLVED without a verified postcondition" in exc_info.value.message


def test_resolved_with_failed_verification_fails(db_session: Session, test_case: Case) -> None:
    transition_case(db_session, test_case, CaseState.INVESTIGATING, reason="Điều tra")
    transition_case(db_session, test_case, CaseState.EXECUTING, reason="Thực thi")
    transition_case(db_session, test_case, CaseState.VERIFYING, reason="Xác minh")
    db_session.commit()

    failed_verif = Verification(
        id="VRF-FAILED-001",
        case_id=test_case.id,
        action_execution_id=None,
        postconditions_json={"order_status": "PENDING"},
        verdict=VerificationVerdict.FAILED.value,
    )
    db_session.add(failed_verif)
    db_session.commit()

    with pytest.raises(AppError) as exc_info:
        transition_case(
            db_session,
            test_case,
            CaseState.RESOLVED,
            reason="Thử giải quyết dù xác minh thất bại",
            verification=failed_verif,
        )
    assert exc_info.value.status_code == 409
    assert "Cannot mark Case as RESOLVED without a verified postcondition" in exc_info.value.message
