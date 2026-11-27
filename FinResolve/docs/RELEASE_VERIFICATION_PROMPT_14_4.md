# FinResolve AI — Prompt 14.4 Final Deployment & Runtime Acceptance Report

## 1. Commit Lineage

- **Repository**: `Phat-le2005/momo`
- **Branch**: `feat/open-world-autonomous-guardian` (not merged to `main`)
- **Main branch**: `c45002c5204c46865ed6cf2e5df2026734b292cb`
- **Starting HEAD (Prompt 14.4)**: `86663453ff02a6190a36581c0c5e5584af00cdf9`
- **Fix commits delivered**:
  1. `674ec1d` — `fix(deploy): correct compose configuration and service startup`
  2. `26e158c` — `fix(agent): enforce exact proposal provenance and worker fencing`
  3. `238e853` — `fix(claims): persist private receipt storage and validate customer workflow`
  4. `06d508e` — `fix(baseline): deduplicate completed evaluation windows`
  5. `c64c29c` — `fix(recovery): retain uncertainty guards across manual escalation`
  6. `33ef535` — `fix(config): preserve existing environment during initialization`

---

## 2. Resolution of All Six Prompt 14.4 Defects

| Fix | Priority | Files & Functions Modified | Verification |
| --- | --- | --- | --- |
| **Fix 1: Docker Compose Least-Privilege Startup** | P0 | `FinResolve/infra/docker-compose.yml` (`api`, `worker`, `monitor-worker`, `commerce-api`, `commerce-worker`, `db`, `commerce-db`, `finshop_private_receipts`) | `api` & `monitor-worker` explicitly set `AUTONOMOUS_RECOVERY_ENABLED=false`; `worker` receives `AUTONOMOUS_RECOVERY_ENABLED=true` + strong signing/write keys; `commerce-worker` sets `COMMERCE_RECOVERY_WRITE_ENABLED=false`; DB & API ports bound to `127.0.0.1`. Validated via `test_prompt14_4_compose_startup.py`. |
| **Fix 2: Exact Proposal Provenance & Worker Fencing** | P1 | `FinResolve/backend/alembic/versions/20261125_0016_proposal_provenance_and_uncertainty.py`, `app/models/actions.py`, `app/ai_core/runtime.py`, `app/supervisor/service.py`, `app/monitoring/recovery_planner.py` | `ActionProposal.investigation_run_id` persisted on creation; Supervisor queries strictly by `investigation_run_id == run_id` with `FOR UPDATE SKIP LOCKED` atomic claim (`CLAIMING`), never by timestamp heuristic; validates `claim_token`, non-expired `locked_until`, and `anomaly_occurrence`. Verified in `test_prompt14_4_proposal_provenance.py`. |
| **Fix 3: Durable Private Receipt Storage & Customer Workflow** | P1 | `MiniStore/backend/Dockerfile`, `app/core/config.py`, `app/core/ocr.py`, `app/api/storefront.py`, `app/main.py`, `FinResolve/backend/app/monitoring/service.py` | Dedicated `/var/lib/finshop/private_receipts` (`0700`, `appuser:appuser`) mounted on `finshop_private_receipts` volume; `resolve_private_receipt_dir` blocks path traversal; DB commit failures unlink orphaned files; `/readyz` checks `receipt_storage_writable` and `ocr_ready`; `customer_pixel_contradiction` flagged. Verified in `test_prompt14_3_pixel_ocr.py` + `test_prompt14_1_receipt_upload.py`. |
| **Fix 4: Deduplicate Completed Behavioral Baseline Windows** | P1 | `MiniStore/backend/app/core/telemetry.py` (`snapshot_completed_fixed_window`), `FinResolve/backend/app/models/monitoring.py`, `FinResolve/backend/app/monitoring/service.py` | Added `completed_fixed_60s` completed bucket; persisted `CommerceSourceBinding.last_learned_window_id` & `last_learned_window_end` so repeated polls and Monitor restarts never double-learn; incomplete windows rejected for recovery; invalid observation gaps reset recovery streak; interval metrics separate completed orders/refunds. Verified in `test_prompt14_3_telemetry_windows.py`. |
| **Fix 5: Retain Uncertainty Guards Across Manual Escalation** | P1 | `FinResolve/backend/app/models/open_world.py`, `FinResolve/backend/app/connectors/commerce_write.py` (`execute_and_verify_commerce_recovery`, `reconcile_unknown_recovery_command`, `resolve_uncertain_recovery_command`) | Added `execution_certainty`, `uncertainty_resolved_at`, `uncertainty_resolved_by`; PostgreSQL `pg_advisory_xact_lock` serializes concurrent workers per target; `MANUAL_REVIEW` commands with `UNKNOWN`/`COMMAND_EXECUTION_UNKNOWN` continue blocking conflicting mutations until explicitly resolved; external business recovery after 404 sets `executed=False` and `verdict=BUSINESS_STATE_RECOVERED`. Verified in `test_prompt14_3_unknown_outcome_safety.py`. |
| **Fix 6: Safe & Idempotent Local Secret Initialization** | P2 | `FinResolve/infra/init_local_secrets.py` | Non-destructive `.env` parser preserves comments, `MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_API_KEY`, custom ports, and valid strong secrets; replaces weak/known-insecure defaults; explicit `--rotate` rotates managed secrets in sync (`COMMERCE_SERVICE_KEY == COMMERCE_SERVICE_API_KEY`); atomic temp-file write with `0o600`. Verified in `test_prompt14_4_compose_startup.py`. |

