# FINRESOLVE AI — FINAL COMPETITION RELEASE BASELINE (`v1.0.0-competition-demo`)

- **Release Status**: `FEATURE_FREEZE_ACTIVE` (Controlled Competition Demonstration Baseline — NOT a production-grade financial or multi-tenant release)
- **Repository**: `https://github.com/Phat-le2005/momo`
- **Release Tag**: `v1.0.0-competition-demo` (`FinResolve AI v1.0.0 — Competition Demo`)
- **Previous `main` Commit SHA**: `c45002c5204c46865ed6cf2e5df2026734b292cb`
- **Pre-Merge Backup Reference**: `backup/main-before-finresolve-release` (points to `c45002c5204c46865ed6cf2e5df2026734b292cb`)
- **Merged Development Branch**: `feat/open-world-autonomous-guardian` (`806081c03d3ae1ed90d3076e084445d5491078fd`)
- **History-Preserving Merge Commit on `main`**: `5f68f8831e4712a155b49f760f6d33bcf762c1e1` (`--no-ff`, preserving full branch history)
- **Pull Request / Merge Method**: History-preserving `--no-ff` merge commit (`5f68f88`) executed directly after 5/5 pre-merge CI passed (`46006544105`, `46006544049`) because `gh` CLI is not installed in the release environment; full release summary preserved in merge commit message.
- **VNPAY Status**: Intentionally disabled (`LOCAL_MOCK` sandbox operations only).

---

## 1. Verified Post-Merge CI & Runtime Acceptance

All **5/5** GitHub Actions CI jobs passed **GREEN** on `main` (`5f68f8831e4712a155b49f760f6d33bcf762c1e1`):

| Workflow / Job | Status | Verified URL |
| --- | :---: | --- |
| **FinResolve CI — API** (PostgreSQL 17, Alembic, Ruff, Mypy, Pytest `231/231`) | **PASS** | https://github.com/Phat-le2005/momo/actions/runs/46007172331 |
| **FinResolve CI — Web** (ESLint, TypeScript, Next.js build, `13/13` unit tests) | **PASS** | https://github.com/Phat-le2005/momo/actions/runs/46007172331 |
| **FinResolve CI — FinShop Commerce API** (PostgreSQL 17, Alembic, Ruff, Mypy, Pytest `40/40`) | **PASS** | https://github.com/Phat-le2005/momo/actions/runs/46007172331 |
| **FinResolve CI — FinShop Commerce Web** (ESLint, TypeScript, unit tests, build) | **PASS** | https://github.com/Phat-le2005/momo/actions/runs/46007172331 |
| **Runtime Acceptance (Docker Compose)** (9 services + Real Tesseract OCR + Persistent Volume + Playwright Chromium `3/3` E2E) | **PASS** | https://github.com/Phat-le2005/momo/actions/runs/46007172296 |

---

## 2. Selected Gemini Models & Verified Live AI Capabilities

Full empirical model discovery and evaluation are documented in `FinResolve/docs/GEMINI_MODEL_EVALUATION_PROMPT_14_10.md`:

- **Provider**: `MODEL_PROVIDER=gemini`
- **Primary Model**: `MODEL_NAME=gemini-3.5-flash-lite`
- **Fallback Model**: `MODEL_FALLBACK_NAME=gemini-3.1-flash-lite`
- **Retry Policy**: `MODEL_MAX_RETRIES=2` (bounded transient retry/backoff for HTTP `429/500/502/503/504` respecting `Retry-After` + jitter; passed explicitly to both `api` and `worker` in `FinResolve/infra/docker-compose.yml`).
- **Verified Live Autonomous Payment-Sync Recovery** (`FinResolve/docs/BENCHMARK_PROMPT_14_10.json`):
  - 3 consecutive end-to-end live Gemini runs succeeded (`RUN-D1EA5D1035`, `RUN-3DD467D451`, `RUN-479CB81C87`).
  - Final benchmark run (`RUN-479CB81C87`): `TP=1, FP=0, TN=3, FN=0` (`Precision=1.0, Recall=1.0, FPR=0.0, F1=1.0`), `tool_calls=["get_order", "get_payment_record"]`, `tokens_used=9,819`, `proposal_id=PROP-8228CCAC6B`, `command_id=RCMD-E7A7162A31`, `execution_certainty=COMMAND_EXECUTION_VERIFIED`, order `ORD-B7B00DCD27` transitioned to `CONFIRMED` with `VERIFIED_RECOVERY` in `11.63s` end-to-end (`unauthorized_mutations=0`, `duplicate_mutations=0`, independently measured).

