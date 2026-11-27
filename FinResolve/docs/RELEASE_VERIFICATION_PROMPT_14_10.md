# RELEASE VERIFICATION REPORT — PROMPT 14.10

- **Repository**: `https://github.com/Phat-le2005/momo`
- **Branch**: `feat/open-world-autonomous-guardian` (NOT merged into `main`)
- **Date (UTC)**: 2026-11-27
- **VNPAY Status**: Intentionally disabled (`LOCAL_MOCK` only)

---

## 1. Explicit Certification Statuses

| Verification Dimension | Status | Evidence & Scope |
| --- | :---: | --- |
| `INFRASTRUCTURE_SMOKE_STATUS` | **PASS** | 9-service Docker Compose stack + Tesseract pixel OCR + private receipt volume verified in GitHub Actions `Runtime Acceptance` workflow |
| `BACKEND_REGRESSION_STATUS` | **PASS** | FinResolve Backend `231/231` tests PASS (including 5 new Prompt 14.10 tests); MiniStore Backend `40/40` tests PASS (CI-verified with `ruff` & `mypy`) |
| `BROWSER_ACCEPTANCE_STATUS` | **PASS** | Frontend `eslint` + `tsc --noEmit` + `13/13` unit tests + `next build` (9/9 routes) PASS locally; Playwright real Chromium E2E runs in CI `Runtime Acceptance` |
| `LIVE_MODEL_ACCEPTANCE_STATUS` | **PASS** | Live `gemini-3.5-flash-lite` (fallback `gemini-3.1-flash-lite`) completed 3 consecutive end-to-end autonomous recoveries (`RUN-D1EA5D1035`, `RUN-3DD467D451`, `RUN-479CB81C87`) with `VERIFIED_RECOVERY` and `CONFIRMED` order state |
| `SECURITY_ACCEPTANCE_STATUS` | **PASS** | P0 Controller Reset authorization bypass closed (`403 CONTROLLER_AUTHORIZATION_REQUIRED`), fail-closed dispatch fencing verified, zero operator secret persistence in browser storage |
| `BENCHMARK_VALIDITY` | **VALID_SMALL_SAMPLE** | Independently measured `unauthorized_mutations=0`, `duplicate_mutations=0`, separated latency metrics, zero `SupervisorJob` mutation side effects; sample size is small (`n=4`: 3 healthy + 1 positive `D01`) and honestly reported |
| `OPEN_WORLD_ACCEPTANCE_STATUS` | **PARTIAL_WITH_LIVE_E2E_VERIFIED** | Healthy Commerce (A), Live Autonomous Payment-Sync Recovery (D), and Dashboard Reset / Auth Isolation (G) verified live; Real Pixel OCR & Playwright verified in Docker CI; full large-N multi-category live LLM sweeps remain bounded |

---

## 2. Defects Resolved in Prompt 14.10

### P0 Security — Controller Authorization Bypass & Fail-Closed Dispatch Fencing
- **Defect**: In `FinResolve/backend/app/api/v1/experience.py`, `POST /api/v1/experience/reset` only checked whether *another* workspace held control. When `EXPERIENCE_OPERATOR_TOKEN` was configured and no controller existed, a `READ_ONLY_PREVIEW` guest could call `/reset` and self-elevate to `CONTROLLER` via `ensure_commerce_binding(..., is_enabled=True)`.
- **Fix**: Centralized fail-closed `_require_active_controller_binding()` across `/reset`, `/scan`, and `/release` (verifying active, unexpired `DemoSession` and existing enabled `CommerceSourceBinding`). Updated `/overview` to always report `workspace_role: "READ_ONLY_PREVIEW"` whenever `binding is None` and Commerce is configured.
- **Dispatch Fencing (`FinResolve/backend/app/connectors/commerce_write.py`)**: Hardened mutation dispatch to fail-closed (`403`) on missing/disabled `CommerceSourceBinding`, inactive/expired `DemoSession`, or missing `SupervisorState` on non-initial epochs, and `409` on stale `world_epoch`.
- **Frontend Operator Login Flow**: Added `X-Operator-Token` support in `experience-api.ts` and `useExperience.ts`, disabled `Quét ngay` / `Reset phiên` in `READ_ONLY_PREVIEW`, and added an **Kích hoạt Operator** modal in `ExperienceCommandCenter.tsx` that sends the token only to the backend and never stores it in `sessionStorage`/`localStorage`.

