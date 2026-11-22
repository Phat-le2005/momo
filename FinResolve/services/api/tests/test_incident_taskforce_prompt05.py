"""Comprehensive PostgreSQL integration tests for Prompt 05:
Adaptive Incident Taskforce (I01–I04).

Acceptance Matrix (prompts/05_incident_taskforce.md & docs/QA_MATRIX.md):
- I01: 12 cases with common causal OBSERVED_FACT (worker_id, batch_id, error_code, time window)
       are correlated into an Incident with trace:
       'Detected 12 potentially linked cases by dropped-sync worker ID and time range',
       and zero hidden simulator oracle fields are ever leaked.
- I02: Unrelated lookalike cases with identical/similar customer complaint text (CLAIM) but
       different causal OBSERVED_FACT metadata are never merged into the Incident. Pure complaint
       text similarity never creates an Incident.
- I03: Shared incident evidence, taskforce completion, incident mitigation, or resolving one
       child Case via Policy/Approval/Verifier NEVER auto-verifies or auto-resolves the other
       child Cases.
- I04: CapabilityRegistry and ScopedRoleToolExecutor enforce strict read-tool scopes for
       FINANCE, OPERATIONS, RISK, and SUPPORT (all writes forbidden; out-of-scope tools raise
       403 ROLE_SCOPE_VIOLATION), and TaskforceManager persists 3 bounded role tasks with typed
       JSON handoffs and CaseEvent audit traces.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.connectors.read_tools import ReadToolRegistry
from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.incidents.capabilities import CapabilityRegistry, ScopedRoleToolExecutor
from app.incidents.correlator import FORBIDDEN_ORACLE_KEYS
from app.incidents.taskforce import TaskHandoffPayload
from app.models.actions import Verification
from app.models.cases import Case, Evidence
from app.models.enums import CaseState, IncidentStatus, TaskforceRole, VerificationVerdict
from app.models.incidents import Incident


def test_i01_correlates_12_cases_by_causal_evidence_with_zero_oracle_leak(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """QA I01:
    - Seed 12 correlated Cases + 2 unrelated lookalikes via `POST /api/v1/incidents/scenarios/seed`.
    - Verify the 12 cases sharing `worker_id="SYNC_WORKER_W07"`, `batch_id="BATCH_20261122_0900"`,
      and `error_code="ERR_DROPPED_WEBHOOK_SYNC"` are grouped into one Incident.
    - Verify correlation_trace includes:
      'Detected 12 potentially linked cases by dropped-sync worker ID and time range'.
    - Verify zero hidden oracle keys leak even if injected into raw Evidence JSON."""
    token = active_session["token"]
    headers = {"X-Demo-Session": token}

    # 1. Seed Prompt 05 scenario (201 CREATED)
    seed_res = client.post("/api/v1/incidents/scenarios/seed", headers=headers)
    assert seed_res.status_code == 201
    seeded = seed_res.json()
    assert seeded["idempotent_replay"] is False
    assert len(seeded["correlated_case_ids"]) == 12
    assert len(seeded["unrelated_lookalike_case_ids"]) == 2
    assert len(seeded["assigned_task_ids"]) == 3
    assert (
        "Detected 12 potentially linked cases by dropped-sync worker ID and time range"
        in seeded["correlation_trace"]
    )

    # Idempotent replay on second seed call without force_new returns 200 OK
    replay_res = client.post("/api/v1/incidents/scenarios/seed", headers=headers)
    assert replay_res.status_code == 200
    assert replay_res.json()["idempotent_replay"] is True
    assert replay_res.json()["incident_id"] == seeded["incident_id"]

    incident_id = seeded["incident_id"]

    # 2. Inject forbidden hidden oracle keys into an Evidence record in DB and re-correlate
    maker = get_sessionmaker()
    with maker() as db:
        first_case_id = seeded["correlated_case_ids"][0]
        ev_fact = db.scalar(
            select(Evidence).where(
                Evidence.case_id == first_case_id,
                Evidence.kind == "OBSERVED_FACT",
            )
        )
        assert ev_fact is not None
        tampered_payload = dict(ev_fact.payload_json)
        tampered_payload["_oracle_root_cause"] = "SECRET_SIMULATOR_FAULT_99"
        tampered_payload["hidden_truth"] = "ORACLE_DO_NOT_EXPOSE"
        tampered_payload["ground_truth"] = "ORACLE_GROUND_TRUTH"
        tampered_payload["fault_injection_label"] = "FAULT_W07"
        ev_fact.payload_json = tampered_payload
        db.commit()

    corr_res = client.post("/api/v1/incidents/correlate", headers=headers)
    assert corr_res.status_code == 200

    # 3. Inspect list, detail, and tasks endpoints; verify zero oracle keys appear anywhere
    list_res = client.get("/api/v1/incidents", headers=headers)
    assert list_res.status_code == 200
    assert list_res.json()["total_count"] == 1

    detail_res = client.get(f"/api/v1/incidents/{incident_id}", headers=headers)
    assert detail_res.status_code == 200
    detail = detail_res.json()
    assert len(detail["linked_cases"]) == 12
    assert (
        "Detected 12 potentially linked cases by dropped-sync worker ID and time range"
        in detail["correlation_trace"]
    )

    serialized_all = json.dumps(
        {
            "list": list_res.json(),
            "detail": detail,
        }
    ).lower()
    assert "secret_simulator_fault_99" not in serialized_all
    assert "oracle_do_not_expose" not in serialized_all
    assert "oracle_ground_truth" not in serialized_all
    for forbidden_key in FORBIDDEN_ORACLE_KEYS:
        assert f'"{forbidden_key}"' not in serialized_all


def test_i02_false_merge_prevention_excludes_unrelated_lookalikes_with_same_complaint_text(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """QA I02:
    - The 2 unrelated lookalike Cases have virtually identical Vietnamese complaint text as the
      12 correlated Cases, but different causal OBSERVED_FACT metadata (failed OTP payment /
      different worker & batch). They MUST NOT be merged into the Incident.
    - Creating multiple Cases with 100% identical complaint text (CLAIM) and no shared causal
      OBSERVED_FACT metadata never creates a false-merge Incident."""
    token = active_session["token"]
    headers = {"X-Demo-Session": token}

    seed_res = client.post("/api/v1/incidents/scenarios/seed", headers=headers)
    assert seed_res.status_code == 201
    seeded = seed_res.json()
    incident_id = seeded["incident_id"]
    lookalike_ids = set(seeded["unrelated_lookalike_case_ids"])
    assert len(lookalike_ids) == 2

    detail_res = client.get(f"/api/v1/incidents/{incident_id}", headers=headers)
    assert detail_res.status_code == 200
    detail = detail_res.json()

    linked_case_ids = {item["case_id"] for item in detail["linked_cases"]}
    assert len(linked_case_ids) == 12
    # Neither lookalike Case was merged into the Incident!
    assert linked_case_ids.isdisjoint(lookalike_ids)

    # Both lookalikes are explicitly documented in rejected_lookalikes with FALSE_MERGE_PREVENTED
    rejected_map = {item["case_id"]: item for item in detail["rejected_lookalikes"]}
    for look_id in lookalike_ids:
        assert look_id in rejected_map
        assert "FALSE_MERGE_PREVENTED" in rejected_map[look_id]["rejection_reason"]

    # Create 3 additional Cases in a fresh session with 100% identical customer complaint text
    # (CLAIM only, no shared causal worker/batch OBSERVED_FACT) and verify 0 incidents are formed.
    sess2_res = client.post("/api/v1/demo/sessions")
    assert sess2_res.status_code == 201
    sess2_headers = {"X-Demo-Session": sess2_res.json()["token"]}

    identical_text = (
        "Tôi đã thanh toán 250.000đ qua MoMo nhưng ứng dụng vẫn báo chờ thanh toán, "
        "đề nghị kiểm tra gấp!"
    )
    for _ in range(3):
        c_res = client.post(
            "/api/v1/cases",
            headers=sess2_headers,
            json={"text": identical_text, "type": "PAYMENT_DISPUTE"},
        )
        assert c_res.status_code == 201

    corr2_res = client.post("/api/v1/incidents/correlate", headers=sess2_headers)
    assert corr2_res.status_code == 200
    assert corr2_res.json()["total_count"] == 0


def test_i03_shared_incident_and_single_child_resolution_never_auto_resolves_other_children(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """QA I03:
    - Correlating 12 Cases and running the 3 Taskforce roles NEVER auto-verifies or auto-resolves
      any child Case.
    - Marking the parent Incident MITIGATED/CLOSED NEVER transitions child Cases to RESOLVED.
    - Investigating, approving, executing, and independently verifying child Case #1 transitions
      ONLY child Case #1 to RESOLVED; the remaining 11 sibling Cases stay in their own state."""
    token = active_session["token"]
    headers = {"X-Demo-Session": token}

    seed_res = client.post("/api/v1/incidents/scenarios/seed", headers=headers)
    assert seed_res.status_code == 201
    seeded = seed_res.json()
    incident_id = seeded["incident_id"]
    correlated_ids: list[str] = seeded["correlated_case_ids"]
    assert len(correlated_ids) == 12

    # 1. Immediately after correlation + Taskforce assignment, all 12 child Cases are still NEW
    detail_before = client.get(f"/api/v1/incidents/{incident_id}", headers=headers).json()
    assert detail_before["status"] == IncidentStatus.TASKFORCE_ASSIGNED.value
    for item in detail_before["linked_cases"]:
        assert item["case_state"] == CaseState.NEW.value
        assert item["is_auto_resolved"] is False

    # 2. Even if parent Incident is marked MITIGATED in DB, child Cases are still NEW
    maker = get_sessionmaker()
    with maker() as db:
        inc_obj = db.get(Incident, incident_id)
        assert inc_obj is not None
        inc_obj.status = IncidentStatus.MITIGATED.value
        db.commit()

        verifs_before = list(
            db.scalars(select(Verification).where(Verification.case_id.in_(correlated_ids))).all()
        )
        assert len(verifs_before) == 0

    # 3. Independently investigate, approve, execute, and verify ONLY child Case #1
    child_1_id = correlated_ids[0]
    other_11_ids = correlated_ids[1:]

    inv_res = client.post(f"/api/v1/cases/{child_1_id}/investigate", headers=headers)
    assert inv_res.status_code == 200
    prop_ids = inv_res.json()["proposed_action_ids"]
    assert len(prop_ids) == 1
    prop_1_id = prop_ids[0]

    appr_res = client.post(
        f"/api/v1/actions/{prop_1_id}/simulate-demo-approval",
        headers=headers,
        json={"reason": "Phê duyệt độc lập cho Case con #1 trong Incident"},
    )
    assert appr_res.status_code == 200

    exec_res = client.post(
        f"/api/v1/actions/{prop_1_id}/execute",
        headers={**headers, "X-Idempotency-Key": f"EXEC-INC-CHILD-{child_1_id}"},
    )
    assert exec_res.status_code == 200
    assert exec_res.json()["case_state"] == CaseState.RESOLVED.value
    assert exec_res.json()["verification"]["verdict"] == VerificationVerdict.PASSED.value

    # 4. Verify child Case #1 is RESOLVED while all other 11 sibling Cases in the Incident
    #    remain untouched in CaseState.NEW (0 auto-resolution!)
    detail_after = client.get(f"/api/v1/incidents/{incident_id}", headers=headers).json()
    state_by_case = {ic["case_id"]: ic["case_state"] for ic in detail_after["linked_cases"]}
    assert state_by_case[child_1_id] == CaseState.RESOLVED.value
    for sibling_id in other_11_ids:
        assert state_by_case[sibling_id] == CaseState.NEW.value

    with maker() as db:
        sibling_cases = list(db.scalars(select(Case).where(Case.id.in_(other_11_ids))).all())
        assert all(c.state == CaseState.NEW.value for c in sibling_cases)


