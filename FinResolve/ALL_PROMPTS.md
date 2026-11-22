# FINRESOLVE — ALL CODING PROMPTS

Read `START_HERE.md`, `AGENTS.md`, `docs/PRODUCT_OVERVIEW.md` and the frozen contract before running these prompts. Implement ONE prompt at a time.


---

<!-- SOURCE: prompts/00_bootstrap.md -->

# PROMPT 00 — INITIAL REPO BOOTSTRAP (run FIRST, one coding agent)

You are the tech lead implementing the FinResolve AI MVP. This is a REAL code-editing task, not a conceptual response. First read `AGENTS.md`, `docs/PRODUCT_OVERVIEW.md`, `docs/PRODUCT_CONTRACT.md`, `docs/DELIVERY_PLAN.md`, `docs/QA_MATRIX.md` and inspect the workspace. Do not ask for broad clarification; choose simple defaults and document assumptions.

## Task
Create a runnable **modular-monolith monorepo skeleton**, preserving the product/docs files already in this repo:
- `apps/web`: Next.js + TypeScript + Tailwind, initial Vietnamese `/demo` landing page, placeholders for `/cases`, `/risks`, `/live`, a visible `MOCK` badge and API health status.
- `services/api`: Python 3.12+ FastAPI/Pydantic v2 with `GET /healthz`, `/api/v1` router, structured error envelope, CORS limited to configured web origin, `.env.example`, SQLAlchemy setup and Alembic initialized (no elaborate domain features yet).
- `infra/docker-compose.yml`: PostgreSQL + backend + frontend; use actual working Dockerfile paths; provide named volume and healthcheck.
- Root `.gitignore`, `Makefile` or `scripts/dev.sh`, `README.md` with step-by-step local startup, model/mock modes and tests, safe sample config.
- `services/api/tests/test_health.py`, initial lint/typecheck/pytest config, minimal frontend build checks. If packages cannot install in this environment, state why instead of inventing results.

## Constraints
No LangChain/LangGraph/CrewAI/AutoGen or prebuilt orchestration framework. Do not call financial production endpoints. Do not create fake payment success in UI. Do not put secrets in code or committed env files. Reuse existing files if the repository already has a scaffold.

## Verification
Run the available installation/build/test commands. Verify `/healthz`, frontend render and Docker Compose config, where runtime allows. Do not claim you ran the UI if not actually started.

## Deliver response
1. Files created/changed.
2. Exact startup and test commands.
3. Real build/test results, and any blockers.
4. Which contract is now ready for prompt 01.
STOP; do not implement future features in this task.


---

<!-- SOURCE: prompts/01_database_cases.md -->

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


---

<!-- SOURCE: prompts/02_ai_core.md -->

# PROMPT 02 — SELF-BUILT BOUNDED AGENTIC CORE (AI owner B)

Read `AGENTS.md`, `docs/PRODUCT_CONTRACT.md`, `docs/QA_MATRIX.md` and P01 API schema. Implement a REAL bounded agentic investigation loop, not hard-coded success scripts and not a wrapper around an agent framework. Work only inside `services/api/app/ai_core`, `services/api/app/connectors` READ-only tool adapters (coordinate ownership), tests, and corresponding API route registration.

