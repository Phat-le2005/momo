# PROMPT 08 — END-TO-END INTEGRATION AND HARDENING (shared integration owner)

Inspect current repo state; do not change architecture or add major features. Integrate merged backend, AI, simulator and frontend. Read `docs/QA_MATRIX.md` and `docs/PRODUCT_CONTRACT.md`.

## Work
- Verify API path/response contracts end-to-end; fix mismatch at one source of truth and update docs.
- Add automated end-to-end smoke where feasible: create session; start 3 scenarios; poll to completion or safety review; inspect CaseEvent trace, DB state and verification; test wrong-session access.
- Enforce guest quota/rate limits, max request/attachment sizes, session expiry; restrict CORS; upload type validation if image enabled; no secrets in logs/client.
- Model adapter misconfiguration, invalid JSON, provider timeout, DB connection errors and worker restarts produce clear `UNKNOWN`/failure states rather than 500s/hangs or fabricated resolution.
- Check idempotency on repeated action, webhook and refund, and optimistic locking on stale state.
- Instrument route latency, model call count/tokens when available, and key outcomes, without logging sensitive or raw image data.
- Run the app locally in normal and no-key DEMO_RULES modes. Show UI label matching actual model mode.
- Fix lint/type errors; ensure Docker Compose start and migration apply from clean DB.

## Test plan
Run every P0 QA test T01–T24 that is implemented and record which are not. Critical T02,T04,T07,T09,T11,T13,T16,T18,T20 must pass; do not deploy with a safety breach. Run a 50–100 order mini-live if present.

## Completion report
Actual test commands/results, security findings, bugfixes, remaining blockers, release readiness checklist with evidence. Do not claim complete if tests have not run.
