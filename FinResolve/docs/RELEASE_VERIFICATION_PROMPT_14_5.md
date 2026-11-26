# FinResolve AI — Prompt 14.5 Final Runtime Safety & Acceptance Report

## 1. Commit Lineage

- **Repository**: `Phat-le2005/momo`
- **Development branch**: `feat/open-world-autonomous-guardian` (not merged to `main`)
- **Main branch**: `c45002c5204c46865ed6cf2e5df2026734b292cb`
- **Starting HEAD (Prompt 14.5)**: `21445e20a047ebd0fad711d70aa7c92422babb3e`
- **Verified Runtime Acceptance Commit**: `f409574b944ab8b1aee615146d86ba419b80487e`
- **Commits delivered in Prompt 14.5**:
  1. `8861b41` — `fix(recovery): preserve uncertainty for all ambiguous dispatch outcomes`
  2. `aca14b2` — `fix(agent): enforce fail-closed worker ownership and lease fencing`
  3. `889ecc4` — `fix(policy): authorize exact agent targets and supporting evidence`
  4. `d7ef15a` — `fix(baseline): learn only from valid completed evaluation windows`
  5. `7cf6bf4` — `test(runtime): execute docker compose acceptance in CI`
  6. `f409574` — `test(runtime): fix order detail unwrap and service header in acceptance script`

---

## 2. Resolution of All Four Audited Prompt 14.5 Defects

| Fix | Priority | Relevant Files & Functions | Regression Coverage |
| --- | --- | --- | --- |
| **Fix 1: Ambiguous Recovery Outcome Classification** | P1 | `FinResolve/backend/app/connectors/commerce_write.py` (`execute_and_verify_commerce_recovery`, `reconcile_unknown_recovery_command`, `resolve_uncertain_recovery_command`) | Replaced string-based timeout detection with explicit execution certainty classification (`CONFIRMED_REJECTED`, `COMMAND_EXECUTION_UNKNOWN`, `COMMAND_EXECUTION_VERIFIED`, `CONFIRMED_ESCALATED`, `BUSINESS_STATE_RECOVERED`). Connection resets, 5xx errors, and HTTP 200 identity mismatches preserve `UNKNOWN_OUTCOME` and retain conflict guards; semantic HTTP 200 rejections and 4xx classify as `CONFIRMED_REJECTED`; manual uncertainty resolution requires authorized role + supporting evidence + immutable audit event. Tested in `test_prompt14_3_unknown_outcome_safety.py`. |
| **Fix 2: Fail-Closed Worker Ownership & Lease Fencing** | P1 | `FinResolve/backend/app/monitoring/recovery_planner.py` (`validate_and_execute_agent_proposal`), `FinResolve/backend/app/supervisor/service.py` (`_run_commerce_post_investigation_recovery`) | Autonomous mutations require an existing `SupervisorJob` row (locked via `FOR UPDATE`), `status == RUNNING`, matching `session_id`, non-empty matching `claim_token`, and non-null unexpired `locked_until`. Missing job, claim token, or lease fails closed (`409`). Failed `CLAIMING` proposals transition safely to `REJECTED`. Tested in `test_prompt14_4_proposal_provenance.py`. |
| **Fix 3: Exact Agent Targets & Evidence Provenance** | P1 | `FinResolve/backend/app/monitoring/recovery_planner.py` (`validate_and_execute_agent_proposal`) | Enforced command-specific required target schemas (`REQUEST_ORDER_SYNC` / `REQUEST_PAYMENT_RECONCILIATION` require explicit `target_order_id`, `target_payment_id`, and `expected_version`; `REQUEST_EVENT_REDELIVERY` requires explicit `event_id` or `target_order_id`). Never substitutes anomaly defaults for missing Agent targets. Enforced 24-hour evidence freshness and `order_id` target correspondence. Tested in `test_prompt14_4_proposal_provenance.py` and `test_prompt14_2_ai_proposal_execution.py`. |
| **Fix 4: Valid Completed Evaluation Windows Only** | P2 | `FinResolve/backend/app/monitoring/service.py` (`run_binding_scan`) | Removed rolling `10s`/`60s` fallback from baseline learning; requires explicit `completed_fixed_60s` (or completed `fixed_60s`) with `is_complete is True`, valid `window_start < window_end`, non-stale samples, and durable deduplication via `CommerceSourceBinding.last_learned_window_id`/`last_learned_window_end`. Counts interval business throughput from authoritative transition events (`ORDER_CONFIRMED`, `PAYMENT_SYNCED_TO_ORDER`, `REFUND_SUCCEEDED`/`REFUND_COMPLETED`) strictly within `[window_start, window_end)`. Tested in `test_prompt14_3_telemetry_windows.py` and `test_prompt14_1_open_world_runtime.py`. |

