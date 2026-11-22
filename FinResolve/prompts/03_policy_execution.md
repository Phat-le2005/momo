# PROMPT 03 — POLICY / APPROVAL / ACTION / VERIFICATION (Backend owner A)

Implement FinResolve's most safety-critical feature using `AGENTS.md`, `docs/PRODUCT_CONTRACT.md` and current P01/P02 code. This is an implementation task; modify files, run tests and report real outputs.

## Features
1. `BusinessActionContract` for `MARK_ORDER_PAYMENT_CONFIRMED` / safe `SYNC_PAID_ORDER` and `REFUND_PROPOSAL`, including input schema, authorized evidence, preconditions, required role, risk level, side effects and expected postconditions.
2. `PolicyEngine`: check session/tenant, tool/role, amount, payment verified by authoritative record, order/refund state and current version; no privilege from model or untrusted tool data.
3. `Approval`: digest bound to action type, exact order, amount/params, evidence IDs, policy version, approver identity and expiry. Demo manager approval must not be a publicly unauthenticated generic override; use scoped demo role with strict mock-only effects.
4. `ActionExecutor`: only policy-gated writes, DB transaction, optimistic lock, unique/idempotency key, safe outcome state. Side-effect timeout produces UNKNOWN, NEVER blind duplicate retry.
5. `Verifier`: independently read authoritative source records after effects. Only then create `Verification(PASS)` and change Case to RESOLVED. A tool returning 200 does not constitute PASS.
6. APIs: `POST /api/v1/actions/{proposal_id}/approve`, `POST /api/v1/actions/{proposal_id}/execute` if safe from role context; add status or trace endpoints as needed. Align contract docs.

## Required integration
Make `payment_sync` Case E2E actually mutate the simulated order after checking verified payment, then inspect DB and response to verify updated state. Keep refund proposal-only until Risk Engine integration. Ensure no direct LLM write path.

## Test gate
T01, T02, T04, T06, T11, T12, T13, T20 from `docs/QA_MATRIX.md`. Include concurrent attempts and approval replay. Report observed test command output and DB effect.

## Do not
No production MoMo call, no personal banking data, no completed/verified refund claims without actual simulator/provider observation. STOP after this slice.