## Design
- Define `ModelAdapter` protocol. Implement configured LLM provider via its supported SDK with env `MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_API_KEY`, timeout and token/step budget. Never hardcode model IDs as trusted forever: use environment variables and document provider setup.
- `DeterministicDemoAdapter` for no-key mode with visible `mode=DEMO_RULES` label; this is NOT AI and cannot be described as AI output. Real adapter has `mode=MODEL_API`.
- Pydantic schema for Plan `{case_id, goal, known_evidence_refs, unknowns, hypotheses[], next_step(kind,tool_name,args), decision_summary}`. Use JSON schema/structured output if supported; reject invalid JSON, unknown tools and arbitrary URLs.
- `ContextBuilder` retrieves relevant Case, authoritative observations and policy only. Untrusted customer/tool content is DATA, never system instructions.
- Tool registry with scoped READ tools `get_order`, `find_payment_candidates`, `get_payment_record`, `get_refund_history`, `get_inventory`, `get_policy`, `get_order_events`; tools must query actual simulator/domain data with provenance and freshness.
- In-house runtime: read context -> request plan -> validate -> run authorized READ tool or ASK_USER/PROPOSE_ACTION/ESCALATE/STOP -> observe -> replan. Max ~6 read steps, 2 replans, no loop, async job (or safe background worker) and progress events. Model can only PROPOSE actions, not issue writes.
- Persist objective, concise decision_summary, evidence IDs, tool result metadata, unknowns and step reason in CaseEvent. Do NOT expose hidden chain-of-thought.

## End-to-end example
Given Vietnamese customer complaint "Tôi đã thanh toán nhưng đơn chưa xác nhận", with appropriate mock DB facts, planner should choose provider/order read tools and propose order-sync only after verified authoritative payment evidence. Two ambiguous matches -> ASK_USER/WAITING_EVIDENCE. Timeout -> UNKNOWN.

## Tests
Pydantic rejects hallucinated tools and `payment_verified` claims; prompt-injection user content cannot bypass allowlist; step/time limit; missing key honest fallback; same Case state reflected after tool reads.

## Stop condition
Do not build incident taskforces, refund executor or polished UI in this prompt. Return changed files, real tests, and any assumptions for P03.


---

<!-- SOURCE: prompts/03_policy_execution.md -->

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


---

<!-- SOURCE: prompts/04_risk_refund.md -->

# PROMPT 04 — RISK CENTER AND REFUND SAFETY (Backend owner A)

Read project contracts and current code. Add **deterministic transaction/refund safety signals**, with AI only for explanatory or ambiguity work. Keep it small and demonstrable.

## Implement
- RiskSignal with categories `ERROR`, `ANOMALY`, `SUSPECTED_ABUSE`, `CONFIRMED_POLICY_VIOLATION`; responses `OBSERVE`, `REVIEW`, `HOLD_ACTION` (only a scoped action controlled by this demo).
- Provider/merchant ledger-backed refundable amount: `eligible_amount - settled_refunds - pending_or_held_refunds`, guard against partial, duplicate and cumulative over-refund; no negative remainder.
- Detect same transaction/refund request reuse, already refunded payment, mismatched order and amount, missing authoritative payment, expired/stale approvals and order version change.
- Risk signals create a CaseEvent with non-accusatory Vietnamese explanation and source refs. If ambiguity or conflicting narratives, AI may request evidence; NEVER label customer as proven fraud.
- API `GET /api/v1/risks` scoped to demo session; wire into existing proposals and Case flows.
- Seed two distinct cases: valid conditional refund proposal that can be manager-approved in MOCK; unsafe duplicate/over-limit refund that must be blocked. Demo refund effects simulated only, never connect production.

## Acceptance tests
T07–T12 and T21; verify valid partial refund once, invalid second attempt blocked, pending refund not duplicated, false block not triggered for a truly unrelated eligible order. DB states and audit evidence must reflect actual effect.

## Scope control
No fraud ML training, no photo-tamper detection, no bank-grade fraud claim. No separate ML service. Document rules, severity and known blind spots. Finish with exact test outcomes and changed files.


---

<!-- SOURCE: prompts/05_incident_taskforce.md -->

# PROMPT 05 — ADAPTIVE INCIDENT TASKFORCE (AI owner B; P1)

First check P0 gate: one Case end-to-end, refund risk, policy and verifier. If P0 fails, DO NOT start Taskforce; help unblock P0 and explain. If P0 passes, implement narrow P1 in-house.

