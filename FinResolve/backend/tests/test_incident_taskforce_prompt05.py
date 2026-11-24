"""Comprehensive PostgreSQL integration tests for Prompt 05 & Prompt 05.1:
Adaptive Incident Taskforce (I01–I04 & Prompt 05.1 Hardening).

Acceptance Matrix (prompts/05_incident_taskforce.md & docs/QA_MATRIX.md):
- I01: 12 cases with common same-event causal OBSERVED_FACT (worker_id, batch_id, error_code,
       time window) are correlated into an Incident with trace:
       'Detected 12 potentially linked cases by dropped-sync worker ID and time range',
       explainable evidence score breakdown, and recursive oracle sanitization across dicts & lists.
- I02: Unrelated lookalike cases with identical/similar customer complaint text (CLAIM), split-event
       causal fields, or timestamps outside the bounded window are never merged into the Incident.
- I03: Shared incident evidence, taskforce completion, incident mitigation, or resolving one
       child Case via Policy/Approval/Verifier NEVER auto-verifies or auto-resolves the other
       child Cases.
- I04: CapabilityRegistry and ScopedRoleToolExecutor enforce strict read-tool scopes for
       FINANCE, OPERATIONS, RISK, and SUPPORT (all writes forbidden; out-of-scope tools raise
       403 ROLE_SCOPE_VIOLATION), and TaskforceManager tracks checked_case_ids vs
       unchecked_case_ids (marked UNKNOWN) honestly.
- Prompt 05.1 Regressions:
  - Mixed payment statuses and in-flight PENDING refunds are derived from actual tool observations
    (never hardcoded positive findings or has_in_flight_refund=False).
  - Conditional role selection (2 roles vs 3 roles based on incident evidence and unknowns).
  - Honest model-mode reporting (never claims LLM_API reasoning when deterministic taskforce rules
    ran without invoking an LLM).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.connectors.read_tools import ReadToolRegistry
from app.core import config as config_module
from app.core.errors import AppError
from app.db.session import get_sessionmaker
from app.incidents.capabilities import CapabilityRegistry, ScopedRoleToolExecutor
from app.incidents.correlator import FORBIDDEN_ORACLE_KEYS, sanitize_no_oracle_leak
from app.incidents.taskforce import TaskHandoffPayload
from app.models.actions import Verification
from app.models.cases import Case, Evidence
from app.models.enums import (
    CaseState,
    EvidenceKind,
    IncidentStatus,
    OrderPaymentStatus,
    OrderStatus,
    PaymentStatus,
    RefundStatus,
    TaskforceRole,
    VerificationVerdict,
)
from app.models.incidents import Incident
from app.models.simulator import SimOrder, SimPayment, SimRefund


def test_i01_correlates_12_cases_by_causal_evidence_with_zero_oracle_leak(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """QA I01 & Prompt 05.1 (Items 5 & 6):
    - Seed 12 correlated Cases + 2 unrelated lookalikes via `POST /api/v1/incidents/scenarios/seed`.
    - Verify the 12 cases sharing same-event `worker_id="SYNC_WORKER_W07"`,
      `batch_id="BATCH_20261122_0900"`, and `error_code="ERR_DROPPED_WEBHOOK_SYNC"` are grouped
      into one Incident with an explainable evidence score breakdown.
    - Verify correlation_trace includes:
      'Detected 12 potentially linked cases by dropped-sync worker ID and time range'.
    - Verify recursive oracle sanitization strips oracle keys inside both nested dicts AND lists."""
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

    # 2. Inject forbidden hidden oracle keys inside nested dicts AND lists in Evidence and test
    #    sanitize_no_oracle_leak + re-correlation
    nested_dirty = {
        "valid_field": "keep_me",
        "_oracle_root_cause": "SECRET_SIMULATOR_FAULT_99",
        "nested_list": [
            {
                "item_id": 1,
                "hidden_truth": "ORACLE_LIST_LEAK_01",
                "ground_truth": "ORACLE_LIST_LEAK_02",
                "deep_list": [{"_oracle_nested_tag": "DEEP_LIST_ORACLE_99", "safe_tag": "OK_TAG"}],
            }
        ],
    }
    sanitized_nested = sanitize_no_oracle_leak(nested_dirty)
    sanitized_dump = json.dumps(sanitized_nested).lower()
    assert "secret_simulator_fault_99" not in sanitized_dump
    assert "oracle_list_leak_01" not in sanitized_dump
    assert "oracle_list_leak_02" not in sanitized_dump
    assert "deep_list_oracle_99" not in sanitized_dump
    assert sanitized_nested["nested_list"][0]["deep_list"][0]["safe_tag"] == "OK_TAG"

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
        tampered_payload["events_list"] = [
            {"step": 1, "hidden_truth": "LIST_ORACLE_SECRET", "_oracle_flag": True}
        ]
        ev_fact.payload_json = tampered_payload
        db.commit()

    corr_res = client.post("/api/v1/incidents/correlate", headers=headers)
    assert corr_res.status_code == 200

    # 3. Inspect list, detail, and tasks endpoints; verify explainable evidence score and 0 leaks
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

    # Verify explainable evidence score breakdown is present and matches incident.confidence
    score_breakdown = detail["shared_factors_json"]["evidence_score_breakdown"]
    assert score_breakdown["scoring_method"] == "EXPLAINABLE_CAUSAL_EVIDENCE_RUBRIC_V1"
    assert detail["confidence"] == score_breakdown["final_score"]
    assert score_breakdown["metrics"]["member_cases_count"] == 12
    assert score_breakdown["metrics"]["all_have_same_event_triad"] is True

    serialized_all = json.dumps(
        {
            "list": list_res.json(),
            "detail": detail,
        }
    ).lower()
    assert "secret_simulator_fault_99" not in serialized_all
    assert "oracle_do_not_expose" not in serialized_all
    assert "oracle_ground_truth" not in serialized_all
    assert "list_oracle_secret" not in serialized_all
    for forbidden_key in FORBIDDEN_ORACLE_KEYS:
        assert f'"{forbidden_key}"' not in serialized_all


def test_i02_false_merge_prevention_excludes_lookalikes_split_events_and_out_of_window_cases(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """QA I02 & Prompt 05.1 (Item 5):
    - The 2 unrelated lookalike Cases have virtually identical Vietnamese complaint text as the
      12 correlated Cases, but different causal OBSERVED_FACT metadata. They MUST NOT be merged.
    - A Case where `worker_id` is in one OBSERVED_FACT event and `batch_id`/`error_code` are in a
      different OBSERVED_FACT event (split-event fields) MUST NOT be merged.
    - A Case sharing the same `(worker_id, batch_id, error_code)` in a single event but occurring
      3 hours outside the 20-minute incident time window MUST NOT be merged into the incident.
    - Pure complaint text similarity never creates an Incident."""
    token = active_session["token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token}

    seed_res = client.post("/api/v1/incidents/scenarios/seed", headers=headers)
    assert seed_res.status_code == 201
    seeded = seed_res.json()
    incident_id = seeded["incident_id"]
    lookalike_ids = set(seeded["unrelated_lookalike_case_ids"])
    assert len(lookalike_ids) == 2

    # Add two more false-merge trap Cases in the same session:
    # Trap A: Split-event causal fields (worker_id on Event 1, batch_id + error_code on Event 2)
    # Trap B: Same causal triad in one event, but timestamp is 3h outside the 09:00..09:05 window
    base_ts = datetime(2026, 11, 22, 9, 0, 0, tzinfo=UTC)
    maker = get_sessionmaker()
    with maker() as db:
        split_case = Case(
            id="CASE-SPLIT-EVENT-TRAP",
            session_id=session_id,
            type="PAYMENT_DISPUTE",
            goal="Kiểm tra chống ghép nhầm khi causal fields nằm ở 2 sự kiện rời rạc",
            state=CaseState.NEW.value,
            version=1,
        )
        out_win_case = Case(
            id="CASE-OUT-OF-WINDOW-TRAP",
            session_id=session_id,
            type="PAYMENT_DISPUTE",
            goal="Kiểm tra chống ghép nhầm khi sự kiện nằm ngoài cửa sổ thời gian 20 phút",
            state=CaseState.NEW.value,
            version=1,
        )
        db.add_all([split_case, out_win_case])
        db.flush()

        db.add_all(
            [
                Evidence(
                    id="EVD-SPLIT-CLM",
                    case_id=split_case.id,
                    session_id=session_id,
                    kind=EvidenceKind.CLAIM.value,
                    source="CUSTOMER_MESSAGE",
                    source_ref="intake_text",
                    payload_json={
                        "text": "Tôi đã thanh toán 250.000đ qua MoMo nhưng vẫn báo chờ thanh toán!"
                    },
                    observed_at=base_ts,
                ),
                # Event 1 only has worker_id (no batch_id or error_code)
                Evidence(
                    id="EVD-SPLIT-FCT-1",
                    case_id=split_case.id,
                    session_id=session_id,
                    kind=EvidenceKind.OBSERVED_FACT.value,
                    source="ORDER_EVENT_LOG_DB",
                    source_ref="sim_orders:ORD-SPLIT-1",
                    payload_json={
                        "worker_id": "SYNC_WORKER_W07",
                        "event_timestamp": base_ts.isoformat(),
                    },
                    observed_at=base_ts,
                ),
                # Event 2 only has batch_id and error_code (from another worker/context)
                Evidence(
                    id="EVD-SPLIT-FCT-2",
                    case_id=split_case.id,
                    session_id=session_id,
                    kind=EvidenceKind.OBSERVED_FACT.value,
                    source="ORDER_EVENT_LOG_DB",
                    source_ref="sim_orders:ORD-SPLIT-1",
                    payload_json={
                        "batch_id": "BATCH_20261122_0900",
                        "error_code": "ERR_DROPPED_WEBHOOK_SYNC",
                        "event_timestamp": base_ts.isoformat(),
                    },
                    observed_at=base_ts,
                ),
                Evidence(
                    id="EVD-OUTWIN-CLM",
                    case_id=out_win_case.id,
                    session_id=session_id,
                    kind=EvidenceKind.CLAIM.value,
                    source="CUSTOMER_MESSAGE",
                    source_ref="intake_text",
                    payload_json={
                        "text": "Tôi đã thanh toán 250.000đ qua MoMo nhưng vẫn báo chờ thanh toán!"
                    },
                    observed_at=base_ts + timedelta(hours=3),
                ),
                Evidence(
                    id="EVD-OUTWIN-FCT",
                    case_id=out_win_case.id,
                    session_id=session_id,
                    kind=EvidenceKind.OBSERVED_FACT.value,
                    source="ORDER_EVENT_LOG_DB",
                    source_ref="sim_orders:ORD-OUTWIN-1",
                    payload_json={
                        "worker_id": "SYNC_WORKER_W07",
                        "batch_id": "BATCH_20261122_0900",
                        "error_code": "ERR_DROPPED_WEBHOOK_SYNC",
                        "event_timestamp": (base_ts + timedelta(hours=3)).isoformat(),
                    },
                    observed_at=base_ts + timedelta(hours=3),
                ),
            ]
        )
        db.commit()

    corr_res = client.post("/api/v1/incidents/correlate", headers=headers)
    assert corr_res.status_code == 200

    detail_res = client.get(f"/api/v1/incidents/{incident_id}", headers=headers)
    assert detail_res.status_code == 200
    detail = detail_res.json()

    linked_case_ids = {item["case_id"] for item in detail["linked_cases"]}
    assert len(linked_case_ids) == 12
    assert linked_case_ids.isdisjoint(lookalike_ids)
    assert "CASE-SPLIT-EVENT-TRAP" not in linked_case_ids
    assert "CASE-OUT-OF-WINDOW-TRAP" not in linked_case_ids

    rejected_map = {item["case_id"]: item for item in detail["rejected_lookalikes"]}
    for trap_id in (*lookalike_ids, "CASE-SPLIT-EVENT-TRAP", "CASE-OUT-OF-WINDOW-TRAP"):
        assert trap_id in rejected_map
        assert "FALSE_MERGE_PREVENTED" in rejected_map[trap_id]["rejection_reason"]


def test_i03_shared_incident_and_single_child_resolution_never_auto_resolves_other_children(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """QA I03:
    - Correlating 12 Cases and running the Taskforce roles NEVER auto-verifies or auto-resolves
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

    detail_before = client.get(f"/api/v1/incidents/{incident_id}", headers=headers).json()
    assert detail_before["status"] == IncidentStatus.TASKFORCE_ASSIGNED.value
    for item in detail_before["linked_cases"]:
        assert item["case_state"] == CaseState.NEW.value
        assert item["is_auto_resolved"] is False

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

    child_1_id = correlated_ids[0]
    other_11_ids = correlated_ids[1:]

    inv_res = client.post(f"/api/v1/cases/{child_1_id}/investigate", headers=headers)
    assert inv_res.status_code == 200
    prop_1_id = inv_res.json()["proposed_action_ids"][0]

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

    detail_after = client.get(f"/api/v1/incidents/{incident_id}", headers=headers).json()
    state_by_case = {ic["case_id"]: ic["case_state"] for ic in detail_after["linked_cases"]}
    assert state_by_case[child_1_id] == CaseState.RESOLVED.value
    for sibling_id in other_11_ids:
        assert state_by_case[sibling_id] == CaseState.NEW.value

    with maker() as db:
        sibling_cases = list(db.scalars(select(Case).where(Case.id.in_(other_11_ids))).all())
        assert all(c.state == CaseState.NEW.value for c in sibling_cases)