---

## 3. Automated Test & Quality Gate Results

- **MiniStore Backend (`MiniStore/backend`)**:
  - `alembic upgrade head` + `alembic check`: PASS (0 drift)
  - `ruff format --check .` (53 files) + `ruff check .`: PASS
  - `mypy app` (40 files, strict): PASS
  - `pytest -q`: **40/40 PASS** (includes real Tesseract `eng+vie` pixel OCR in GitHub Actions Ubuntu CI)
- **FinResolve Backend (`FinResolve/backend`)**:
  - `alembic upgrade head` (through `0016_proposal_provenance`) + `alembic check`: PASS (0 drift)
  - `ruff format --check .` (134 files) + `ruff check .`: PASS
  - `mypy app` (93 files, strict): PASS
  - `pytest -q`: **195/195 PASS**
- **Frontends (`FinResolve/frontend` + `MiniStore/frontend`)**:
  - `npm run lint`, `npm run typecheck`, unit tests, `npm run build`: verified in GitHub Actions CI.

---

## 4. Runtime Infrastructure Status & Honest Blockers

- **Docker Compose Live Stack (`docker compose up -d --build`)**: **NOT_EXECUTED_LOCALLY** — Docker CLI / Docker Desktop is not installed on the local Windows build host. Static least-privilege startup validation was verified via `test_prompt14_4_compose_startup.py` against `APP_ENV=docker`.
- **Real Pixel OCR (`tesseract-ocr` + `eng` + `vie`)**: **VERIFIED_IN_CI** (GitHub Actions Ubuntu runner executes `test_prompt14_3_pixel_ocr.py` and `test_prompt14_1_receipt_upload.py` with real `tesseract` binary on pure Pillow-rendered PNG pixels with zero `tEXt` metadata). Local Windows host lacks `tesseract` binary and honestly reports `ocr_ready: false`.
- **Live LLM Validation**: **REAL_LLM_VERIFICATION_PENDING** — no external model API key was supplied in this environment.
- **VNPAY Sandbox**: **INTENTIONALLY_DISABLED** (`COMMERCE_VNPAY_ENABLED=false`, `COMMERCE_VNPAY_REFUND_ENABLED=false`).

---

## 5. Reproduction Commands

```bash
# 1. Initialize strong 256-bit local secrets (non-destructive, idempotent)
python FinResolve/infra/init_local_secrets.py

# 2. Validate and start Docker Compose stack (requires Docker Desktop / Engine)
docker compose -f FinResolve/infra/docker-compose.yml config --quiet
docker compose -f FinResolve/infra/docker-compose.yml up -d --build
docker compose -f FinResolve/infra/docker-compose.yml ps

# 3. Verify readiness & OCR health
curl -fsS http://127.0.0.1:8000/readyz
curl -fsS http://127.0.0.1:8100/readyz
curl -fsS http://127.0.0.1:8100/api/v1/health/ocr
```

---

## 6. Independent Release Readiness Decisions

- **`ENGINEERING_MERGE_READINESS`**: **CONDITIONALLY_READY_PENDING_DOCKER_HOST_SMOKE** — All six P0/P1/P2 defects are fixed with full regression coverage (235/235 backend tests + 4/4 CI jobs green including real Ubuntu Tesseract OCR). Before final production/demo sign-off, run `docker compose up -d --build` on a host with Docker Engine to confirm container healthchecks and persistent volume survival across container recreation.
- **`LIVE_AI_DEMO_READINESS`**: **REAL_LLM_VERIFICATION_PENDING** — Requires executing a live-model Supervisor investigation with authorized `MODEL_API_KEY` credentials.
- **`EXTERNAL_PAYMENT_READINESS`**: **NOT_READY (INTENTIONALLY_DISABLED)** — VNPAY Sandbox credentials are unavailable; `LOCAL_MOCK` remains active and verified.