## Build
- `IncidentCorrelator`: candidate grouping based on shared causal evidence (provider/worker/batch, event time window, same error code, SKU/promotion, observation timeline). No pure message embedding similarity grouping. Keep uncertainty and allow separate unrelated lookalikes.
- `Incident` + `IncidentCase` relation and per-case independent state. Each incident has a hypothesis and evidence refs, no hidden ground truth; human can inspect candidate groups.
- `CapabilityRegistry` with logical roles Finance, Operations, Risk, Support, each bounded read-tool/permission scope. `TaskforceManager` allocates 2–3 role tasks to an incident; uses same process/runtime/LLM, not one process or model per Case; records typed JSON handoffs via AgentTask and CaseEvent.
- Shared evidence for root-cause investigation ONLY; never imply a shared result verifies each child. Per-case policy and verifier still required for writes.
- API read endpoints `GET /api/v1/incidents`, details and assigned tasks. Include trace: 'Detected 12 potentially linked cases by dropped-sync worker ID and time range'.

## Scenario
12 affected cases share worker/batch metadata; 2 unrelated cases have similar customer message but different root cause. Demonstrate grouping and rejection of incorrect merge. Show Finance investigates payment, Operations checks sync; Risk may review suspicious refunds.

## Tests
I01–I04: false merge prevention, no oracle leak, role scopes, no auto-resolving children. If code complexity escalates, implement read-only taskforce recommendations rather than half-working autonomous agents.

Return exact tests and the UI/API contract for frontend owner. STOP.


---

<!-- SOURCE: prompts/06_simulator.md -->

# PROMPT 06 — MINI MEGA LIVE SIMULATOR (AI/Simulator owner B, coordinated with Backend A)

Implement a genuine self-contained synthetic commerce world for judges. Keep scope minimal; do not build an academic digital twin framework. Must reuse FinResolve's existing entities, adapters and policy actions.

## Requirements
- `ScenarioRegistry` with seeded `payment_sync`, `refund_risk`, `mega_live` modes. Each run uses an isolated guest DemoSession and deterministic seed; reset is possible.
- For `mega_live` create 50–100 orders, payment records, simple SKU inventory, promo quota and order events; inject a small mix of dropped order-sync, duplicate events, refund attempt, one stock contention and several unrelated lookalikes. Reproduce exact outcomes from same seed.
- Separate **true World State** from `Observable API`: agent sees only relevant authorized order/payment/stock/events; never expose injected fault labels, ground truth fields, expected solution or Oracle to model.
- Faults should alter actual DB/state event delivery, not just display risk counters on UI. ActionExecutor changes simulated state, Verifier reads it back.
- Keep Oracle as a simple test-only scorer with `PASS/FAIL/UNKNOWN`, unsafe effects, mismatched/verified count; not an independent new platform. Aggregate results labeled MOCK.
- Endpoints as `docs/PRODUCT_CONTRACT.md`: create/run scenario, status/report; run async with progress or polling, no blocking browser during batch.
- Two distinct controls: 'Demo paid order mismatch' and 'Demo duplicate refund'; `mega_live` starts burst event stream and shows incident candidates.

## Mandatory safety
No real-world platform connections; use fake business IDs, synthetic persons and sample images explicitly marked SAMPLE. Do not create realistic transferable fake financial proof documents. No huge number of LLM calls for bulk orders; process normal events in code/batches and call AI only for selected exceptions.

## Acceptance
T01/T05/T14/T24 + I05; DB diff after action, seed repeatability, no oracle leakage. Document performance under 100-order demo with actual measurement, if run. Report limitations.


---

<!-- SOURCE: prompts/07_frontend.md -->

# PROMPT 07 — REAL PRODUCT UI (Frontend/DevOps owner C)

Read `AGENTS.md`, `docs/PRODUCT_CONTRACT.md` and existing OpenAPI responses. Implement clean, Vietnamese-first, responsive, accessible Next.js UI. This is a real feature implementation, not a design mockup.