def test_i04_capability_registry_strict_read_tool_scopes_and_typed_taskforce_handoffs(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """QA I04:
    - CapabilityRegistry defines FINANCE, OPERATIONS, RISK, SUPPORT with strict read-only tool
      allowlists (`can_execute_writes=False`).
    - ScopedRoleToolExecutor allows in-scope read tools and blocks out-of-scope tools with
      403 FORBIDDEN (`ROLE_SCOPE_VIOLATION`).
    - TaskforceManager persists 3 bounded AgentTasks (FINANCE -> OPERATIONS -> RISK) with
      Pydantic-validated typed JSON handoffs and CaseEvent audit traces.
    - Tenant isolation blocks cross-session access to `/api/v1/incidents/{incident_id}`."""
    token = active_session["token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token}

    # 1. Verify GET /api/v1/incidents/capabilities exposes all 4 logical roles with write=False
    cap_res = client.get("/api/v1/incidents/capabilities", headers=headers)
    assert cap_res.status_code == 200
    caps = {c["role"]: c for c in cap_res.json()}
    assert set(caps.keys()) == {"FINANCE", "OPERATIONS", "RISK", "SUPPORT"}
    for role_spec in caps.values():
        assert role_spec["can_execute_writes"] is False
        assert role_spec["can_propose_actions"] is False

    # 2. Enforce strict per-role tool scopes via ScopedRoleToolExecutor
    maker = get_sessionmaker()
    with maker() as db:
        reg = ReadToolRegistry(db, session_id)

        fin_exec = ScopedRoleToolExecutor(role=TaskforceRole.FINANCE.value, tool_registry=reg)
        ops_exec = ScopedRoleToolExecutor(role=TaskforceRole.OPERATIONS.value, tool_registry=reg)
        risk_exec = ScopedRoleToolExecutor(role=TaskforceRole.RISK.value, tool_registry=reg)
        sup_exec = ScopedRoleToolExecutor(role=TaskforceRole.SUPPORT.value, tool_registry=reg)

        # In-scope read calls succeed
        assert fin_exec.execute_read_tool("get_refund_history", {"order_id": "NONE"}).status in (
            "OK",
            "NOT_FOUND",
        )
        assert ops_exec.execute_read_tool("get_inventory", {"sku": "SKU-NONE"}).status in (
            "OK",
            "NOT_FOUND",
        )
        assert risk_exec.execute_read_tool("get_policy", {"action_type": "ORDER_SYNC"}).status == (
            "OK"
        )
        assert sup_exec.execute_read_tool("get_policy", {"action_type": "ORDER_SYNC"}).status == (
            "OK"
        )

        # Out-of-scope read calls MUST fail with 403 ROLE_SCOPE_VIOLATION
        with pytest.raises(AppError) as exc_fin:
            fin_exec.execute_read_tool("get_inventory", {"sku": "SKU-01"})
        assert exc_fin.value.status_code == 403
        assert "ROLE_SCOPE_VIOLATION" in exc_fin.value.message

        with pytest.raises(AppError) as exc_ops:
            ops_exec.execute_read_tool("get_refund_history", {"payment_id": "PAY-01"})
        assert exc_ops.value.status_code == 403
        assert "ROLE_SCOPE_VIOLATION" in exc_ops.value.message

        with pytest.raises(AppError) as exc_risk:
            risk_exec.execute_read_tool("get_inventory", {"sku": "SKU-01"})
        assert exc_risk.value.status_code == 403
        assert "ROLE_SCOPE_VIOLATION" in exc_risk.value.message

        with pytest.raises(AppError) as exc_sup:
            sup_exec.execute_read_tool("get_payment_record", {"payment_id": "PAY-01"})
        assert exc_sup.value.status_code == 403
        assert "ROLE_SCOPE_VIOLATION" in exc_sup.value.message

    # 3. Verify TaskforceManager AgentTasks & typed JSON handoffs on seeded Incident
    seed_res = client.post("/api/v1/incidents/scenarios/seed", headers=headers)
    assert seed_res.status_code == 201
    incident_id = seed_res.json()["incident_id"]
    first_case_id = seed_res.json()["correlated_case_ids"][0]

    tasks_res = client.get(f"/api/v1/incidents/{incident_id}/tasks", headers=headers)
    assert tasks_res.status_code == 200
    tasks = tasks_res.json()
    assert len(tasks) == 3
    assert [t["role"] for t in tasks] == ["FINANCE", "OPERATIONS", "RISK"]

    for t in tasks:
        assert t["state"] == "COMPLETED"
        spec = CapabilityRegistry.get_spec(t["role"])
        assert set(t["allowed_tools"]) == set(spec.allowed_read_tools)
        # Validate handoff_json against TaskHandoffPayload Pydantic schema
        handoff = TaskHandoffPayload.model_validate(t["handoff_json"])
        assert handoff.incident_id == incident_id
        assert handoff.from_role == t["role"]
        assert handoff.auto_resolved_children is False
        assert handoff.requires_per_case_approval_and_verification is True
        assert len(handoff.checked_case_ids) == 12
        for obs in handoff.tool_observations:
            assert obs.tool_name in spec.allowed_read_tools

    # Verify CaseEvent audit trace (`INCIDENT_CORRELATED` and `INCIDENT_TASKFORCE_HANDOFF`)
    evts_res = client.get(f"/api/v1/cases/{first_case_id}/events", headers=headers)
    assert evts_res.status_code == 200
    evt_types = {e["type"] for e in evts_res.json()}
    assert "INCIDENT_CORRELATED" in evt_types
    assert "INCIDENT_TASKFORCE_HANDOFF" in evt_types

    # 4. Verify cross-session tenant isolation on GET /api/v1/incidents/{incident_id}
    other_sess = client.post("/api/v1/demo/sessions").json()
    other_headers = {"X-Demo-Session": other_sess["token"]}
    cross_detail = client.get(f"/api/v1/incidents/{incident_id}", headers=other_headers)
    assert cross_detail.status_code == 404
    cross_tasks = client.get(f"/api/v1/incidents/{incident_id}/tasks", headers=other_headers)
    assert cross_tasks.status_code == 404
