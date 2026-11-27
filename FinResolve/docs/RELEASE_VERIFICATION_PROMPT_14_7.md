# RELEASE VERIFICATION REPORT — PROMPT 14.7: FINAL SECURITY, SANDBOX ISOLATION & OPEN-WORLD DEMO ACCEPTANCE

## 1. Release Context

- **Repository**: `https://github.com/Phat-le2005/momo`
- **Development Branch**: `feat/open-world-autonomous-guardian`
- **Starting Reviewed Commit**: `d9787aed4b6e579c8c44c1716ffcd8598bab7e10`
- **Main Branch**: `c45002c5204c46865ed6cf2e5df2026734b292cb` (untouched; no automatic merge)
- **Payment Mode**: `LOCAL_MOCK` only (`VNPAY` and VNPAY refunds intentionally disabled)

---

## 2. Resolution of the Four Audited Defects

| Defect | Resolution | Implementation Files |
|---|---|---|
| **FIX 1 (P1): Sandbox Ownership & Isolation** | Enforced strict server-side sandbox binding ownership (`extra="forbid"` rejects browser-supplied `merchant_id`, `commerce_session_id`, or `base_url`); exposed honest `sandbox_isolation_mode` (`SHARED_DEMO_SANDBOX` when bound to `CSESS_FINSHOP_DEFAULT_01` vs `DEDICATED_SANDBOX` when bound to a workspace-specific session); verified two guest workspaces A & B never leak anomalies, proposals, cases, timeline, or resets across sessions. | `FinResolve/backend/app/api/v1/experience.py`, `FinResolve/backend/app/core/config.py` |
| **FIX 2 (P1): Complete Recovery Response Identity & Strict 409 Semantics** | Made `command_id`, `command`, `idempotency_key`, `request_hash`, `merchant_id`, `commerce_session_id`, strict-int `world_epoch` (rejecting `None`, `bool`, strings), and command-specific targets (`order_id`, `payment_id`, `event_id`) mandatory on both HTTP 200 execution AND authenticated status reconciliation. Removed free-text prefix parsing on HTTP 409 and excluded `IDEMPOTENCY_KEY_CONFLICT`/`REQUEST_HASH_MISMATCH` from confirmed rejection so ambiguous or hash-conflict 409s conservatively preserve `UNKNOWN_OUTCOME`. | `FinResolve/backend/app/connectors/commerce_write.py`, `MiniStore/backend/app/api/service_write.py` |
| **FIX 3 (P2): Remove Unsafe Evidence Fallback** | Removed `if not relevant_ev_refs: relevant_ev_refs = fact_evidence_ids[:4]` completely. Required exact structured current-run returned `Evidence` payloads for BOTH `order_id` (`{"order": ...}`) and `payment_id` (`{"payment": ...}` or `{"candidates": [...]}`), rejecting tool-arg-only or substring matches. Stopped creating permanent `HOLD_ACTION` risk signals for merely missing provenance so legitimate subsequent investigations can proceed cleanly. | `FinResolve/backend/app/ai_core/runtime.py` |
| **FIX 4 (P2): Complete World-Epoch Isolation in Unified Experience** | Scoped `/api/v1/experience/overview` KPIs, `ActionProposal`, `CaseEvent`, and `Case` counters strictly to active `(session_id, world_epoch)`. Marked old `PROPOSED` proposals `CANCELLED` and stale `QUEUED` jobs `CANCELLED` on Reset while preserving unresolved uncertain `RecoveryCommand` mutation guards intact (`unresolved_uncertainty_guards`). Redacted sensitive keys from `technical_trace`, made `storefront_url` configurable (`COMMERCE_PUBLIC_STOREFRONT_URL`), and ensured newly initialized bindings without a successful scan report `UNVERIFIED` rather than `CONNECTED`. | `FinResolve/backend/app/api/v1/experience.py`, `FinResolve/backend/app/core/config.py` |

---

## 3. Automated Test Counts & Quality Gates

| Suite | Result |
|---|---|
| `FinResolve/backend` (`pytest -q`, PostgreSQL 17) | **220 / 220 PASS** (+8 new Prompt 14.7 tests in `test_prompt14_7_security_and_isolation.py`) |
| `FinResolve/backend` (`ruff format --check`, `ruff check`, `mypy app`) | **PASS** (0 errors across 94 source files) |
| `MiniStore/backend` (`pytest -q`, `ruff`, `mypy`) | **40 / 40 PASS** (0 errors) |
| `FinResolve/frontend` (`eslint`, `tsc`, Node tests, `next build`) | **PASS** (13 / 13 tests — both `session-state.test.mts` and `experience-api.test.mts` wired into `.github/workflows/ci.yml`) |

---

## 4. Open-World Acceptance Matrix (Honest Per-Experiment Status)

