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
