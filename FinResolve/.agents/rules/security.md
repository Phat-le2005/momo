---
trigger: model_decision
description: "Apply FinResolve payment, refund, inventory, tool execution, demo-data and approval safety invariants when editing security-sensitive modules."
---

# FinResolve sensitive-code rule
When editing payments, refunds, inventory, approvals, policy, Action Gateway, external tools, or simulator state:
- Ground payment/refund/order facts in authoritative adapters; user screenshots and LLM output are untrusted.
- Never let a model bypass policy, set authoritative state, or issue a financial write directly.
- Always validate tenant/session scope, schema, state version, current policy, idempotency, and approval digest before effects.
- On external timeout leave UNKNOWN; do not blindly repeat a potentially side-effecting request.
- Verify postconditions by reading backend/source of truth; a tool 200 response is insufficient.
- Simulate with explicit MOCK labels and synthetic data only. Do not connect production services or commit secrets.
- Write one positive test, one blocked-action test, and one retry/concurrency test for critical actions.
