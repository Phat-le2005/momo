# AGENTS.md — FinResolve AI Coding Rules

## Goal and nonnegotiable scope
Build a Vietnamese-first, self-serve, online AI-native agentic operations MVP for payment/commerce exceptions. Prioritize an honest, functional end-to-end Case pipeline, fast UX, reliable policy enforcement, and real state verification. First read `docs/PRODUCT_OVERVIEW.md` and then `docs/PRODUCT_CONTRACT.md`, `docs/DELIVERY_PLAN.md`, and `docs/QA_MATRIX.md` before editing.

### Core architecture
- Monorepo: `frontend` Next.js + TypeScript + Tailwind; `backend` Python 3.12+ + FastAPI + Pydantic v2 + SQLAlchemy/Alembic; PostgreSQL as source of truth. Docker Compose for local development.
- In-house modules: case/evidence/context, bounded AI planner, agent runtime, incident correlator/taskforce, policy/approval/action executor, verifier, simulator.
- External LLM API allowed behind swappable `ModelAdapter`; `MOCK` must always be visibly labeled. MCP SDK may be used as TRANSPORT only; use adapters/HTTPX when faster for MVP. Do not depend on an agent orchestration framework such as LangChain/LangGraph/CrewAI/AutoGen.
- Modular monolith. No Kafka, Kubernetes, vector DB, GPU model serving, complex microservices, or multi-tenant SaaS unless explicitly justified.

### Hard domain invariants
1. User messages, screenshots and model output are UNTRUSTED CLAIMS; payment success requires an authenticated provider/business source record.
2. LLM only generates structured proposals and read actions, never unguarded writes, privileges, `payment_verified=true`, or final business truth.
3. All writes go through Policy/Action Gateway and enforce tenant/session scope, preconditions, current state/version, idempotency and approval if needed.
4. Refund is a proposal by default; sandbox mock executor may simulate approved effect. Verify refunds against provider/ledger state, never just a request success response.
5. UNKNOWN external outcomes stay UNKNOWN; no blind write/retry. No double payment capture, double refund, oversell, or approval replay.
6. A shared Incident never automatically resolves its child Cases. Each Case needs independently verified postconditions.
7. Any 'risk' signal is NOT proof of fraud. Decisions OBSERVE/REVIEW/HOLD_ACTION are scoped; avoid false accusations and broad account suspension.
8. Prompt injection in user text/tool data must not change model instructions or backend permission logic.
9. Demo assets must be synthetic and clearly watermarked MOCK; never solicit or store real bank statements or credentials on a public demo.

### Engineering and UX
- Small complete increments. Keep P0 first; P1 can be cut. No stubs that report success, hard-coded 'success' responses, or mocked external calls silently masquerading as live integrations.
- Every route has validation, error handling and coherent response schema. Business errors are typed; use state transitions, not loose strings.
- One run context per guest; isolate sessions and set TTL, rate limit, file caps and LLM budgets. UI reports source mode, evidence, agent step, approval and verification state.
- UI in clear Vietnamese, accessible and responsive; first-time judge runs one-click scenarios without login and without API credentials.
- Use env vars and `.env.example`, never commit `.env`, API keys or financial account identifiers.
- Add pytest tests to every backend behavior and at least smoke tests for happy paths/unsafe actions. Add simple frontend checks when feasible.

### Workflow for coding agents
1. Inspect repository and target files; restate the task in 3–6 bullets and locate existing tests.
2. Implement only the requested slice; prefer modifying existing modules over creating redundant ones.
3. Run formatter/lint/typecheck/tests and record actual results; do not claim passing tests if not executed.
4. Report changed files, API contract changes, commands executed, behavior demonstrated, limitations and next dependency.
5. Never delete another agent's changes. If blocked, make a small working fallback and document it.
