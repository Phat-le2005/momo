"""Tests for multi-tenant / guest session isolation and authentication."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_missing_session_header_rejected(client: TestClient) -> None:
    res = client.post("/api/v1/cases", json={"text": "Khiếu nại thanh toán"})
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"
    assert "Missing required session header" in res.json()["error"]["message"]


def test_invalid_session_token_rejected(client: TestClient) -> None:
    res = client.post(
        "/api/v1/cases",
        headers={"X-Demo-Session": "invalid_token_12345"},
        json={"text": "Khiếu nại thanh toán"},
    )
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHORIZED"


def test_guest_session_isolation_cannot_access_other_case(client: TestClient) -> None:
    # 1. Create Session A
    res_a = client.post("/api/v1/demo/sessions")
    assert res_a.status_code == 201
    token_a = res_a.json()["token"]

    # 2. Create Session B
    res_b = client.post("/api/v1/demo/sessions")
    assert res_b.status_code == 201
    token_b = res_b.json()["token"]

    # 3. Session A creates a Case
    create_res = client.post(
        "/api/v1/cases",
        headers={"X-Demo-Session": token_a},
        json={"text": "Khách hàng Nguyễn Văn A báo đã chuyển tiền nhưng đơn chưa cập nhật"},
    )
    assert create_res.status_code == 201
    case_a_id = create_res.json()["id"]

    # 4. Session A can read its own Case
    read_a = client.get(f"/api/v1/cases/{case_a_id}", headers={"X-Demo-Session": token_a})
    assert read_a.status_code == 200
    assert read_a.json()["id"] == case_a_id

    # 5. QA T18: Session B attempts to read Session A's Case -> 404 (No data leak)
    read_b = client.get(f"/api/v1/cases/{case_a_id}", headers={"X-Demo-Session": token_b})
    assert read_b.status_code == 404
    assert read_b.json()["error"]["code"] == "NOT_FOUND"

    # 6. Session B attempts to read Session A's Case events -> 404
    events_b = client.get(f"/api/v1/cases/{case_a_id}/events", headers={"X-Demo-Session": token_b})
    assert events_b.status_code == 404

    # 7. Session B attempts to read Session A's Case status -> 404
    status_b = client.get(f"/api/v1/cases/{case_a_id}/status", headers={"X-Demo-Session": token_b})
    assert status_b.status_code == 404


def test_expired_session_rejected(client: TestClient) -> None:
    from datetime import UTC, datetime, timedelta

    from app.db.session import get_sessionmaker
    from app.models.session import DemoSession

    # Create an expired session directly in DB
    maker = get_sessionmaker()
    with maker() as db:
        expired_sess = DemoSession(
            id="sess_expired_001",
            token="dsk_expired_001",
            status="ACTIVE",
            max_cases=10,
            case_count=0,
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        db.add(expired_sess)
        db.commit()

    res = client.get(
        "/api/v1/cases/CASE-001",
        headers={"X-Demo-Session": "dsk_expired_001"},
    )
    assert res.status_code == 401
    assert "expired" in res.json()["error"]["message"].lower()
