# FinResolve AI — Normalized Event Integration Contract (Mini Store & VNPAY Ready)

## Architecture Boundary

```text
Mini Store Frontend
        ↓
Commerce Backend  ↔  VNPAY Sandbox
        ↓
Commerce Events / Payment Observations
        ↓
POST /api/v1/events/ingest (202 Accepted / 200 Idempotent Replay / 409 Conflict)
        ↓
Durable Event Inbox (`normalized_events`)
        ├── Normal Business Events (ORDER_CREATED, ORDER_UPDATED, PAYMENT_INITIATED, INVENTORY_UPDATED)
        │     → Stored as observations only (0 Cases, 0 Jobs, 0 LLM calls)
        └── Anomaly / Investigation Triggers (PAYMENT_SYNC_MISMATCH, REFUND_ANOMALY, DUPLICATE_REFUND_DETECTED, ORDER_STALLED, PAYMENT_DISPUTE_REPORTED)
              → Create 1 Case + Enqueue 1 Durable PostgreSQL Job (`supervisor_jobs`)
              ↓
Dedicated Standalone Supervisor Worker (`python -m app.supervisor.worker`)
        ↓
BoundedAgentRuntime / IncidentCorrelator / TaskforceManager
        ↓
Policy -> Explicit Human Approval -> Automatic Execution -> Independent Verifier
```

Supervisor job submission is completely independent of the Challenge Lab. Future Mini Store
and VNPAY Sandbox adapters submit events to `POST /api/v1/events/ingest` using the canonical
`NormalizedBusinessEvent` schema below.

## Authentication & Server-Derived Provenance

- **Session Auth (required)**: `X-Demo-Session: <session_token>`
- **Service Ingestion Auth (optional, server-verified)**: `X-Service-Token: svc_<SOURCE>.<MERCHANT_ID>.<HMAC_SHA256>`
  - Allowed service sources: `MINI_STORE_COMMERCE`, `VNPAY_SANDBOX_IPN`, `ORDER_EVENT_LOG_DB`.
  - When valid: server sets `verified_source` from the authenticated service principal, verifies `event.merchant_id == principal.merchant_id` (403 on mismatch), and creates `Evidence(kind="OBSERVED_FACT")` (still never auto-marking payment verified without ledger reconciliation).
  - When absent: server sets `verified_source = "UNTRUSTED_GUEST_CLIENT"` and creates `Evidence(kind="CLAIM")` strictly — regardless of caller-provided `source` or `provenance.signature_verified`.
  - When invalid/forged/cross-session: rejected with `403 FORBIDDEN`.

## Canonical Event Contract (`NormalizedBusinessEvent`)

| Field | Type | Required | Description |
| :--- | :--- | :---: | :--- |
| `event_id` | `string` (3–128) | Yes | Unique per `(session_id, world_epoch, merchant_id, event_id)` in `normalized_events` |
| `event_type` | `string` (3–64) | Yes | Allowlisted normal or anomaly event type |
| `source` | `string` (2–64) | Yes | Claimed origin system (recorded as `claimed_source`; server derives `verified_source`) |
| `source_mode` | `string` | Yes | Strictly `"MOCK"` in Milestone 09.1.1 |
| `schema_version` | `string` | Yes | Strictly `"1.0"` |
| `merchant_id` | `string` | Yes | Scoped merchant identifier (default `"MERCHANT_MOCK_01"`) |
| `session_id` | `string \| null` | No | Optional; if supplied must match authenticated `X-Demo-Session` (403 otherwise) |
| `world_epoch` | `int \| null` | No | Optional; if supplied must match active `SupervisorState.world_epoch` (409 otherwise) |
| `order_id` | `string \| null` | No | Target commerce order identifier |
| `payment_id` | `string \| null` | No | Target payment transaction identifier |
| `correlation_id` | `string \| null` | No | Distributed trace / batch correlation ID |
| `occurred_at` | `ISO-8601 datetime` | Yes | Source occurrence timestamp (UTC) |
| `received_at` | `ISO-8601 datetime \| null` | No | Always overwritten server-side on receipt |
| `payload` | `object` | Yes | Bounded (`<= 32KB`) sanitized payload, stored nested under `event_payload` (cannot overwrite canonical metadata) |
| `provenance` | `object` | Yes | Recorded under `provenance.caller_provenance`; never trusted as signature proof |

## Idempotency & Conflict Semantics

- Same `(session_id, world_epoch, merchant_id, event_id)` + identical canonical `payload_hash` → **`200 OK`** (`idempotent_replay=true`, 0 duplicate Cases/jobs).
- Same `(session_id, world_epoch, merchant_id, event_id)` + different `payload_hash` → **`409 CONFLICT`** (`EVENT_ID_PAYLOAD_CONFLICT`).
- Stale `world_epoch` → **`409 CONFLICT`** (`STALE_WORLD_EPOCH`).
- Session Case quota exceeded on anomaly trigger → **`429 TOO MANY REQUESTS`** (enforced atomically under `DemoSession FOR UPDATE`).

## Response (`202 Accepted` or `200 OK` on Replay)

```json
{
  "event_id": "EVT-STORE-20261124-001",
  "session_id": "sess_...",
  "world_epoch": 1,
  "merchant_id": "MERCHANT_MOCK_01",
  "verified_source": "MINI_STORE_COMMERCE",
  "event_type": "PAYMENT_SYNC_MISMATCH",
  "triggered_investigation": true,
  "idempotent_replay": false,
  "case_id": "CASE-...",
  "job_id": "JOB-...",
  "job_status": "QUEUED",
  "source_mode": "MOCK"
}
```
