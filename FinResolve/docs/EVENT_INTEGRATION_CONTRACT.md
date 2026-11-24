# FinResolve AI — Normalized Event Integration Contract (Mini Store & VNPAY Ready)

## Architecture Boundary

```text
Mini Store Frontend
        ↓
Commerce Backend  ↔  VNPAY Sandbox
        ↓
Commerce Events / Payment Observations
        ↓
POST /api/v1/events/ingest (202 Accepted)
        ↓
Durable PostgreSQL Job Queue (supervisor_jobs)
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

## Canonical Event Contract (`NormalizedBusinessEvent`)

| Field | Type | Required | Description |
| :--- | :--- | :---: | :--- |
| `event_id` | `string` (3–128) | Yes | Globally unique source event identifier for deduplication & audit |
| `event_type` | `string` (3–64) | Yes | e.g. `PAYMENT_SYNC_MISMATCH`, `REFUND_REQUESTED`, `VNPAY_IPN_RECEIVED`, `ORDER_STALLED` |
| `source` | `string` (2–64) | Yes | Origin system (e.g. `MINI_STORE_COMMERCE`, `VNPAY_SANDBOX_IPN`, `CUSTOMER_MESSAGE`) |
| `source_mode` | `string` | Yes | Strictly `"MOCK"` in Milestone 09.1 (future: `"VNPAY_SANDBOX"` behind explicit adapter) |
| `schema_version` | `string` | Yes | Contract version, currently `"1.0"` |
| `merchant_id` | `string` | Yes | Scoped merchant identifier (default `"MERCHANT_MOCK_01"`) |
| `session_id` | `string \| null` | No | Optional; if supplied must match authenticated `X-Demo-Session` (403 otherwise) |
| `world_epoch` | `int \| null` | No | Optional world generation counter; server binds authoritative `SupervisorState.world_epoch` |
| `order_id` | `string \| null` | No | Target commerce order identifier |
| `payment_id` | `string \| null` | No | Target payment transaction identifier |
| `correlation_id` | `string \| null` | No | Distributed trace / batch correlation ID |
| `occurred_at` | `ISO-8601 datetime` | Yes | Source occurrence timestamp (UTC) |
| `received_at` | `ISO-8601 datetime` | Yes | Ingestion receipt timestamp (UTC) |
| `payload` | `object` | Yes | Sanitized event payload (never hidden Oracle truth) |
| `provenance` | `object` | Yes | Signature verification metadata, adapter version, source reference |

## Response (`202 Accepted`)

```json
{
  "event_id": "EVT-STORE-20261124-001",
  "session_id": "sess_...",
  "world_epoch": 1,
  "case_id": "CASE-...",
  "job_id": "JOB-...",
  "job_status": "QUEUED",
  "source_mode": "MOCK"
}
```

## Safety Invariants
- User-submitted sources (`CUSTOMER_MESSAGE`, `RECEIPT_UPLOAD`, `USER_CLAIM`) are stored strictly as `Evidence(kind="CLAIM")` and never promoted to verified payment truth.
- Ingestion returns `202 Accepted` immediately after atomic database commit; Agent investigation runs asynchronously in the dedicated Supervisor worker.
- Financial actions still require deterministic `PolicyEngine` validation, explicit human `Approval`, idempotent `ActionExecutor`, and independent `Verifier` read-back before `RESOLVED`.
