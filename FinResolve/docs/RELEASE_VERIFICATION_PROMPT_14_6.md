# RELEASE VERIFICATION REPORT — PROMPT 14.6: FINAL REGRESSION FIX, SAFETY HARDENING & RELEASE VALIDATION

## 1. Release Context

- **Repository**: `https://github.com/Phat-le2005/momo`
- **Development Branch**: `feat/open-world-autonomous-guardian`
- **Starting Reviewed Commit**: `b385b44797f5104f592850a6d509d27214c7b406`
- **Main Branch**: `c45002c5204c46865ed6cf2e5df2026734b292cb` (untouched; no automatic merge)
- **Payment Mode**: `LOCAL_MOCK` only (`VNPAY` and VNPAY refunds intentionally disabled)

---

## 2. Root Cause of the 13 Regression Failures (FIX 1 — P0)

Commit `b385b44` persisted an extra `"expected_version"` key alongside `"expected_order_version"` inside `ActionProposal.params_json` for `ORDER_SYNC` proposals in `FinResolve/backend/app/ai_core/runtime.py`.

Because `OrderSyncActionParams` in `FinResolve/backend/app/policy/contracts.py` strictly defines `expected_order_version` with `ConfigDict(extra="forbid")`, `PolicyEngine` correctly rejected approval/execution with `409 Conflict` / `403 Forbidden` across 13 existing tests.

### Resolution (Without Weakening Policy Validation)
- Preserved `OrderSyncActionParams` with `extra="forbid"` and `expected_order_version` as the single canonical business parameter.
- Removed the duplicate `"expected_version"` key from `ActionProposal.params_json` in both simulator and `COMMERCE_HTTP` proposal paths (`b8dbd3b`).
- Kept `ActionProposal.expected_version` strictly for Case optimistic locking and `params_json["expected_order_version"]` strictly for target Order optimistic locking.
- Added explicit rejection of contradictory version aliases (`expected_order_version` vs `expected_version` when both supplied with differing values) in `runtime.py`.

### All 13 Restored Regression Tests (PASS)
1. `tests/test_agentic_ai_upgrade_prompt08_2.py::test_08_2_no_db_row_lock_held_during_llm_invocation_and_multi_step_e2e`
2. `tests/test_autonomous_mission_control_prompt09.py::test_prompt09_test5_and_test6_duplicate_refund_and_auto_continuation_after_approval`
3. `tests/test_incident_taskforce_prompt05.py::test_i03_shared_incident_and_single_child_resolution_never_auto_resolves_other_children`
4. `tests/test_policy_execution_prompt03.py::test_t01_full_e2e_complaint_investigate_propose_approve_execute_verify_resolved`
5. `tests/test_policy_execution_prompt03.py::test_t04_execution_timeout_records_unknown_and_blocks_blind_retry`
6. `tests/test_policy_execution_prompt03.py::test_t06_idempotency_approval_replay_and_concurrent_execution`
7. `tests/test_policy_execution_prompt03.py::test_t11_t12_legacy_digest_rejected_and_identity_parameter_tampering_blocked`
8. `tests/test_policy_execution_prompt03.py::test_t13_order_version_changed_before_write_rejected_on_recheck`
9. `tests/test_policy_execution_prompt03.py::test_t20_executor_returns_200_but_postcondition_not_met_fails_verification`
10. `tests/test_policy_execution_prompt03.py::test_caffa301_public_session_never_returns_manager_capability_and_simulated_approval_labeled`
11. `tests/test_policy_execution_prompt03.py::test_caffa301_recovery_for_committed_execution_stuck_in_verifying_without_repeating_write`
12. `tests/test_prompt09_1_1_final_hardening.py::test_bulk_challenge_single_case_verification_sets_partially_resolved`
13. `tests/test_simulator_prompt06.py::test_prompt06_scenario_catalog_and_payment_sync_db_diff_after_action`

---

## 3. Safety Hardening Delivered (FIXES 2, 3, 4, 5)

| Fix | Scope | Key Implementation Files |
|---|---|---|
| **Fix 2 (P1)** | Preserve exact Agent `expected_order_version` on `COMMERCE_HTTP` proposals; reject missing, stale, non-integer, or contradictory version aliases instead of silently overwriting with latest DB version | `FinResolve/backend/app/ai_core/runtime.py`, `schemas.py`, `context.py` |
| **Fix 3 (P1)** | Require complete receiver HTTP 200 identity envelope (`command_id`, `command`, `idempotency_key`, `request_hash`, `merchant_id`, `world_epoch`, target `order_id`, explicit supported `status`); classify empty/missing/unknown responses as `UNKNOWN_OUTCOME` (`COMMAND_EXECUTION_UNKNOWN`) | `FinResolve/backend/app/connectors/commerce_write.py`, `MiniStore/backend/app/api/service_write.py` |
| **Fix 4 (P1)** | Distinguish HTTP 409 conflict semantics via machine-readable `reason_code`: confirmed pre-mutation rejections (`VERSION_MISMATCH`, `STALE_WORLD_EPOCH`, `IDEMPOTENCY_KEY_CONFLICT`) -> `FAILED/CONFIRMED_REJECTED`; in-flight or undocumented conflicts (`COMMAND_IN_FLIGHT`, unknown) -> `UNKNOWN_OUTCOME` with mutation guard retained | `FinResolve/backend/app/connectors/commerce_write.py`, `MiniStore/backend/app/api/service_write.py` |
| **Fix 5 (P2)** | Strengthen current-run evidence provenance for `COMMERCE_HTTP`: require BOTH `get_order` AND `get_payment_record` (or `find_payment_candidates`) in current run, validate merchant scope, payment-to-order link, amount, currency, and attach only relevant target evidence refs | `FinResolve/backend/app/ai_core/runtime.py`, `context.py` |

