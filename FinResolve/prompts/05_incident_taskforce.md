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
