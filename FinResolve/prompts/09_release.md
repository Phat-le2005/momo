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