def test_i04_capability_registry_strict_scopes_and_accurate_checked_vs_unchecked_unknown_tracking(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """QA I04 & Prompt 05.1 (Item 2):
    - CapabilityRegistry enforces strict per-role read-tool allowlists (`can_execute_writes=False`).
    - Out-of-scope tools raise 403 FORBIDDEN (`ROLE_SCOPE_VIOLATION`).
    - TaskHandoffPayload tracks `checked_case_ids` (only the directly tool-queried sample Case(s))
      and `unchecked_case_ids` (the remaining uninspected Cases), marking every unchecked Case
      as `UNKNOWN` in `per_case_verification_status` instead of claiming all 12 Cases were verified.
    """
    token = active_session["token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token}

    cap_res = client.get("/api/v1/incidents/capabilities", headers=headers)
    assert cap_res.status_code == 200
    caps = {c["role"]: c for c in cap_res.json()}
    assert set(caps.keys()) == {"FINANCE", "OPERATIONS", "RISK", "SUPPORT"}
    for role_spec in caps.values():
        assert role_spec["can_execute_writes"] is False
        assert role_spec["can_propose_actions"] is False

    maker = get_sessionmaker()
    with maker() as db:
        reg = ReadToolRegistry(db, session_id)
        fin_exec = ScopedRoleToolExecutor(role=TaskforceRole.FINANCE.value, tool_registry=reg)
        ops_exec = ScopedRoleToolExecutor(role=TaskforceRole.OPERATIONS.value, tool_registry=reg)
        risk_exec = ScopedRoleToolExecutor(role=TaskforceRole.RISK.value, tool_registry=reg)
        sup_exec = ScopedRoleToolExecutor(role=TaskforceRole.SUPPORT.value, tool_registry=reg)

        for exec_role, forbidden_tool in (
            (fin_exec, "get_inventory"),
            (ops_exec, "get_refund_history"),
            (risk_exec, "get_inventory"),
            (sup_exec, "get_payment_record"),
        ):
            with pytest.raises(AppError) as exc_info:
                exec_role.execute_read_tool(forbidden_tool, {"order_id": "ORD-01"})
            assert exc_info.value.status_code == 403
            assert "ROLE_SCOPE_VIOLATION" in exc_info.value.message

    seed_res = client.post("/api/v1/incidents/scenarios/seed", headers=headers)
    assert seed_res.status_code == 201
    incident_id = seed_res.json()["incident_id"]
    all_12_ids = set(seed_res.json()["correlated_case_ids"])

    tasks_res = client.get(f"/api/v1/incidents/{incident_id}/tasks", headers=headers)
    assert tasks_res.status_code == 200
    tasks = tasks_res.json()
    assert len(tasks) == 3
    assert [t["role"] for t in tasks] == ["FINANCE", "OPERATIONS", "RISK"]

    for t in tasks:
        spec = CapabilityRegistry.get_spec(t["role"])
        handoff = TaskHandoffPayload.model_validate(t["handoff_json"])
        # Only sample Case(s) were directly inspected via read tools (1 checked, 11 unchecked)
        assert len(handoff.checked_case_ids) == 1
        assert len(handoff.unchecked_case_ids) == 11
        assert set(handoff.checked_case_ids) | set(handoff.unchecked_case_ids) == all_12_ids
        assert set(handoff.checked_case_ids).isdisjoint(set(handoff.unchecked_case_ids))

        # Every unchecked Case MUST be explicitly marked UNKNOWN
        for unchecked_id in handoff.unchecked_case_ids:
            assert handoff.per_case_verification_status[unchecked_id] == "UNKNOWN"
        for obs in handoff.tool_observations:
            assert obs.tool_name in spec.allowed_read_tools

    # Cross-session tenant isolation check
    other_sess = client.post("/api/v1/demo/sessions").json()
    other_headers = {"X-Demo-Session": other_sess["token"]}
    assert client.get(f"/api/v1/incidents/{incident_id}", headers=other_headers).status_code == 404


def test_prompt05_1_pending_refund_and_mixed_payment_statuses_derived_from_tool_observations(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Prompt 05.1 (Items 1, 2, 5 & 8):
    - When an incident contains mixed payment statuses (e.g. one Case has a FAILED payment) and
      an in-flight PENDING refund on one of the payments:
      1. `FINANCE` actually queries the non-SUCCESS payment, reports the mixed/failed payment
         status, and recommends `HOLD_OR_REVIEW_MIXED_PAYMENT_STATUS_PER_CASE`.
      2. `RISK` actually queries `get_refund_history`, detects `has_in_flight_refund=True`
         (`in_flight_pending_or_unknown_amount=120000`, `in_flight_statuses=['PENDING']`), and
         recommends `HOLD_ACTION_RECONCILE_IN_FLIGHT_OR_SETTLED_REFUND_PER_CASE`.
      3. Explainable evidence score penalizes mixed payment statuses."""
    token = active_session["token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token}
    base_ts = datetime(2026, 11, 22, 10, 0, 0, tzinfo=UTC)

    maker = get_sessionmaker()
    with maker() as db:
        # Case 1: Paid order with an in-flight PENDING refund
        o1 = SimOrder(
            id="ORD-MIX-01",
            session_id=session_id,
            customer_name="Khách Mix 1",
            customer_phone="0911000111",
            amount=300000,
            currency="VND",
            subtotal=300000,
            grand_total=300000,
            status=OrderStatus.PENDING.value,
            payment_status=OrderPaymentStatus.UNPAID.value,
        )
        p1 = SimPayment(
            id="PAY-MIX-01",
            session_id=session_id,
            order_id=o1.id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref="MOMO_MIX_01",
            result_code=0,
            amount=300000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=base_ts,
        )
        r1_pending = SimRefund(
            id="REF-MIX-PEND-01",
            session_id=session_id,
            order_id=o1.id,
            payment_id=p1.id,
            amount=120000,
            requested_amount=120000,
            approved_amount=120000,
            completed_amount=0,
            currency="VND",
            request_id="REQ-MIX-PEND-01",
            status=RefundStatus.PENDING.value,
            reason="Lệnh hoàn tiền đang chờ xử lý",
        )
        c1 = Case(
            id="CASE-MIX-01",
            session_id=session_id,
            type="PAYMENT_DISPUTE",
            goal="Kiểm tra đơn ORD-MIX-01 có lệnh hoàn PENDING",
            state=CaseState.NEW.value,
            version=1,
        )

        # Case 2: Correlated by same worker/batch/error event, but payment actually FAILED!
        o2 = SimOrder(
            id="ORD-MIX-02",
            session_id=session_id,
            customer_name="Khách Mix 2",
            customer_phone="0911000222",
            amount=300000,
            currency="VND",
            subtotal=300000,
            grand_total=300000,
            status=OrderStatus.PENDING.value,
            payment_status=OrderPaymentStatus.UNPAID.value,
        )
        p2_failed = SimPayment(
            id="PAY-MIX-02",
            session_id=session_id,
            order_id=o2.id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref="MOMO_MIX_02",
            result_code=1006,
            amount=300000,
            currency="VND",
            status=PaymentStatus.FAILED.value,
            verified_at=None,
        )
        c2 = Case(
            id="CASE-MIX-02",
            session_id=session_id,
            type="PAYMENT_DISPUTE",
            goal="Kiểm tra đơn ORD-MIX-02 có thanh toán FAILED",
            state=CaseState.NEW.value,
            version=1,
        )
        db.add_all([o1, p1, r1_pending, c1, o2, p2_failed, c2])
        db.flush()

        db.add_all(
            [
                Evidence(
                    id="EVD-MIX-FCT-01",
                    case_id=c1.id,
                    session_id=session_id,
                    kind=EvidenceKind.OBSERVED_FACT.value,
                    source="ORDER_EVENT_LOG_DB",
                    source_ref=f"sim_orders:{o1.id}",
                    payload_json={
                        "order_id": o1.id,
                        "payment_id": p1.id,
                        "payment_status": "SUCCESS",
                        "worker_id": "SYNC_WORKER_W99",
                        "batch_id": "BATCH_MIX_1000",
                        "error_code": "ERR_DROPPED_WEBHOOK_SYNC",
                        "event_timestamp": base_ts.isoformat(),
                    },
                    observed_at=base_ts,
                ),
                Evidence(
                    id="EVD-MIX-FCT-02",
                    case_id=c2.id,
                    session_id=session_id,
                    kind=EvidenceKind.OBSERVED_FACT.value,
                    source="ORDER_EVENT_LOG_DB",
                    source_ref=f"sim_orders:{o2.id}",
                    payload_json={
                        "order_id": o2.id,
                        "payment_id": p2_failed.id,
                        "payment_status": "FAILED",
                        "worker_id": "SYNC_WORKER_W99",
                        "batch_id": "BATCH_MIX_1000",
                        "error_code": "ERR_DROPPED_WEBHOOK_SYNC",
                        "event_timestamp": (base_ts + timedelta(seconds=30)).isoformat(),
                    },
                    observed_at=base_ts + timedelta(seconds=30),
                ),
            ]
        )
        db.commit()

    corr_res = client.post("/api/v1/incidents/correlate", headers=headers)
    assert corr_res.status_code == 200
    inc_id = corr_res.json()["items"][0]["id"]

    detail = client.get(f"/api/v1/incidents/{inc_id}", headers=headers).json()
    breakdown = detail["shared_factors_json"]["evidence_score_breakdown"]
    assert breakdown["components"]["payment_status_consistency_score"] < 0
    assert set(breakdown["metrics"]["observed_payment_statuses"]) == {"FAILED", "SUCCESS"}

    tasks_by_role = {t["role"]: t for t in detail["assigned_tasks"]}
    assert set(tasks_by_role.keys()) == {"FINANCE", "OPERATIONS", "RISK"}

    # FINANCE must detect the FAILED payment and NOT claim all payments succeeded
    fin_handoff = TaskHandoffPayload.model_validate(tasks_by_role["FINANCE"]["handoff_json"])
    assert fin_handoff.recommended_per_case_action == (
        "HOLD_OR_REVIEW_MIXED_PAYMENT_STATUS_PER_CASE"
    )
    assert "CASE-MIX-02" in fin_handoff.findings_vi
    assert fin_handoff.per_case_verification_status["CASE-MIX-02"] == "OBSERVED_PAYMENT_FAILED"

    # RISK must detect the in-flight PENDING refund via get_refund_history
    risk_handoff = TaskHandoffPayload.model_validate(tasks_by_role["RISK"]["handoff_json"])
    assert risk_handoff.recommended_per_case_action == (
        "HOLD_ACTION_RECONCILE_IN_FLIGHT_OR_SETTLED_REFUND_PER_CASE"
    )
    ref_obs = next(
        obs
        for obs in risk_handoff.tool_observations
        if obs.tool_name == "get_refund_history" and obs.case_id == "CASE-MIX-01"
    )
    assert ref_obs.summary["has_in_flight_refund"] is True
    assert ref_obs.summary["in_flight_pending_or_unknown_amount"] == 120000
    assert ref_obs.summary["in_flight_statuses"] == ["PENDING"]


def test_prompt05_1_conditional_role_selection_and_honest_model_mode_reporting(
    client: TestClient,
    active_session: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prompt 05.1 (Items 3, 4 & 8):
    - A small 2-case order-sync incident with uniform SUCCESS payments and 0 refunds conditionally
      selects 2 bounded roles: `['FINANCE', 'OPERATIONS']` (no unnecessary RISK task).
    - Even if environment settings configure `MODEL_PROVIDER='openai'`, `MODEL_NAME='gpt-4o-mini'`,
      and `MODEL_API_KEY='sk-configured'` (`configured_model_mode == 'LLM_API'`), Incident APIs
      and Taskforce handoffs honestly report `model_mode == 'DEMO_RULES'`,
      `runtime_mode == 'DEMO_RULES'`, `taskforce_execution_mode == 'DETERMINISTIC_TASKFORCE_RULES'`,
      and `llm_invoked is False` because the taskforce did not invoke an LLM."""
    token = active_session["token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token}
    base_ts = datetime(2026, 11, 22, 14, 0, 0, tzinfo=UTC)

    maker = get_sessionmaker()
    with maker() as db:
        for idx in (1, 2):
            ord_id = f"ORD-SMALL-0{idx}"
            pay_id = f"PAY-SMALL-0{idx}"
            case_id = f"CASE-SMALL-0{idx}"
            db.add_all(
                [
                    SimOrder(
                        id=ord_id,
                        session_id=session_id,
                        customer_name=f"Khách Small {idx}",
                        customer_phone=f"092200011{idx}",
                        amount=180000,
                        currency="VND",
                        subtotal=180000,
                        grand_total=180000,
                        status=OrderStatus.PENDING.value,
                        payment_status=OrderPaymentStatus.UNPAID.value,
                    ),
                    SimPayment(
                        id=pay_id,
                        session_id=session_id,
                        order_id=ord_id,
                        merchant_id="MERCHANT_MOCK_01",
                        provider="MOCK_MOMO",
                        provider_ref=f"MOMO_SMALL_0{idx}",
                        result_code=0,
                        amount=180000,
                        currency="VND",
                        status=PaymentStatus.SUCCESS.value,
                        verified_at=base_ts,
                    ),
                    Case(
                        id=case_id,
                        session_id=session_id,
                        type="ORDER_SYNC",
                        goal=f"Đồng bộ đơn {ord_id}",
                        state=CaseState.NEW.value,
                        version=1,
                    ),
                ]
            )
            db.flush()
            db.add(
                Evidence(
                    id=f"EVD-SMALL-FCT-0{idx}",
                    case_id=case_id,
                    session_id=session_id,
                    kind=EvidenceKind.OBSERVED_FACT.value,
                    source="ORDER_EVENT_LOG_DB",
                    source_ref=f"sim_orders:{ord_id}",
                    payload_json={
                        "order_id": ord_id,
                        "payment_id": pay_id,
                        "payment_status": "SUCCESS",
                        "worker_id": "SYNC_WORKER_W12",
                        "batch_id": "BATCH_SMALL_1400",
                        "error_code": "ERR_DROPPED_WEBHOOK_SYNC",
                        "event_timestamp": (base_ts + timedelta(seconds=idx * 10)).isoformat(),
                    },
                    observed_at=base_ts + timedelta(seconds=idx * 10),
                )
            )
        db.commit()

    # Simulate LLM_API configured in Settings while Taskforce runs deterministically
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("MODEL_NAME", "gpt-4o-mini")
    monkeypatch.setenv("MODEL_API_KEY", "sk-simulated-configured-key")
    config_module.get_settings.cache_clear()
    try:
        corr_res = client.post("/api/v1/incidents/correlate", headers=headers)
        assert corr_res.status_code == 200
        corr_body = corr_res.json()

        # Honest model-mode reporting: configured_model_mode is LLM_API, but effective
        # model_mode for Taskforce is DEMO_RULES and llm_invoked is False!
        assert corr_body["configured_model_mode"] == "LLM_API"
        assert corr_body["model_mode"] == "DEMO_RULES"
        assert corr_body["taskforce_execution_mode"] == "DETERMINISTIC_TASKFORCE_RULES"
        assert corr_body["llm_invoked"] is False

        inc_id = corr_body["items"][0]["id"]
        detail = client.get(f"/api/v1/incidents/{inc_id}", headers=headers).json()
        assert detail["configured_model_mode"] == "LLM_API"
        assert detail["model_mode"] == "DEMO_RULES"
        assert detail["llm_invoked"] is False

        # Conditional role selection: small 2-case clean sync incident selects 2 roles
        assigned_roles = [t["role"] for t in detail["assigned_tasks"]]
        assert assigned_roles == ["FINANCE", "OPERATIONS"]

        for t in detail["assigned_tasks"]:
            handoff = TaskHandoffPayload.model_validate(t["handoff_json"])
            assert handoff.runtime_mode == "DEMO_RULES"
            assert handoff.configured_model_mode == "LLM_API"
            assert handoff.taskforce_execution_mode == "DETERMINISTIC_TASKFORCE_RULES"
            assert handoff.llm_invoked is False
    finally:
        config_module.get_settings.cache_clear()


def test_prompt05_1_reconcile_incident_case_memberships_when_case_changes_causal_event(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Prompt 05.1 Final (Items 1 & 4):
    - When a Case changes its causal OBSERVED_FACT event (by appending a newer corrective
      OBSERVED_FACT) and re-correlation runs:
      1. Stale `IncidentCase` link is removed from the old Incident.
      2. An append-only `CaseEvent(type='INCIDENT_UNLINKED')` audit trail is preserved and
         `removed_stale_case_ids` is recorded on the Incident.
      3. When an Incident drops below 2 valid Cases, its remaining stale links are removed,
         the Incident transitions to `CLOSED`, and child Cases are NEVER auto-resolved."""
    token = active_session["token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token}
    base_ts = datetime(2026, 11, 22, 15, 0, 0, tzinfo=UTC)

    maker = get_sessionmaker()
    with maker() as db:
        for idx in (1, 2, 3):
            ord_id = f"ORD-RECON-0{idx}"
            pay_id = f"PAY-RECON-0{idx}"
            case_id = f"CASE-RECON-0{idx}"
            db.add_all(
                [
                    SimOrder(
                        id=ord_id,
                        session_id=session_id,
                        customer_name=f"Khách Recon {idx}",
                        customer_phone=f"093300011{idx}",
                        amount=200000,
                        currency="VND",
                        subtotal=200000,
                        grand_total=200000,
                        status=OrderStatus.PENDING.value,
                        payment_status=OrderPaymentStatus.UNPAID.value,
                    ),
                    SimPayment(
                        id=pay_id,
                        session_id=session_id,
                        order_id=ord_id,
                        merchant_id="MERCHANT_MOCK_01",
                        provider="MOCK_MOMO",
                        provider_ref=f"MOMO_RECON_0{idx}",
                        result_code=0,
                        amount=200000,
                        currency="VND",
                        status=PaymentStatus.SUCCESS.value,
                        verified_at=base_ts,
                    ),
                    Case(
                        id=case_id,
                        session_id=session_id,
                        type="ORDER_SYNC",
                        goal=f"Đồng bộ đơn {ord_id}",
                        state=CaseState.NEW.value,
                        version=1,
                    ),
                ]
            )
            db.flush()
            db.add(
                Evidence(
                    id=f"EVD-RECON-FCT-0{idx}",
                    case_id=case_id,
                    session_id=session_id,
                    kind=EvidenceKind.OBSERVED_FACT.value,
                    source="ORDER_EVENT_LOG_DB",
                    source_ref=f"sim_orders:{ord_id}",
                    payload_json={
                        "order_id": ord_id,
                        "payment_id": pay_id,
                        "payment_status": "SUCCESS",
                        "worker_id": "SYNC_WORKER_W55",
                        "batch_id": "BATCH_RECON_1500",
                        "error_code": "ERR_DROPPED_WEBHOOK_SYNC",
                        "event_timestamp": (base_ts + timedelta(seconds=idx * 15)).isoformat(),
                    },
                    observed_at=base_ts + timedelta(seconds=idx * 15),
                )
            )
        db.commit()

    # Initial correlation links all 3 Cases into 1 Incident
    res_1 = client.post("/api/v1/incidents/correlate", headers=headers)
    assert res_1.status_code == 200
    inc_id = res_1.json()["items"][0]["id"]
    detail_1 = client.get(f"/api/v1/incidents/{inc_id}", headers=headers).json()
    assert {ic["case_id"] for ic in detail_1["linked_cases"]} == {
        "CASE-RECON-01",
        "CASE-RECON-02",
        "CASE-RECON-03",
    }

    # Append a newer corrective OBSERVED_FACT on CASE-RECON-03 with a different causal event
    with maker() as db:
        db.add(
            Evidence(
                id="EVD-RECON-FCT-03-UPDATED",
                case_id="CASE-RECON-03",
                session_id=session_id,
                kind=EvidenceKind.OBSERVED_FACT.value,
                source="ORDER_EVENT_LOG_DB",
                source_ref="sim_orders:ORD-RECON-03",
                payload_json={
                    "order_id": "ORD-RECON-03",
                    "payment_id": "PAY-RECON-03",
                    "payment_status": "FAILED",
                    "worker_id": "CHECKOUT_WORKER_W88",
                    "batch_id": "BATCH_OTHER_9999",
                    "error_code": "ERR_USER_OTP_EXPIRED",
                    "event_timestamp": (base_ts + timedelta(minutes=5)).isoformat(),
                },
                observed_at=base_ts + timedelta(minutes=5),
            )
        )
        db.commit()

    # Re-correlate: CASE-RECON-03 stale link MUST be removed and audited
    res_2 = client.post("/api/v1/incidents/correlate", headers=headers)
    assert res_2.status_code == 200
    detail_2 = client.get(f"/api/v1/incidents/{inc_id}", headers=headers).json()
    assert {ic["case_id"] for ic in detail_2["linked_cases"]} == {
        "CASE-RECON-01",
        "CASE-RECON-02",
    }
    assert "CASE-RECON-03" in detail_2["shared_factors_json"]["removed_stale_case_ids"]

    c3_evts = client.get("/api/v1/cases/CASE-RECON-03/events", headers=headers).json()
    assert any(e["type"] == "INCIDENT_UNLINKED" for e in c3_evts)

    # Now update CASE-RECON-02 so the cluster drops below 2 valid Cases -> Incident closes &
    # removes remaining stale links without auto-resolving any child Case
    with maker() as db:
        db.add(
            Evidence(
                id="EVD-RECON-FCT-02-UPDATED",
                case_id="CASE-RECON-02",
                session_id=session_id,
                kind=EvidenceKind.OBSERVED_FACT.value,
                source="ORDER_EVENT_LOG_DB",
                source_ref="sim_orders:ORD-RECON-02",
                payload_json={
                    "order_id": "ORD-RECON-02",
                    "payment_id": "PAY-RECON-02",
                    "payment_status": "FAILED",
                    "worker_id": "CHECKOUT_WORKER_W89",
                    "batch_id": "BATCH_OTHER_8888",
                    "error_code": "ERR_CARD_DECLINED",
                    "event_timestamp": (base_ts + timedelta(minutes=6)).isoformat(),
                },
                observed_at=base_ts + timedelta(minutes=6),
            )
        )
        db.commit()

    res_3 = client.post("/api/v1/incidents/correlate", headers=headers)
    assert res_3.status_code == 200
    detail_3 = client.get(f"/api/v1/incidents/{inc_id}", headers=headers).json()
    assert detail_3["status"] == IncidentStatus.CLOSED.value
    assert len(detail_3["linked_cases"]) == 0
    assert {"CASE-RECON-01", "CASE-RECON-02", "CASE-RECON-03"}.issubset(
        set(detail_3["shared_factors_json"]["removed_stale_case_ids"])
    )

    with maker() as db:
        all_3 = list(
            db.scalars(
                select(Case).where(Case.id.in_(["CASE-RECON-01", "CASE-RECON-02", "CASE-RECON-03"]))
            ).all()
        )
        assert all(c.state == CaseState.NEW.value for c in all_3)


def test_prompt05_1_risk_distinguishes_empty_refund_history_from_missing_payment_or_order(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Prompt 05.1 Final (Items 2 & 4):
    - `get_refund_history` distinguishes a verified payment with 0 refunds (`status='OK'`,
      `payment_found=True`, `authority_status='VERIFIED_EMPTY_REFUND_HISTORY'`) from a missing
      payment/order (`status='NOT_FOUND'`, `payment_found=False`, `outcome='UNKNOWN'`).
    - When payment/order is missing, `RISK` marks that Case as `UNKNOWN` (`has_in_flight_refund`
      is `None`, not `False`) and recommends `HOLD_OR_REVIEW_UNKNOWN_REFUND_AUTHORITY_PER_CASE`."""
    token = active_session["token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token}
    base_ts = datetime(2026, 11, 22, 16, 0, 0, tzinfo=UTC)

    maker = get_sessionmaker()
    with maker() as db:
        # Case 1: Real verified payment with 0 refunds
        o1 = SimOrder(
            id="ORD-REF-AUTH-01",
            session_id=session_id,
            customer_name="Khách Auth 1",
            customer_phone="0944000111",
            amount=220000,
            currency="VND",
            subtotal=220000,
            grand_total=220000,
            status=OrderStatus.PENDING.value,
            payment_status=OrderPaymentStatus.UNPAID.value,
        )
        p1 = SimPayment(
            id="PAY-REF-AUTH-01",
            session_id=session_id,
            order_id=o1.id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref="MOMO_AUTH_01",
            result_code=0,
            amount=220000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=base_ts,
        )
        c1 = Case(
            id="CASE-REF-AUTH-01",
            session_id=session_id,
            type="REFUND_REQUEST",
            goal="Kiểm tra lịch sử hoàn tiền có thanh toán xác thực",
            state=CaseState.NEW.value,
            version=1,
        )
        # Case 2: References missing payment_id / order_id (not in DB!)
        c2 = Case(
            id="CASE-REF-AUTH-02-MISSING",
            session_id=session_id,
            type="REFUND_REQUEST",
            goal="Kiểm tra lịch sử hoàn tiền khi thiếu bản ghi thanh toán",
            state=CaseState.NEW.value,
            version=1,
        )
        db.add_all([o1, p1, c1, c2])
        db.flush()
        db.add_all(
            [
                Evidence(
                    id="EVD-AUTH-FCT-01",
                    case_id=c1.id,
                    session_id=session_id,
                    kind=EvidenceKind.OBSERVED_FACT.value,
                    source="ORDER_EVENT_LOG_DB",
                    source_ref=f"sim_orders:{o1.id}",
                    payload_json={
                        "order_id": o1.id,
                        "payment_id": p1.id,
                        "payment_status": "SUCCESS",
                        "worker_id": "SYNC_WORKER_W77",
                        "batch_id": "BATCH_AUTH_1600",
                        "error_code": "ERR_DROPPED_WEBHOOK_SYNC",
                        "event_timestamp": base_ts.isoformat(),
                    },
                    observed_at=base_ts,
                ),
                Evidence(
                    id="EVD-AUTH-FCT-02",
                    case_id=c2.id,
                    session_id=session_id,
                    kind=EvidenceKind.OBSERVED_FACT.value,
                    source="ORDER_EVENT_LOG_DB",
                    source_ref="sim_orders:ORD-MISSING-999",
                    payload_json={
                        "order_id": "ORD-MISSING-999",
                        "payment_id": "PAY-MISSING-999",
                        "payment_status": "UNKNOWN",
                        "worker_id": "SYNC_WORKER_W77",
                        "batch_id": "BATCH_AUTH_1600",
                        "error_code": "ERR_DROPPED_WEBHOOK_SYNC",
                        "event_timestamp": (base_ts + timedelta(seconds=20)).isoformat(),
                    },
                    observed_at=base_ts + timedelta(seconds=20),
                ),
            ]
        )
        db.commit()

    corr_res = client.post("/api/v1/incidents/correlate", headers=headers)
    assert corr_res.status_code == 200
    inc_id = corr_res.json()["items"][0]["id"]
    detail = client.get(f"/api/v1/incidents/{inc_id}", headers=headers).json()
    risk_task = next(t for t in detail["assigned_tasks"] if t["role"] == "RISK")
    risk_handoff = TaskHandoffPayload.model_validate(risk_task["handoff_json"])

    # Case 1 has verified empty refund history
    assert (
        risk_handoff.per_case_verification_status["CASE-REF-AUTH-01"]
        == "OBSERVED_NO_REFUND_ON_VERIFIED_PAYMENT"
    )
    obs_c1 = next(
        o
        for o in risk_handoff.tool_observations
        if o.tool_name == "get_refund_history" and o.case_id == "CASE-REF-AUTH-01"
    )
    assert obs_c1.status == "OK"
    assert obs_c1.summary["payment_found"] is True
    assert obs_c1.summary["has_in_flight_refund"] is False
    assert obs_c1.summary["refund_authority_status"] == "VERIFIED_EMPTY_REFUND_HISTORY"

    # Case 2 has missing payment/order -> UNKNOWN (NOT false positive "no refund"!)
    assert risk_handoff.per_case_verification_status["CASE-REF-AUTH-02-MISSING"] == "UNKNOWN"
    obs_c2 = next(
        o
        for o in risk_handoff.tool_observations
        if o.tool_name == "get_refund_history" and o.case_id == "CASE-REF-AUTH-02-MISSING"
    )
    assert obs_c2.status == "NOT_FOUND"
    assert obs_c2.summary["payment_found"] is False
    assert obs_c2.summary["has_in_flight_refund"] is None
    assert obs_c2.summary["refund_authority_status"] == "UNKNOWN_PAYMENT_OR_ORDER_NOT_FOUND"
    assert risk_handoff.recommended_per_case_action == (
        "HOLD_OR_REVIEW_UNKNOWN_REFUND_AUTHORITY_PER_CASE"
    )


def test_prompt05_1_checked_case_ids_computed_from_actual_tool_invocations_only(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Prompt 05.1 Final (Items 3 & 4):
    - When a Case is selected in the planned sample (`sampled_ics`) but has NO `order_id` and
      NO `payment_id` (so no case-scoped read tool can be invoked for it):
      - `checked_case_ids` MUST NOT include that Case for any role.
      - `unchecked_case_ids` MUST include that Case, and `per_case_verification_status` MUST mark
        it `UNKNOWN` (even if a role invoked a generic `get_policy` call)."""
    token = active_session["token"]
    session_id = active_session["id"]
    headers = {"X-Demo-Session": token}
    base_ts = datetime(2026, 11, 22, 17, 0, 0, tzinfo=UTC)

    maker = get_sessionmaker()
    with maker() as db:
        # Case 1: No order_id, no payment_id -> no executable case-scoped read tool!
        c1 = Case(
            id="CASE-NO-TOOL-01",
            session_id=session_id,
            type="PAYMENT_DISPUTE",
            goal="Sự cố thiếu hoàn toàn mã đơn và mã thanh toán",
            state=CaseState.NEW.value,
            version=1,
        )
        # Case 2: Has order_id (no payment_id) -> OPERATIONS & SUPPORT can call get_order,
        # FINANCE can call find_payment_candidates.
        o2 = SimOrder(
            id="ORD-TOOL-CHECK-02",
            session_id=session_id,
            customer_name="Khách Tool Check 2",
            customer_phone="0955000222",
            amount=150000,
            currency="VND",
            subtotal=150000,
            grand_total=150000,
            status=OrderStatus.PENDING.value,
            payment_status=OrderPaymentStatus.UNPAID.value,
        )
        c2 = Case(
            id="CASE-HAS-ORD-02",
            session_id=session_id,
            type="PAYMENT_DISPUTE",
            goal="Sự cố có order_id để chạy tool đọc",
            state=CaseState.NEW.value,
            version=1,
        )
        db.add_all([c1, o2, c2])
        db.flush()
        db.add_all(
            [
                Evidence(
                    id="EVD-NOTOOL-FCT-01",
                    case_id=c1.id,
                    session_id=session_id,
                    kind=EvidenceKind.OBSERVED_FACT.value,
                    source="ORDER_EVENT_LOG_DB",
                    source_ref="worker:SYNC_WORKER_W66",
                    payload_json={
                        "worker_id": "SYNC_WORKER_W66",
                        "batch_id": "BATCH_NOTOOL_1700",
                        "error_code": "ERR_DROPPED_WEBHOOK_SYNC",
                        "event_timestamp": base_ts.isoformat(),
                    },
                    observed_at=base_ts,
                ),
                Evidence(
                    id="EVD-NOTOOL-FCT-02",
                    case_id=c2.id,
                    session_id=session_id,
                    kind=EvidenceKind.OBSERVED_FACT.value,
                    source="ORDER_EVENT_LOG_DB",
                    source_ref=f"sim_orders:{o2.id}",
                    payload_json={
                        "order_id": o2.id,
                        "worker_id": "SYNC_WORKER_W66",
                        "batch_id": "BATCH_NOTOOL_1700",
                        "error_code": "ERR_DROPPED_WEBHOOK_SYNC",
                        "event_timestamp": (base_ts + timedelta(seconds=15)).isoformat(),
                    },
                    observed_at=base_ts + timedelta(seconds=15),
                ),
            ]
        )
        db.commit()

    corr_res = client.post("/api/v1/incidents/correlate", headers=headers)
    assert corr_res.status_code == 200
    inc_id = corr_res.json()["items"][0]["id"]
    detail = client.get(f"/api/v1/incidents/{inc_id}", headers=headers).json()

    assert len(detail["assigned_tasks"]) >= 2
    for t in detail["assigned_tasks"]:
        handoff = TaskHandoffPayload.model_validate(t["handoff_json"])
        # CASE-NO-TOOL-01 had NO executable case read tool -> MUST NOT be in checked_case_ids!
        assert "CASE-NO-TOOL-01" not in handoff.checked_case_ids
        assert "CASE-NO-TOOL-01" in handoff.unchecked_case_ids
        assert handoff.per_case_verification_status["CASE-NO-TOOL-01"] == "UNKNOWN"
        # CASE-HAS-ORD-02 had an executable case read tool -> IS in checked_case_ids!
        assert "CASE-HAS-ORD-02" in handoff.checked_case_ids


def test_a5c9c48_preserves_agent_task_history_referenced_by_case_events_on_recorrelation(
    client: TestClient, active_session: dict[str, str]
) -> None:
    """Review a5c9c48 Item 1:
    - Re-correlating an Incident MUST NOT delete prior `AgentTask` rows referenced by
      `CaseEvent(type='INCIDENT_TASKFORCE_HANDOFF')`.
    - Prior tasks are marked `superseded=True` in `handoff_json` and remain queryable in DB and
      via `GET /api/v1/incidents/{incident_id}/tasks?include_superseded=true`."""
    from app.models.incidents import AgentTask

    token = active_session["token"]
    headers = {"X-Demo-Session": token}

    seed_res = client.post("/api/v1/incidents/scenarios/seed", headers=headers)
    assert seed_res.status_code == 201
    seeded = seed_res.json()
    incident_id = seeded["incident_id"]
    first_case_id = seeded["correlated_case_ids"][0]
    initial_task_ids = set(seeded["assigned_task_ids"])
    assert len(initial_task_ids) == 3

    # Trigger re-correlation (which runs TaskforceManager with force_rerun=True)
    recorr_res = client.post("/api/v1/incidents/correlate", headers=headers)
    assert recorr_res.status_code == 200

    # Active tasks endpoint returns the 3 latest non-superseded tasks
    active_tasks = client.get(f"/api/v1/incidents/{incident_id}/tasks", headers=headers).json()
    assert len(active_tasks) == 3
    active_task_ids = {t["id"] for t in active_tasks}
    assert active_task_ids.isdisjoint(initial_task_ids)

    # Full history endpoint returns all 6 tasks (3 superseded + 3 active)
    all_tasks = client.get(
        f"/api/v1/incidents/{incident_id}/tasks?include_superseded=true",
        headers=headers,
    ).json()
    assert len(all_tasks) == 6
    all_task_ids = {t["id"] for t in all_tasks}
    assert initial_task_ids.issubset(all_task_ids)
    assert active_task_ids.issubset(all_task_ids)

    # Every task_id referenced by CaseEvent(INCIDENT_TASKFORCE_HANDOFF) MUST still exist in DB!
    evts = client.get(f"/api/v1/cases/{first_case_id}/events", headers=headers).json()
    handoff_evts = [e for e in evts if e["type"] == "INCIDENT_TASKFORCE_HANDOFF"]
    assert len(handoff_evts) == 2
    referenced_ids = {
        tid for e in handoff_evts for tid in (e["payload_json"].get("task_ids") or [])
    }
    assert initial_task_ids.issubset(referenced_ids)
    assert active_task_ids.issubset(referenced_ids)

    maker = get_sessionmaker()
    with maker() as db:
        db_tasks = list(db.scalars(select(AgentTask).where(AgentTask.id.in_(referenced_ids))).all())
        assert {t.id for t in db_tasks} == referenced_ids
        for t in db_tasks:
            if t.id in initial_task_ids:
                assert t.handoff_json.get("superseded") is True


def test_a5c9c48_get_refund_history_validates_payment_order_relationship_prevents_cross_mixing(
    active_session: dict[str, str],
) -> None:
    """Review a5c9c48 Item 2:
    - `get_refund_history` with mismatched `(payment_id, order_id)` MUST return `status='ERROR'`
      with `cross_order_payment_mismatch=True` and `outcome='UNKNOWN'` instead of mixing refunds.
    - When an order has two payments (`PAY-A1` and `PAY-A2`) and only `PAY-A2` has a refund,
      querying `get_refund_history(payment_id='PAY-A1', order_id='ORD-A')` MUST return 0 refunds
      for `PAY-A1` (never mixing `PAY-A2`'s refund into `PAY-A1`)."""
    session_id = active_session["id"]
    now = datetime.now(UTC)
    maker = get_sessionmaker()

    with maker() as db:
        ord_a = SimOrder(
            id="ORD-XMIX-A",
            session_id=session_id,
            customer_name="Khách A",
            customer_phone="0966111111",
            amount=300000,
            currency="VND",
            subtotal=300000,
            grand_total=300000,
            status=OrderStatus.PAID.value,
            payment_status=OrderPaymentStatus.PAID.value,
        )
        ord_b = SimOrder(
            id="ORD-XMIX-B",
            session_id=session_id,
            customer_name="Khách B",
            customer_phone="0966222222",
            amount=300000,
            currency="VND",
            subtotal=300000,
            grand_total=300000,
            status=OrderStatus.PAID.value,
            payment_status=OrderPaymentStatus.PAID.value,
        )
        pay_a1 = SimPayment(
            id="PAY-XMIX-A1",
            session_id=session_id,
            order_id=ord_a.id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref="MOMO_XMIX_A1",
            result_code=0,
            amount=300000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=now,
        )
        pay_a2 = SimPayment(
            id="PAY-XMIX-A2",
            session_id=session_id,
            order_id=ord_a.id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref="MOMO_XMIX_A2",
            result_code=0,
            amount=300000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=now,
        )
        pay_b = SimPayment(
            id="PAY-XMIX-B1",
            session_id=session_id,
            order_id=ord_b.id,
            merchant_id="MERCHANT_MOCK_01",
            provider="MOCK_MOMO",
            provider_ref="MOMO_XMIX_B1",
            result_code=0,
            amount=300000,
            currency="VND",
            status=PaymentStatus.SUCCESS.value,
            verified_at=now,
        )
        ref_a2 = SimRefund(
            id="REF-XMIX-A2",
            session_id=session_id,
            order_id=ord_a.id,
            payment_id=pay_a2.id,
            amount=100000,
            requested_amount=100000,
            approved_amount=100000,
            completed_amount=100000,
            currency="VND",
            request_id="REQ-XMIX-A2",
            status=RefundStatus.COMPLETED.value,
            reason="Refund on PAY-A2 only",
        )
        ref_b = SimRefund(
            id="REF-XMIX-B1",
            session_id=session_id,
            order_id=ord_b.id,
            payment_id=pay_b.id,
            amount=300000,
            requested_amount=300000,
            approved_amount=300000,
            completed_amount=300000,
            currency="VND",
            request_id="REQ-XMIX-B1",
            status=RefundStatus.COMPLETED.value,
            reason="Refund on ORD-B",
        )
        db.add_all([ord_a, ord_b, pay_a1, pay_a2, pay_b, ref_a2, ref_b])
        db.commit()

        reg = ReadToolRegistry(db, session_id)

        # 1. Cross-order mismatch: PAY-XMIX-A1 (belongs to ORD-XMIX-A) queried with ORD-XMIX-B
        res_mismatch = reg.execute(
            "get_refund_history",
            {"payment_id": "PAY-XMIX-A1", "order_id": "ORD-XMIX-B"},
        )
        assert res_mismatch.status == "ERROR"
        assert res_mismatch.data["cross_order_payment_mismatch"] is True
        assert res_mismatch.data["outcome"] == "UNKNOWN"
        assert res_mismatch.data["refunds"] == []

        # 2. Same order (ORD-XMIX-A) with two payments: querying PAY-XMIX-A1 MUST NOT return
        #    REF-XMIX-A2 (which belongs to PAY-XMIX-A2)!
        res_a1 = reg.execute(
            "get_refund_history",
            {"payment_id": "PAY-XMIX-A1", "order_id": "ORD-XMIX-A"},
        )
        assert res_a1.status == "OK"
        assert res_a1.data["payment_found"] is True
        assert res_a1.data["refund_count"] == 0
        assert res_a1.data["refunds"] == []
        assert res_a1.data["authority_status"] == "VERIFIED_EMPTY_REFUND_HISTORY"

        # 3. Querying PAY-XMIX-A2 returns ONLY REF-XMIX-A2
        res_a2 = reg.execute(
            "get_refund_history",
            {"payment_id": "PAY-XMIX-A2", "order_id": "ORD-XMIX-A"},
        )
        assert res_a2.status == "OK"
        assert res_a2.data["refund_count"] == 1
        assert res_a2.data["refunds"][0]["refund_id"] == "REF-XMIX-A2"
