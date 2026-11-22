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
