"""Tests for Case API: intake, evidence provenance, idempotency, and quota."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_vietnamese_free_text_intake_creates_claim_evidence(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    vietnamese_text = (
        "Tôi đã thanh toán 250.000đ qua ví MoMo mã GD MOMO_TXN_888 lúc 10h15, "
        "nhưng hệ thống báo đơn hàng ORD-SYNC vẫn đang chờ xử lý."
    )

    res = client.post(
        "/api/v1/cases",
        headers={"X-Demo-Session": token},
        json={"text": vietnamese_text, "type": "PAYMENT_DISPUTE"},
    )
    assert res.status_code == 201
    data = res.json()

    assert data["state"] == "NEW"
    assert data["version"] == 1
    assert data["type"] == "PAYMENT_DISPUTE"
    assert data["session_id"] == active_session["id"]

    # Hard Domain Invariant 1:
    # User message is strictly an UNTRUSTED CLAIM; never authenticated payment proof.
    assert len(data["evidence"]) == 1
    evidence = data["evidence"][0]
    assert evidence["kind"] == "CLAIM"
    assert evidence["source"] == "CUSTOMER_MESSAGE"
    assert evidence["source_ref"] == "intake_text"
    assert evidence["payload_json"]["text"] == vietnamese_text
    assert evidence["confidence"] is None

    case_id = data["id"]

    # Check events audit trail
    events_res = client.get(f"/api/v1/cases/{case_id}/events", headers={"X-Demo-Session": token})
    assert events_res.status_code == 200
    events = events_res.json()
    assert len(events) == 1
    assert events[0]["type"] == "CASE_CREATED"
    assert events[0]["payload_json"]["initial_state"] == "NEW"

    # Check status endpoint
    status_res = client.get(f"/api/v1/cases/{case_id}/status", headers={"X-Demo-Session": token})
    assert status_res.status_code == 200
    status_data = status_res.json()
    assert status_data["case_id"] == case_id
    assert status_data["state"] == "NEW"
    assert status_data["version"] == 1


def test_idempotent_client_retry_deduplication(
    client: TestClient, active_session: dict[str, str]
) -> None:
    token = active_session["token"]
    idempotency_key = "idemp-client-key-999"

    # First request
    res1 = client.post(
        "/api/v1/cases",
        headers={"X-Demo-Session": token, "X-Idempotency-Key": idempotency_key},
        json={"text": "Yêu cầu khiếu nại kiểm tra hoàn tiền"},
    )
    assert res1.status_code == 201
    case1 = res1.json()

    # Second request with exact same idempotency key and same text
    res2 = client.post(
        "/api/v1/cases",
        headers={"X-Demo-Session": token, "X-Idempotency-Key": idempotency_key},
        json={"text": "Yêu cầu khiếu nại kiểm tra hoàn tiền"},
    )
    assert res2.status_code == 200  # Replay existing case
    case2 = res2.json()

    # Must return the same Case without duplicate creation
    assert case1["id"] == case2["id"]
    assert case1["version"] == case2["version"]

    # Third request with same idempotency key but DIFFERENT payload -> MUST return 409
    res3 = client.post(
        "/api/v1/cases",
        headers={"X-Demo-Session": token, "X-Idempotency-Key": idempotency_key},
        json={"text": "Nội dung khiếu nại đã bị thay đổi hoàn toàn"},
    )
    assert res3.status_code == 409
    assert res3.json()["error"]["code"] == "CONFLICT"

    # Check session case_count was only incremented once
    read_case = client.get(f"/api/v1/cases/{case1['id']}", headers={"X-Demo-Session": token})
    assert read_case.status_code == 200


def test_session_case_quota_enforcement(client: TestClient) -> None:
    # Create session with quota = 2 cases
    sess_res = client.post("/api/v1/demo/sessions", json={"max_cases": 2})
    assert sess_res.status_code == 201
    token = sess_res.json()["token"]

    # Case 1: OK
    res1 = client.post(
        "/api/v1/cases",
        headers={"X-Demo-Session": token},
        json={"text": "Khiếu nại số 1"},
    )
    assert res1.status_code == 201

    # Case 2: OK
    res2 = client.post(
        "/api/v1/cases",
        headers={"X-Demo-Session": token},
        json={"text": "Khiếu nại số 2"},
    )
    assert res2.status_code == 201

    # Case 3: Rate limited / Quota exceeded
    res3 = client.post(
        "/api/v1/cases",
        headers={"X-Demo-Session": token},
        json={"text": "Khiếu nại số 3 vượt hạn mức"},
    )
    assert res3.status_code == 429
    assert res3.json()["error"]["code"] == "RATE_LIMITED"
    assert "quota exceeded" in res3.json()["error"]["message"].lower()
