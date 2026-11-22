# Product-first delivery plan — 22/11 to 03/12/2026

## Milestones (3 engineers + coding agents, not 3 autonomous production agents)
The deadline in v1.2 is 03/12/2026. 22 Nov → 02 Dec are **11 full development dates**, and 03 Dec is submission/smoke test. Do not assume 13 *remaining* full coding days.

| Date | Main deliverable | Owner | Acceptance gate |
|---|---|---|---|
| 22 Nov | Repo, docker-compose, frontend/backend skeleton, OpenAPI skeleton, mock seed | A backend + C frontend | `docker compose up --build` starts health and web |
| 23 Nov | Cases/Evidence/DB migrations, demo session isolation, fixtures | A | `POST /cases` + `GET /cases` + audit tests |
| 24 Nov | Real model adapter + bounded planner + read tools | B AI | One free-form Case produces validated plan, tool evidence captured |
| 25 Nov | Policy, action contracts, approval, executor and verifier | A+B | `payment_sync` E2E complete; unknown stays unknown |
| 26 Nov | Case Center user interface + SSE/polling + risk guardrails | C UI + A | A judge can run payment and blocked refund cases without CLI |
| 27 Nov | Refund Risk Center + negative tests + free text | A+B | duplicate/over-limit blocked, legitimate route works |
| 28 Nov | Incident grouping and 2–3 logical taskforce roles (cut if P0 late) | B | common cause grouped, unrelated not merged |
| 29 Nov | Mega Live simplified event generator 50–100 orders | B+C | state changes in DB, observable API no oracle leak |
| 30 Nov | Connect all pages, polish, guest isolation and session quotas | C | 3 click-to-run scenarios stable |
| 01 Dec | Deploy public, E2E security/regression + run demo | All | Test on fresh browser, 1-page guide and screenshots |
| 02 Dec | Stabilize, freeze, fallback recording, submit materials rehearsal | All | No P0 blocker, demo link remains up |
| 03 Dec | Recheck and **submit before deadline** | Lead | submission receipt captured |

## Roles and Git ownership
A — Backend/Safety: `services/api/app/{db,cases,evidence,policy,execution,verification,risk,connectors}` + migrations + tests.
B — AI/Incidents: `services/api/app/{ai_core,incidents}` + `simulator/` logic + planner test cases.
C — Frontend/DevOps: `apps/web`, `infra`, frontend integration, demo README and hosted deployment.

Use one repo with 3 branches/worktrees. Freeze schemas and REST shapes on first day; one maintainer owns DB migrations. Merge small verified PRs regularly. Code agent should not unilaterally alter another owner's API schemas.

## P0 and P1 gates
- **26 Nov hard gate:** a whole `payment_sync` Case completes in DB with evidence, controlled action, read-back verification. If not: delete new P1 work, help finish P0.
- **28 Nov gate:** `refund_risk` safe block and legitimate path work. If not: no Taskforce, no advanced live simulator.
- **30 Nov gate:** a stranger can access `/demo`, click any scenario and understand outcome; if not, UX/deploy outrank new tools.
- **01 Dec gate:** all three scenarios stable with seeded data and no secrets; smoke on deployed URL.
- **02 Dec:** freeze. No new libraries, new models, broad refactors or architecture changes.

## Definition of done, each task
- File edits committed/ready for review; no fake success; endpoint and DB state actually change as specified.
- Happy path, unsafe path and one error path tests executed.
- Docker local run instructions work; migration applied; lint/typecheck/pytest evidence reported.
- UI distinguish MOCK from authorized provider, CLAIM from OBSERVED FACT, and RESOLVED from UNKNOWN.
- Error/toast/retry states clear; no request hangs without progress.

## Minimum quality targets (targets, not measured performance)
- User-facing interactions feel immediate; case investigation is async and displays progress.
- At least 20 critical integration/guardrail test scenarios; 0 unsafe writes and 0 false RESOLVED within that mandatory suite.
- API read p95 target <500ms under single-user demo load; no blanket 3–10s guarantee for LLM Cases.
- 3 scenarios reproducible/resettable; no sign-in/configuration by judge.
- No auto financial writes or real production links.
