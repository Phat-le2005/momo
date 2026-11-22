# FinResolve v1.2 — Frozen Product & API Contract (MVP)

## Product promise
From anomalous business event or customer complaint to **evidence-grounded, policy-controlled, verifiably resolved** payment/commerce exception. AI is central in multilingual/free-form understanding, uncertainty reduction, investigation planning and incident investigation; deterministic code handles balances, rules, authorization, transactions and verified effects.

## P0: must ship
1. Vietnamese-first Self-Serve Demo: 3 one-click synthetic scenarios and free-text intake; no signup/API configuration.
2. Case Center: create/inspect Case, Evidence, AI decision trace, current state, next action, unresolved unknowns.
3. A bounded AI loop with ModelAdapter (API if configured; clearly labeled deterministic demo fallback otherwise). Tool reads are real against simulator DB, not canned results.
4. Policy/Approval/Executor/Verifier end-to-end, with 0 unsafe transitions on mandatory tests.
5. Risk Center: deterministic duplicate/refund-limit/pending/authority checks + AI explanation when ambiguity exists.
6. Public deploy, README, 1-page Vietnamese judge instructions, health check, single-command local startup.

## P1: if P0 stable
- Simple incident grouping using cause-related evidence and Adaptive Taskforce of 2–3 logical roles sharing a runtime/model.
- Mega Live of 50–100 synthetic orders, fault injection with a small group of same-cause cases and at least one unrelated lookalike.
- Sample image upload + vision extraction; authorized MoMo sandbox query only when credentials are obtained.

## NEVER in MVP
- Real-money payment/refund; unapproved account freeze; full fraud detection claims; indiscriminate screenshot authentication; scraping external platforms; autonomous agent generation/privilege escalation; Kafka/K8s/own training.

## Source authority ranking
Verified provider/merchant ledger (authorized and scoped) > own business DB and versioned policy > tool observations with provenance > user text/images (CLAIM) > AI hypothesis. **Never promote a lower-level claim to verified payment status.**

## Core entities (recommended; migration can refine names, must update contracts)
- `Case`: id, session_id/tenant_id, type, goal, state, version, owner, created_at, updated_at.
- `Evidence`: id, case_id, kind {CLAIM, OBSERVED_FACT, HYPOTHESIS, UNKNOWN}, source, source_ref, observed_at, payload_json, confidence (optional, not authority).
- `CaseEvent`: id, case_id, type, payload_json, created_at; append-only audit trail.
- `RiskSignal`: id, case_id, category, level, evidence_refs, response {OBSERVE,REVIEW,HOLD_ACTION}.
- `ActionProposal`: id, case_id, action_type, params_json, evidence_refs, expected_version, risk_level, status.
- `Approval`: id, proposal_id, approver_role, decision, digest, expiry, created_at.
- `ActionExecution`: id, proposal_id, idempotency_key, state {PENDING,EXECUTED,FAILED,UNKNOWN}, created_at.
- `Verification`: id, case_id, action_execution_id, postconditions_json, verdict, checked_at.
- `Incident`: id, session_id/tenant_id, hypothesis, status, evidence_refs; `IncidentCase` relation.
- `AgentTask`: id, case_id, incident_id(optional), role, objective, state, evidence_refs.
- Simulator: Order, Payment, Refund, Inventory, Voucher, BusinessEvent, session-scoped seed.

## Case states
`NEW -> INVESTIGATING -> WAITING_EVIDENCE / WAITING_APPROVAL / EXECUTING -> VERIFYING -> RESOLVED`; also `ESCALATED`, `FAILED`, `UNKNOWN_OUTCOME`. Never mark RESOLVED without verified postcondition. Order, Payment, Refund, Inventory have SEPARATE lifecycles.

## Bounded Planner JSON (Pydantic validated)
```json
{
  "case_id": "CASE-001",
  "goal": "Verify complaint about payment and order",
  "known_evidence_refs": ["E-001"],
  "unknowns": ["payment_provider_match"],
  "hypotheses": [{"label": "missing_order_sync", "supporting_evidence_refs": []}],
  "next_step": {"kind": "READ_TOOL", "tool_name": "find_payment_candidates", "args": {"order_id": "O-001"}},
  "decision_summary": "Check the authorized payment source before touching order state."
}
```
`next_step.kind`: READ_TOOL | ASK_USER | PROPOSE_ACTION | ESCALATE | STOP. Model may not set payment verification facts or call arbitrary URLs.

## Minimal tool interface
Reads: `get_order`, `find_payment_candidates`, `get_payment_record`, `get_refund_history`, `get_inventory`, `get_policy`, `get_order_events`. Writes are proposals only: `propose_order_sync`, `propose_refund`; execution is backend-only: `execute_approved_action` -> `verify_case_state`.

## Suggested REST API (keep in `openapi.json`, version under `/api/v1`)
- `GET /healthz`
- `POST /api/v1/demo/sessions` -> `session_id`, expiry, rate quota
- `POST /api/v1/demo/scenarios/{scenario_id}/run` -> `run_id`, `case_ids`, `source_mode=MOCK`; scenarios `payment_sync`, `refund_risk`, `mega_live`
- `POST /api/v1/cases` -> Case create from `{session_id, text, optional_attachment_id}`
- `GET /api/v1/cases/{case_id}` -> Case detail + summary
- `GET /api/v1/cases/{case_id}/events` -> Decision Trace / audit timeline
- `POST /api/v1/cases/{case_id}/investigate` -> accepted/background job reference
- `GET /api/v1/cases/{case_id}/status` -> state/job progress
- `GET /api/v1/risks` -> scoped risk list
- `GET /api/v1/incidents` -> scoped incident list (P1)
- `POST /api/v1/actions/{proposal_id}/approve` -> authorized demo-manager approval, not public arbitrary bypass
- `GET /api/v1/demo/runs/{run_id}/report` -> honest verified outcomes (P1)
Use `X-Demo-Session` or secure demo-session cookie per request, not freely supplied tenant IDs; session ownership must be checked by server.

## 3 reference demo scenarios
- **payment_sync**: provider payment VERIFIED SUCCESS but order PENDING (e.g., dropped sync). AI reads evidence; proposes safe order-sync; backend policy checks and executes; reads back order version/status; marks RESOLVED only if postcondition holds.
- **refund_risk**: an already-refunded or pending-refund order receives another complaint. Risk detects duplicate/over-cap; safe action is REVIEW/HOLD_ACTION or rejection of a specific proposed refund, not blanket account ban. A legitimate new refund request should not be blocked without reason.
- **mega_live**: synthetic 50–100 orders, 8–15 delayed/lost events in a shared batch, 2–3 visually similar but unrelated cases; correlation proposes Incident and assigns Finance/Operations/Risk taskforce. Resolve each child Case only after independent read-back.

## Model/simulator rules
- If no LLM key, use deterministic `DEMO_RULES` mode with clear UI badge; never represent it as live generative AI.
- Model adapter env config: `MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_API_KEY`, budget and timeout. Log token metrics if API exposes them.
- Simulator Oracle/hidden truth must not be in tool observations or model context. Allow reset/reseed; synthetic data only.
