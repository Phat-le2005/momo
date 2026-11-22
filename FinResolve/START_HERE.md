# START HERE — FIRST MESSAGE FOR CODEX / ANTIGRAVITY

Use this repository as the implementation workspace for **FinResolve AI**. The owner supplied the original product plan at `references/FinResolve_AI_Native_v1.2.docx`; a searchable Markdown conversion is included next to it.

**Before writing code**, read these files in this order:
1. `AGENTS.md`
2. `docs/PRODUCT_OVERVIEW.md`
3. `docs/PRODUCT_CONTRACT.md`
4. `docs/DELIVERY_PLAN.md`
5. `docs/QA_MATRIX.md`
6. `prompts/00_bootstrap.md`

The product is an **AI-native agentic payment & commerce exception/risk operations MVP**, with evidence-grounded investigation, bounded AI planning, safe policy-controlled actions, verification, a user-facing risk center, and a self-serve sandbox. Prioritize **working code, accurate business state, speed and user experience** over academic benchmarking or expanding architecture.

Your first deliverable is **Prompt 00 only**. Make actual file changes in this repository (not code examples in chat): create the runnable Next.js + FastAPI + PostgreSQL scaffold, Docker Compose, health endpoint and a Vietnamese `/demo` page. Keep the source docs untouched. Do not use prebuilt agent orchestration frameworks. Use synthetic business data only. No real-money operations or unverified payment claims.

**Process:**
- Briefly restate the goal in 5 bullets, then immediately implement `prompts/00_bootstrap.md`.
- Run available tests, typecheck/build and Docker Compose checks; report actual outputs and blockers. Never claim a command passed unless executed.
- Report edited files and startup commands.
- STOP after Prompt 00; do not start Prompt 01 until requested.