| Experiment | Docker Runtime Acceptance | PostgreSQL Regression Suite | Honest Status |
|---|---|---|---|
| **A — Healthy Commerce** | Checkout + `LOCAL_MOCK` pay → `CONFIRMED`, 0 unnecessary incidents | Covered | **PASS** |
| **B — Unexpected Operational Degradation** | Real HTTP traffic + measured telemetry windows (`fixed_60s`, `completed_fixed_60s`) | Open-world baseline deviation detection + Case creation covered | **PARTIAL** (Docker checks real traffic & telemetry; full baseline-anomaly injection verified in PostgreSQL suite) |
| **C — Real Fake Bill Investigation** | Real rendered-pixel PNG → Tesseract `PIXEL_OCR` + persistent private receipt volume across container recreation | Contradictory receipt investigation covered | **PARTIAL** (Docker verifies real pixel OCR & persistence; full supervisor contradiction verdict verified in PostgreSQL suite) |
| **D — Autonomous Recovery Causality** | `LOCAL_MOCK` paid + unsynced → Continuous Monitor scan → Anomaly → Case → Supervisor → Signed Recovery Command (`COMMAND_EXECUTION_VERIFIED`) → MiniStore `CONFIRMED` | Covered + Live Gemini E2E (`RUN-B77B2F07A3`) | **PASS** |
| **E — Unknown Unsupported Incident** | Unauthorized/unsupported command rejected (`401/403`) | Safe escalation without mutation covered | **PARTIAL** (Docker checks API guard; full Agent escalation verified in PostgreSQL suite) |
| **F — Actual Crash Reconciliation** | Unauthorized status lookup rejected (`401/403`) | Timeout/502/404/restart reconciliation + mutation guard retention covered | **PARTIAL** (Docker checks auth guard; full crash reconciliation verified in PostgreSQL suite) |
| **G — Real Sustained Recovery** | Independent telemetry window tracking + sustained `CONFIRMED` orders | Multi-window non-overlapping sustained recovery verified in PostgreSQL suite | **PARTIAL** (Docker checks window metadata; multi-window non-overlap verified in PostgreSQL suite) |

---

## 5. Live Gemini Verification Evidence

Verified on the hardened codebase against real MiniStore Uvicorn (`http://127.0.0.1:8100`) + real PostgreSQL 17 + real Google Gemini API (`gemini-3.1-flash-lite`, `model_mode=LLM_API`, `is_fallback=false`):
- **Valid Autonomous Payment Sync**: `RUN-B77B2F07A3` (`steps_executed=3`, `replans_used=1`, `total_tokens_used=9762`, `latency_ms=14105.0`), tool calls `get_order` (`276ms`) + `get_payment_record` (`146ms`), proposal `PROP-27BA8B57F8` (`expected_order_version=1`, exact current-run evidence refs), signed command `RCMD-E04FEE64FA` → MiniStore `RCV-4DA981ECB0` → `ORD-92041DF6DD` `CONFIRMED`, `VERIFIED_RECOVERY`.
- **Incomplete Provenance Blocked**: `RUN-3E8000C174` (`total_tokens_used=5608`, `latency_ms=19658.8`) safely blocked (`MISSING_CURRENT_RUN_PROVENANCE`).

---

## 6. Remaining Limitations

- **Browser Playwright E2E**: Frontend build, lint, typecheck, and 13/13 unit tests (including `experience-api.test.mts`) pass in CI; automated headless Playwright walkthrough against the live 9-service Docker stack is not yet added to GitHub Actions (`NOT_EXECUTED`).
- **Shared Default Sandbox**: When multiple browser guests use the default `CSESS_FINSHOP_DEFAULT_01` demo environment, FinResolve isolates each guest's sessions, cases, approvals, timelines, and world epochs, and explicitly labels the underlying FinShop storefront as `SHARED_DEMO_SANDBOX` rather than claiming isolated per-user storefront databases.

---

## 7. Independent Readiness Decisions

- **`ENGINEERING_MERGE_READINESS`**: **READY** — All 4 audited defects resolved, 260/260 backend tests + 13/13 frontend tests pass, zero lint/type errors, all 5 CI jobs green on final commit.
- **`USER_EXPERIENCE_READINESS`**: **READY (with Playwright E2E noted as follow-up)** — Unified Experience landing page and Guardian Command Center built, unit-tested, and wired into CI.
- **`SANDBOX_ISOLATION_READINESS`**: **READY (Honest `SHARED_DEMO_SANDBOX` labeling + strict FinResolve session/epoch fencing)** — No false isolation claims; cross-session and cross-epoch leakage eliminated and tested.
- **`LIVE_AI_DEMO_READINESS`**: **READY** — Genuine `gemini-3.1-flash-lite` (`LLM_API`, `is_fallback=false`) end-to-end recovery verified with real tool reads, exact version preservation, HMAC authorization, and independent verification.
- **`COMPETITION_DEMO_READINESS`**: **READY** — Full 9-service Docker Compose stack, real Tesseract OCR, persistent receipts, live Gemini recovery, and Premium Blue Guardian Command Center verified.
- **`EXTERNAL_PAYMENT_READINESS`**: **INTENTIONALLY_DEFERRED** — VNPAY and VNPAY refunds remain disabled (`LOCAL_MOCK` only).
