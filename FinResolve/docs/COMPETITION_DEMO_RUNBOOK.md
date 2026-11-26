# FINRESOLVE AI — COMPETITION DEMO RUNBOOK (MOMO TECH CONTEST 2026)

## 1. Prerequisites & Environment Configuration

- Docker Desktop / Docker Engine with Compose v2
- Optional (for Live AI mode): an authorized Google Gemini API key (`MODEL_PROVIDER=gemini`, `MODEL_NAME=gemini-3.1-flash-lite`, `MODEL_API_KEY=<your-key>`)
- **Security rule**: Never commit `.env` or secret keys. VNPAY remains intentionally disabled (`LOCAL_MOCK` only).

### Step 1: Generate ephemeral 256-bit local secrets
```bash
python FinResolve/infra/init_local_secrets.py
```
If demonstrating **Live AI mode**, append your authorized model credentials to `FinResolve/infra/.env`:
```env
MODEL_PROVIDER=gemini
MODEL_NAME=gemini-3.1-flash-lite
MODEL_API_KEY=<YOUR_AUTHORIZED_KEY>
```
*(If `MODEL_API_KEY` is omitted, FinResolve automatically runs in transparent `DEMO_RULES` deterministic mode and displays `Chế độ DEMO_RULES` in the header.)*

---

## 2. Starting & Verifying the 9-Service Stack

```bash
docker compose --env-file FinResolve/infra/.env -f FinResolve/infra/docker-compose.yml up -d --build
```

### Health Checks (Wait ~15–20 seconds)
- FinResolve API readiness: `GET http://localhost:8000/readyz` → `200 OK`
- FinShop Commerce API readiness: `GET http://localhost:8100/readyz` → `200 OK` (`receipt_storage_writable: true`)
- Tesseract Pixel OCR readiness: `GET http://localhost:8100/api/v1/health/ocr` → `ocr_ready: true`, `languages: ["eng", "vie"]`
- FinResolve Unified Experience UI: `http://localhost:3000` (Landing Page) → `http://localhost:3000/experience` (Guardian Command Center)
- FinShop LIVE Storefront UI: `http://localhost:3100`

---

## 3. Safe Shared Sandbox Ownership Procedure

The competition stack operates a **controlled single-workspace shared sandbox (`SHARED_DEMO_SANDBOX`)**:
1. Open `http://localhost:3000` and click **Bắt đầu trải nghiệm** → navigates to `/experience` and acquires `workspace_role: CONTROLLER`.
2. Reloading the tab restores the same controller session via `sessionStorage` + `X-Demo-Session` header.
3. Any additional browser guest receives `READ_ONLY_PREVIEW` (zero private business data, `403` on scan/reset).
4. To hand off control cleanly to another presenter browser, call `POST /api/v1/experience/release` (allowed only when zero unresolved uncertain recovery commands exist).

---

## 4. Live Demonstration Script

### A. Healthy Commerce Baseline
1. Switch to **Khách hàng** tab in `/experience` → click **Mở FinShop LIVE ↗** (`http://localhost:3100`).
2. Add any product to cart and checkout with `LOCAL_MOCK` (`auto_sync_order=true`).
3. Order transitions to `CONFIRMED`; return to **Guardian** tab to show healthy telemetry and zero unnecessary payment-sync alerts.

### B. Autonomous Payment-Sync Detection & Recovery
1. Create a `LOCAL_MOCK` order where payment succeeds but order sync is interrupted (`auto_sync_order=false`).
2. Wait for the Continuous Monitor (or click **Quét ngay**).
3. Observe the live Vietnamese timeline in **Hoạt động AI Guardian trực tiếp**:
   - Anomaly detected (`D01`) → Case created → AI calls `get_order` + `get_payment_record` → proposes `ORDER_SYNC` with exact `expected_order_version` → Policy Engine signs HMAC-SHA256 → MiniStore verifies signature and confirms order → Independent Verifier confirms `VERIFIED_RECOVERY`.
4. Expand **Xem Technical Trace ▼** to show verifiable IDs (`run_id`, `proposal_id`, `command_id`, `request_hash`).

### C. Suspicious Receipt Investigation (Real Pixel OCR)
1. On FinShop, open an order and upload a synthetic payment receipt image containing contradictory transaction details.
2. Tesseract extracts text directly from PNG pixels (`PIXEL_OCR_EXTRACTED`) and stores the file in the private persistent volume.
3. Guardian compares extracted claims against authoritative payment records and NEVER treats an uploaded image as payment confirmation.

### D. Safe Escalation & Reset
- For unsupported or contradictory incidents, Guardian escalates safely without unauthorized mutation.
- To start a clean dashboard epoch between judge demos, click **Reset phiên** → confirm in the modal. (Unresolved uncertain mutation guards are intentionally preserved; external Commerce history is not destroyed.)

---

## 5. Offline / Provider-Outage Contingency

If the external Gemini API is unreachable or rate-limited during the presentation:
1. Remove `MODEL_API_KEY` from `FinResolve/infra/.env` and restart `finresolve-api` and `finresolve-supervisor`.
2. The UI header explicitly displays **Chế độ DEMO_RULES** (never mislabeled as Live AI).
3. All monitoring, OCR, Policy Engine HMAC signing, and recovery verification continue functioning offline.

---

## 6. Clean Shutdown

```bash
docker compose --env-file FinResolve/infra/.env -f FinResolve/infra/docker-compose.yml down -v
```
