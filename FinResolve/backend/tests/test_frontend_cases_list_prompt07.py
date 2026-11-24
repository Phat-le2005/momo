"""Prompt 07: session-scoped Case Center list/API contract tests (PostgreSQL)."""

from fastapi.testclient import TestClient


def test_case_list_is_session_scoped_and_includes_evidence(client: TestClient) -> None:
    a = client.post("/api/v1/demo/sessions", json={"seed_fixtures": False, "max_cases": 20}).json()
    b = client.post("/api/v1/demo/sessions", json={"seed_fixtures": False, "max_cases": 20}).json()
    ha = {"X-Demo-Session": a["token"]}
    hb = {"X-Demo-Session": b["token"]}

    empty = client.get("/api/v1/cases", headers=ha)
    assert empty.status_code == 200
    assert empty.json() == []

    ca = client.post(
        "/api/v1/cases",
        headers=ha,
        json={"text": "Đã thanh toán nhưng đơn vẫn báo chờ", "type": "PAYMENT_DISPUTE"},
    )
    cb = client.post(
        "/api/v1/cases",
        headers=hb,
        json={"text": "Tôi cần kiểm tra khoản hoàn tiền", "type": "REFUND_REQUEST"},
    )
    assert ca.status_code == 201 and cb.status_code == 201

    own_a = client.get("/api/v1/cases", headers=ha)
    own_b = client.get("/api/v1/cases", headers=hb)
    assert own_a.status_code == 200 and own_b.status_code == 200
    assert [c["id"] for c in own_a.json()] == [ca.json()["id"]]
    assert [c["id"] for c in own_b.json()] == [cb.json()["id"]]
    assert own_a.json()[0]["evidence"][0]["kind"] == "CLAIM"
    assert (
        own_a.json()[0]["evidence"][0]["payload_json"]["text"]
        == "Đã thanh toán nhưng đơn vẫn báo chờ"
    )
    assert client.get("/api/v1/cases", headers={}).status_code in (401, 403)
    assert client.get(f"/api/v1/cases/{cb.json()['id']}", headers=ha).status_code == 404


def test_two_sessions_can_run_same_default_seed_without_pk_collision(client: TestClient) -> None:
    """Two separate guest sessions running mega_live with the same default seed stay isolated."""
    a = client.post("/api/v1/demo/sessions", json={"seed_fixtures": False, "max_cases": 40}).json()
    b = client.post("/api/v1/demo/sessions", json={"seed_fixtures": False, "max_cases": 40}).json()
    ha = {"X-Demo-Session": a["token"]}
    hb = {"X-Demo-Session": b["token"]}

    ra = client.post(
        "/api/v1/demo/scenarios/mega_live/run",
        headers=ha,
        json={"seed": 20261122, "order_count": 50, "run_async": False},
    )
    rb = client.post(
        "/api/v1/demo/scenarios/mega_live/run",
        headers=hb,
        json={"seed": 20261122, "order_count": 50, "run_async": False},
    )
    assert ra.status_code == 201 and rb.status_code == 201
    cases_a = {c["id"] for c in client.get("/api/v1/cases", headers=ha).json()}
    cases_b = {c["id"] for c in client.get("/api/v1/cases", headers=hb).json()}
    assert len(cases_a) == 13 and len(cases_b) == 13
    assert cases_a.isdisjoint(cases_b)