---

## 4. Automated Test Results (Local PostgreSQL 17 Verification)

| Suite | Count & Result |
|---|---|
| `FinResolve/backend` (`pytest -q`) | **212 / 212 PASS** (including all 13 restored regression tests + 12 new Prompt 14.6 hardening tests in `test_prompt14_6_hardening.py`) |
| `FinResolve/backend` (`ruff check app tests` + `mypy app`) | **PASS** (0 errors across 94 source files) |
| `MiniStore/backend` (`pytest -q`, `ruff`, `mypy`) | **40 / 40 PASS** (0 lint/type errors) |
| `FinResolve/frontend` (`eslint`, `tsc`, Node tests, `next build`) | **PASS** (13/13 unit tests, 9/9 routes built) |

---

## 5. Live Gemini Regression Verification (Hardened Implementation)

Executed against real MiniStore Uvicorn (`http://127.0.0.1:8100`) + real PostgreSQL 17 + real Google Gemini API (`gemini-3.1-flash-lite`) with zero mock transports:

### Run 1 — Valid Autonomous Payment Sync Recovery (`VERIFIED_RECOVERY`)
- **Model**: `gemini-3.1-flash-lite` (`model_mode=LLM_API`, `is_fallback=false`)
- **Case / Order / Payment**: `CASE-MON-F9E6D589C8` / `ORD-92041DF6DD` / `PAY-4AFE562337`
- **Run ID**: `RUN-B77B2F07A3` (`steps_executed=3`, `replans_used=1`, `total_tokens_used=9762`, `latency_ms=14105.0`)
- **Current-Run Tool Calls**:
  1. `get_order(order_id="ORD-92041DF6DD")` -> `EVD-41CBBBDC3D` (`276 ms`)
  2. `get_payment_record(payment_id="PAY-4AFE562337")` -> `EVD-2E0B81202C` (`146 ms`)
- **Agent Proposal**: `PROP-27BA8B57F8` (`ORDER_SYNC`, `expected_order_version=1`, `amount=274000`, `currency="VND"`, `evidence_refs=["EVD-2E0B81202C", "EVD-41CBBBDC3D"]`)
- **Policy & Signed Execution**: `RCMD-E04FEE64FA` (`policy_decision=ALLOW_POLICY_ENGINE_LOCAL_MOCK_L`, MiniStore receiver `RCV-4DA981ECB0`, verified complete identity envelope + `request_hash`)
- **Final Verified State**: MiniStore order `ORD-92041DF6DD` -> `CONFIRMED` (`version=2`), `is_synced_to_order=true`, `execution_certainty=COMMAND_EXECUTION_VERIFIED`, `verdict=VERIFIED_RECOVERY`.

### Run 2 — Incomplete Current-Run Provenance Blocked Live
- **Model**: `gemini-3.1-flash-lite` (`model_mode=LLM_API`, `is_fallback=false`)
- **Run ID**: `RUN-3E8000C174` (`total_tokens_used=5608`, `latency_ms=19658.8`)
- **Outcome**: Agent attempted `ORDER_SYNC` after reading only `get_order` -> blocked by Fix 5 provenance guard (`UNSAFE_PROPOSAL_BLOCKED: MISSING_CURRENT_RUN_PROVENANCE`, `RSK-6AFFC13292`, `final_state=WAITING_EVIDENCE`). Subsequent mutation attempt blocked by Policy Engine (`DENY:RISK_HOLD_ACTION_ACTIVE`, `RCMD-E33977C095`).

---

## 6. Docker Compose Runtime Acceptance Coverage (`FinResolve/infra/runtime_acceptance.py`)

Extended `runtime_acceptance.py` (executed by `.github/workflows/runtime-acceptance.yml` across all 9 services):
- **Experiment A (Healthy commerce)**: LOCAL_MOCK checkout + payment -> `CONFIRMED`, no unnecessary incident creation.
- **Experiment B (Traffic & Telemetry)**: Real HTTP traffic -> measured `fixed_60s` and `completed_fixed_60s` telemetry windows.
- **Experiment C (Suspicious receipt + Pixel OCR)**: Synthetic contradictory receipt PNG (zero metadata) -> real Tesseract `PIXEL_OCR` + persistent private receipt volume verified across container recreation.
- **Experiment D (Autonomous recovery)**: LOCAL_MOCK payment with `auto_sync_order=False` -> FinResolve Continuous Monitor scan -> automatic anomaly detection, Case creation, Supervisor investigation, signed `REQUEST_ORDER_SYNC` -> MiniStore order independently verified `CONFIRMED`.
- **Experiment E & F (Unsupported command & reconciliation guards)**: Unauthorized/unsupported command (`DROP_DATABASE`) and unauthorized command status lookup rejected (`401/403`).
- **Experiment G (Sustained recovery & window integrity)**: Verified independent telemetry window tracking and sustained `CONFIRMED` order states.

---

## 7. Independent Readiness Decisions

- **`ENGINEERING_MERGE_READINESS`**: **READY** (pending final commit CI green confirmation on GitHub Actions; all local backend 252/252 and frontend 13/13 tests + builds passed).
- **`LIVE_AI_DEMO_READINESS`**: **READY** — genuine `gemini-3.1-flash-lite` (`LLM_API`, `is_fallback=false`) verified end-to-end on the hardened codebase (`RUN-B77B2F07A3`, `PROP-27BA8B57F8`, `RCMD-E04FEE64FA`, `VERIFIED_RECOVERY`).
- **`EXTERNAL_PAYMENT_READINESS`**: **INTENTIONALLY_DEFERRED** — VNPAY and VNPAY refunds remain disabled (`LOCAL_MOCK` only).