---

## 3. Deployment & Operator Access Instructions

Full operational steps are maintained in `FinResolve/docs/COMPETITION_DEMO_RUNBOOK.md`. Quick start:

1. Generate ephemeral 256-bit local secrets (including `EXPERIENCE_OPERATOR_TOKEN`):
   ```bash
   python FinResolve/infra/init_local_secrets.py
   ```
2. Optional — append authorized Gemini credentials to `FinResolve/infra/.env` (never commit `.env`):
   ```env
   MODEL_PROVIDER=gemini
   MODEL_NAME=gemini-3.5-flash-lite
   MODEL_FALLBACK_NAME=gemini-3.1-flash-lite
   MODEL_MAX_RETRIES=2
   MODEL_API_KEY=<YOUR_AUTHORIZED_KEY>
   ```
3. Start the 9-service stack:
   ```bash
   docker compose --env-file FinResolve/infra/.env -f FinResolve/infra/docker-compose.yml up -d --build
   ```
4. **Operator Access**:
   - Open `http://localhost:3000` → click **Trải nghiệm ngay** → `/experience`.
   - Anonymous visitors receive `READ_ONLY_PREVIEW` (mutation buttons disabled; `403 CONTROLLER_AUTHORIZATION_REQUIRED` on `/scan`, `/reset`, `/release`).
   - Presenter clicks **Kích hoạt Operator** and enters `EXPERIENCE_OPERATOR_TOKEN` from `FinResolve/infra/.env` (sent only to backend; never stored in browser storage).

---

## 4. Remaining Known Acceptance Limitations (Honest Scope)

- **Controlled Internal Demonstration Release**: Restricted to authorized `LOCAL_MOCK` sandbox operations; NOT certified for production financial processing or multi-tenant deployment.
- **Single-Controller Shared Sandbox**: Only one authorized Operator workspace controls the default FinShop sandbox at a time (`SHARED_DEMO_SANDBOX`); other visitors are isolated in `READ_ONLY_PREVIEW`.
- **Partial Open-World Acceptance**: Healthy Commerce (A), Live Autonomous Payment-Sync Recovery (D), Dashboard Reset & Auth Isolation (G), Real Tesseract Pixel OCR, and Playwright Chromium E2E are verified; full large-N live LLM sweeps across all seven open-world experiment categories remain partial.
- **Small-Sample Benchmark**: `BENCHMARK_PROMPT_14_10.json` uses `n=4` (`3` healthy + `1` positive `D01`); larger multi-category benchmarks belong to the post-freeze evaluation phase.
- **External Payment Gateways Deferred**: VNPAY and real financial mutations remain intentionally disabled.

---

## 5. Rollback Procedure

To restore `main` cleanly without rewriting Git history:

```bash
# Option A: Safe revert of the merge commit on main (recommended, preserves history)
git checkout main
git pull origin main
git revert -m 1 5f68f8831e4712a155b49f760f6d33bcf762c1e1
git push origin main

# Option B: Inspect or branch from the pre-merge backup reference
git checkout backup/main-before-finresolve-release
```

---

## 6. Feature Freeze Policy (`FEATURE_FREEZE_ACTIVE`)

Effective immediately on `main` (`v1.0.0-competition-demo`):
- **Frozen**: No new product features, architecture redesigns, Agent framework replacements, new payment integrations, or unnecessary contract changes.
- **Permitted (via separate branches & reviewed PRs only)**: Critical security fixes, correctness/demo-blocking bug fixes, additional acceptance tests, benchmark improvements, documentation, and competition presentation preparation.
