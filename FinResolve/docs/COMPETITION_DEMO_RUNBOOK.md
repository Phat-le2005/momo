# FINRESOLVE AI — COMPETITION DEMO RUNBOOK (MOMO TECH CONTEST 2026)

## 1. Prerequisites & Environment Configuration

- Docker Desktop / Docker Engine with Compose v2
- Optional (for Live AI mode): an authorized Google Gemini API key (`MODEL_PROVIDER=gemini`, `MODEL_NAME=gemini-3.1-flash-lite`, `MODEL_API_KEY=<your-key>`)
- **Security rule**: Never commit `.env` or secret keys. VNPAY remains intentionally disabled (`LOCAL_MOCK` only).

### Step 1: Generate ephemeral 256-bit local secrets (including `EXPERIENCE_OPERATOR_TOKEN`)
```bash
python FinResolve/infra/init_local_secrets.py
```
To enable **Live AI mode**, append your authorized model credentials to `FinResolve/infra/.env`:
```env
MODEL_PROVIDER=gemini
MODEL_NAME=gemini-3.1-flash-lite
MODEL_API_KEY=<YOUR_AUTHORIZED_KEY>
```
*(If `MODEL_API_KEY` is omitted, FinResolve runs in transparent `DEMO_RULES` deterministic mode and displays `Chế độ DEMO_RULES` in the Command Center header.)*

---

## 2. Starting & Verifying the 9-Service Stack

Compose services: `db`, `api`, `worker`, `monitor-worker`, `web`, `commerce-db`, `commerce-api`, `commerce-worker`, `commerce-web`.

```bash
docker compose --env-file FinResolve/infra/.env -f FinResolve/infra/docker-compose.yml up -d --build
```

### Readiness Checks (Wait ~15–20 seconds)
- FinResolve API readiness: `GET http://localhost:8000/readyz` → `200 OK`
- FinShop Commerce API readiness: `GET http://localhost:8100/readyz` → `200 OK` (`receipt_storage_writable: true`)
- Tesseract Pixel OCR readiness: `GET http://localhost:8100/api/v1/health/ocr` → `ocr_ready: true`, `languages: ["eng", "vie"]`
- FinResolve Unified Experience UI: `http://localhost:3000` → `http://localhost:3000/experience`
- FinShop LIVE Storefront UI: `http://localhost:3100`

---

## 3. Secure Shared Sandbox Ownership Procedure

The competition stack operates a **controlled single-workspace shared sandbox (`SHARED_DEMO_SANDBOX`)**:
1. Anonymous visitors without `EXPERIENCE_OPERATOR_TOKEN` automatically receive `workspace_role: READ_ONLY_PREVIEW` (zero private business data, `403` on scan/reset).
2. An authorized operator claims `CONTROLLER` via `POST /api/v1/experience/bootstrap` with header `X-Operator-Token: <EXPERIENCE_OPERATOR_TOKEN>` (read from `FinResolve/infra/.env` on the presenter host — never embedded in frontend bundles).
3. Reloading restores the same controller session via `X-Demo-Session`.
4. To hand off control cleanly to another presenter session, call `POST /api/v1/experience/release` (blocked with `409 CONFLICT` if any unresolved `UNKNOWN_OUTCOME` recovery command exists).

---

## 4. Live Demonstration Workflow

### A. Healthy Commerce Baseline
1. Open FinShop LIVE (`http://localhost:3100`), add a product to cart, and complete `LOCAL_MOCK` checkout (`auto_sync_order=true`).
2. Confirm order reaches `CONFIRMED` with zero payment-sync false alerts.

### B. Autonomous Payment-Sync Detection & Recovery
1. Create a `LOCAL_MOCK` order with normal sync interrupted (`auto_sync_order=false`).
2. Observe the live Vietnamese timeline in **Hoạt động AI Guardian trực tiếp**:
   - Continuous Monitor detects `D01` → Case created → AI executes `get_order` + `get_payment_record` → proposes `ORDER_SYNC` with exact `expected_order_version` → Policy Engine signs HMAC-SHA256 → MiniStore verifies signature and confirms order → Independent Verifier confirms `VERIFIED_RECOVERY`.
3. Expand **Xem Technical Trace ▼** to show verifiable `run_id`, `proposal_id`, `command_id`, and `request_hash`.

### C. Suspicious Receipt Investigation (Real Pixel OCR)
1. Upload a synthetic receipt PNG containing contradictory transaction details via the FinShop customer order page.
2. Tesseract extracts text directly from PNG pixels (`PIXEL_OCR_EXTRACTED`) and stores the receipt in the private persistent volume (`finshop_private_receipts`).
3. Guardian compares claims against authoritative payment records and never treats an uploaded image as payment confirmation.

### D. Safe Escalation & Dashboard Reset
- Unsupported or incomplete-evidence incidents escalate safely without unauthorized mutation.
- To start a clean dashboard epoch between judge demos, click **Reset phiên** → confirm. Unresolved uncertainty guards remain protected; external Commerce history is preserved.

---

## 5. Offline / Provider-Outage Contingency

If the external Gemini API is unreachable or rate-limited during the presentation:
1. Remove or blank `MODEL_API_KEY` in `FinResolve/infra/.env`.
2. **Recreate** the `api` and `worker` containers so Compose reloads the updated environment variables:
```bash
docker compose --env-file FinResolve/infra/.env -f FinResolve/infra/docker-compose.yml up -d --force-recreate api worker
```
3. Verify the header explicitly displays **Chế độ DEMO_RULES** (never mislabeled as Live AI).

---

## 6. Safe Non-Destructive Shutdown & Opt-In Cleanup

### Standard Non-Destructive Shutdown (Preserves PostgreSQL & Private Receipt Volumes)
```bash
docker compose --env-file FinResolve/infra/.env -f FinResolve/infra/docker-compose.yml down
```

### Opt-In Destructive Cleanup (DELETES ALL VOLUMES & STORED RECEIPTS)
Only run when you explicitly intend to wipe all demo databases and private receipt storage:
```bash
docker compose --env-file FinResolve/infra/.env -f FinResolve/infra/docker-compose.yml down -v
```