## Navigation (3 pages; use shared design system)
1. `/demo` Start page with short value promise, 3 large one-click buttons: 'Thanh toán chưa cập nhật', 'Yêu cầu hoàn tiền bất thường', 'Mega Live'. Also an optional free-text complaint input. Clearly show MOCK or MODEL_API/DEMO_RULES mode.
2. `/cases` Case Center: case list + detail view with status, customer complaint, evidence grouped by CLAIM/OBSERVED_FACT/UNKNOWN, timeline (Agent read step, tool result, policy, approval, verification); relevant CTA only if authorized. Show `WAITING_EVIDENCE`, `WAITING_APPROVAL`, `UNKNOWN_OUTCOME`, and retry/error states honestly.
3. `/risks` Risk Center and `/live` lightweight Mega Live route: risk signals with source, severity and action; live event counts, incident candidate and role tasks, verified/unverified cases. If backend P1 not ready, hide or show 'Đang triển khai' rather than fabricate simulated real-time statuses.

## Integration
- Use backend `/api/v1` endpoints and typed `fetch` API client. Create guest session once on load and keep token in safe session-scoped storage/cookie as supported; all case queries session scoped.
- Implement polling or SSE for investigation progress; no blank waiting pages. Include obvious timestamps, status, error messages, 'Reset thử nghiệm'.
- Real approval uses backend role/session authorization; do not enable approval bypass merely with frontend button state.
- Avoid excessive dependencies; Tailwind + a small component library if already installed, otherwise create primitives.
- Layout desktop/laptop first, mobile responsive. Vietnamese labels, tooltips for technical terms, readable contrast and keyboard buttons.

## Acceptance
Fresh incognito browser -> `/demo` -> click scenario -> see created Case -> timeline -> verified result or safely held action -> see Risk/Incident summary. TypeScript build passes; console has no uncaught errors; no API key in client bundle. Screenshots from real running UI if browser available.

## Deliver
Changed files, screenshots or run steps, confirmed endpoint behavior, build/test output. No fake cards that are disconnected from backend.


---

<!-- SOURCE: prompts/08_integration_tests.md -->

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


---

<!-- SOURCE: prompts/09_release.md -->

# PROMPT 09 — PUBLIC DEMO RELEASE (Frontend/DevOps owner C + team review)

Your goal is to ship a stable, honest public MVP for a hackathon judge to use without credentials. Read `AGENTS.md`, `docs/DELIVERY_PLAN.md`, `docs/QA_MATRIX.md`. Do not deploy without human review of account secrets/target platform; do not modify actual payment accounts or production financial services.

## Ship
1. Production Docker builds and runtime config; `.env.example` lists every required env var. No secret keys committed, no frontend secret exposure. Use hosted HTTPS URL and persistent PostgreSQL/volumes as appropriate.
2. Health/readiness endpoints, restart policies, error recovery; responsive pages `/demo`, `/cases`, `/risks`, `/live` (P1 hidden if incomplete).
3. Three judge scenarios one-click. No manual webhook or MoMo credentials. Demo clearly says MOCK or authorized SANDBOX, and separately states whether MODEL_API or DEMO_RULES provided reasoning.
4. `docs/JUDGE_GUIDE_VI.md`: <=1 A4 page, 3-click tutorial, brief explanation and limitations. `docs/KNOWN_LIMITATIONS.md`: honest unavailable capabilities.
5. `docs/DEMO_SCRIPT_VI.md`: ~3-minute video/live script using *actual* features only, with fallback screen recording. Include 'what AI decided', 'what code verified', and 'what action was stopped'.
6. Check session isolation, rate-limit and image upload caps on live server; test under fresh incognito browser from outside developer laptop.
7. Report public URL, deployment environment, build artifacts, smoke checklist and remaining limitations. Do not claim URL live if it has not been personally verified.

## Deadline
Freeze as of 02/12/2026; recheck and submit BEFORE 03/12 deadline. No last-minute broad refactors or new dependencies.
