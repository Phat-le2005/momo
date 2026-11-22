# PROMPT 01 — DATA CONTRACT, CASES & EVIDENCE (Backend owner A)

Read `AGENTS.md`, `docs/PRODUCT_CONTRACT.md`, `docs/QA_MATRIX.md`, and current code. Implement the smallest fully working persistence/API slice. Make real edits; do not merely propose changes.

## Scope
- SQLAlchemy domain models + Alembic migration for `DemoSession`, `Case`, `Evidence`, `CaseEvent`, `ActionProposal`, `Approval`, `ActionExecution`, `Verification`, `RiskSignal`; enough simulator Order/Payment/Refund tables for later prompts. Distinct payment/order/refund state. Index by session/tenant and case ID. Version field for optimistic concurrency.
- `POST /api/v1/demo/sessions`: session token/ID, TTL and quota. Every Case and data retrieval is scoped to actual server-validated session; no trusting arbitrary tenant from request body.
- `POST /api/v1/cases` Vietnamese free-text intake (as CLAIM, never authenticated payment), `GET /api/v1/cases/{id}`, `GET /api/v1/cases/{id}/events`, `GET /api/v1/cases/{id}/status`.
- Explicit Case state enum + validated transition function and append-only CaseEvent. No RESOLVED transition unless accompanied by a successful verification record.
- Seed fixture: paid provider record/order pending, duplicate refund candidate, unrelated delayed order. All fake merchants/order data with visible `MOCK` source marker.
- Pydantic response schemas and generated OpenAPI. Provide sample `curl` in README, document payload contracts for web/AI owners.

## Acceptance
- Alembic upgrade from empty Postgres works.
- Create guest session → create Case → read Case and events works.
- Wrong session cannot access Case, even by guessing its ID.
- Attempted state transition NEW -> RESOLVED without verification fails.
- Test duplicate client retry/dedupe if request idempotency key exists; otherwise document API level.
- No model calls yet.

## Tests
Pytest for state machine, session isolation, evidence provenance, CRUD. Run actual tests and report output.

## Deliver
Changed file list, migration name, tested curl examples, OpenAPI changes, test status. Do not implement planner or frontend now.