---

## 3. Real Docker Compose Runtime Acceptance & CI Evidence (Commit `f409574`)

All **5/5** GitHub Actions jobs passed:

- **Runtime Acceptance Workflow (9-Service Docker Compose Stack)** — **SUCCESS**:
  - Run `45816992049`: https://github.com/Phat-le2005/momo/actions/runs/45816992049/job/139899175069
  - Sanitized artifact uploaded: `runtime-acceptance-evidence` (`runtime-acceptance-report.json`, `compose-ps.txt`, `compose-ps-final.txt`)
  - Verified live inside Docker Compose (`ubuntu-latest`):
    - Ephemeral 256-bit secret generation (`FinResolve/infra/init_local_secrets.py`, secrets redacted)
    - `docker compose config --quiet` & `docker compose up -d --build` (all 9 containers: `db`, `api`, `worker`, `monitor-worker`, `web`, `commerce-db`, `commerce-api`, `commerce-worker`, `commerce-web`)
    - `/readyz` on FinResolve API & MiniStore API, `/api/v1/health/ocr` (`ocr_ready: true`, `eng` + `vie`), `receipt_storage_writable: true`, and both Next.js frontends (`/demo`, `/`)
    - Unauthorized `/api/v1/service/recovery/execute` rejected (401/403)
    - Experiment A: Healthy `LOCAL_MOCK` checkout -> payment -> order confirmation (`CONFIRMED`)
    - Experiment C: Pure Pillow-rendered PNG receipt (zero `tEXt`/`iTXt` metadata) -> real Tesseract `PIXEL_OCR_EXTRACTED` -> `docker compose up -d --force-recreate --no-deps commerce-api` -> private receipt file verified intact on persistent volume `/var/lib/finshop/private_receipts`
    - Experiment B: Real HTTP traffic -> measured `fixed_60s` and `completed_fixed_60s` telemetry windows
    - FinResolve Supervisor worker health & status check
    - Guaranteed `docker compose down -v` cleanup
- **FinResolve CI Workflow (Run `45816991853`)** — **4/4 SUCCESS**:
  - `API (PostgreSQL 17, Alembic, Ruff, Mypy, Pytest)` (197+ tests): https://github.com/Phat-le2005/momo/actions/runs/45816991853/job/139899174306
  - `FinShop Commerce API (PostgreSQL 17, Alembic, Ruff, Mypy, Pytest)` (40 tests + real Ubuntu Tesseract OCR): https://github.com/Phat-le2005/momo/actions/runs/45816991853/job/139899174134
  - `Web (Next.js lint, typecheck, build, unit tests)`: https://github.com/Phat-le2005/momo/actions/runs/45816991853/job/139899174168
  - `FinShop Commerce Web (Next.js lint, typecheck, unit tests, build)`: https://github.com/Phat-le2005/momo/actions/runs/45816991853/job/139899173711

---

## 4. Final Readiness Decisions

- **`ENGINEERING_MERGE_READINESS`**: **READY**
  - All audited P0/P1/P2 defects across Prompts 14.3–14.5 are resolved and covered by PostgreSQL regression tests.
  - All 5/5 GitHub Actions jobs pass, including the full 9-service Docker Compose runtime acceptance stack, real Tesseract `eng+vie` pixel OCR, persistent private receipt volume survival across container recreation, fail-closed worker fencing, exact Agent target authorization, and ambiguous recovery uncertainty protection.
  - Recommended action: open PR from `feat/open-world-autonomous-guardian` into `main` for human review and merge (not auto-merged).
- **`LIVE_AI_DEMO_READINESS`**: **`REAL_LLM_VERIFICATION_PENDING`**
  - No external `MODEL_API_KEY` was provisioned in this session; live-model Supervisor execution remains pending authorized model credentials.
- **`EXTERNAL_PAYMENT_READINESS`**: **INTENTIONALLY_DEFERRED (DISABLED)**
  - `COMMERCE_VNPAY_ENABLED=false` and `COMMERCE_VNPAY_REFUND_ENABLED=false` remain enforced; `LOCAL_MOCK` is active and verified end-to-end.