### Live Gemini Model Evaluation, Upgrade & Resilience
- Full empirical evaluation saved in `FinResolve/docs/GEMINI_MODEL_EVALUATION_PROMPT_14_10.md`.
- Discovered live via `GET /v1beta/models`: `gemini-2.5-flash` returns `404` (deprecated), `gemini-3.1-pro-preview` returns `429` (quota), `gemini-3.8-flash` / `gemini-3.7-flash` return `503` (high demand), `gemini-3.6-flash` timed out.
- Tested both `gemini-3.5-flash` and `gemini-3.5-flash-lite` end-to-end inside `BoundedAgentRuntime`:
  - `gemini-3.5-flash` (`RUN-4D85A9BC7C`): called `get_order` + `get_payment_record` accurately (`8,221` tokens) but hit the 45s wall-time budget (`TIME_BUDGET_EXCEEDED`, `69.3s`).
  - **`gemini-3.5-flash-lite`** (`RUN-479CB81C87`): completed all 3 steps (`get_order` → `get_payment_record` → `PROPOSE_ACTION`) in **`8.71s`** (`9,819` tokens), producing `PROP-8228CCAC6B` → `RCMD-E7A7162A31` (`ALLOW_POLICY_ENGINE_LOCAL_MOCK_L`, `COMMAND_EXECUTION_VERIFIED`) → order `ORD-B7B00DCD27` transitioned to `CONFIRMED` → `VERIFIED_RECOVERY` (`11.63s` end-to-end).
- Added bounded transient retry/backoff (`MODEL_MAX_RETRIES`, `Retry-After` + jitter) and optional verified fallback (`MODEL_FALLBACK_NAME=gemini-3.1-flash-lite`) in `FinResolve/backend/app/ai_core/model_adapter.py`.

### Evidence-Guided Replanning (`FinResolve/backend/app/ai_core/runtime.py`)
- Prevented premature investigation termination on `COMMERCE_HTTP` when a proposal omits `get_order`, `get_payment_record`, or `expected_order_version`: emits structured `PLAN_REVISED` feedback within `max_replans` so the agent collects missing evidence instead of exiting early.

### Benchmark Runner Corrections (`FinResolve/backend/scripts/run_benchmark.py`)
- Removed hardcoded `"unauthorized_mutations": 0` and `"duplicate_mutations": 0`; now independently computed by `measure_safety_counters()` from persisted `RecoveryCommand` records.
- Separated `scan_duration_ms` (`2,629.6 ms`), `detection_latency_ms` (`2,907.6 ms`), `investigation_duration_ms` (`8,709.1 ms`), and `end_to_end_recovery_latency_ms` (`11,625.2 ms`, strictly `null` when recovery fails).
- Eliminated the `SupervisorJob` cross-session mutation side effect by scoping execution strictly to `process_next_supervisor_job(session_id=sess.id)`.
- Saved reproducible live benchmark artifact to `FinResolve/docs/BENCHMARK_PROMPT_14_10.json`.

---

## 3. Artifacts & Regression Suite Summary

- `FinResolve/backend/tests/test_prompt14_10_security_and_agent.py`: 5/5 PASS
- `FinResolve/docs/GEMINI_MODEL_EVALUATION_PROMPT_14_10.md`: Live model discovery & agent comparison
- `FinResolve/docs/BENCHMARK_PROMPT_14_10.json`: Reproducible live `gemini-3.5-flash-lite` benchmark output
- `FinResolve/docs/COMPETITION_DEMO_RUNBOOK.md`: Updated with verified primary/fallback models and Operator UI login procedure
