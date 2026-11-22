# Coding-agent delegation board — 3 human owners

**Never run multiple coding agents editing the same checkout.** Each human owner supervises one worktree/branch and reviews code before merge.

## Parallelization map

```text
Stage 0 (single agent):     P00 repo + Docker + health + web skeleton
Stage 1 (single backend):  P01 schema + Cases + Evidence + auth/session contracts
                            |             |               |
Stage 2 (parallel):      P02 AI Core    P03 Policy      P07 frontend UI
                          (Owner B)     (Owner A)       (Owner C)
                            |             |
Stage 3:                  P05 Incident  P04 Refund Risk   P07 integration
                          (P1 only)       (P0)
                            \            /
Stage 4:                 P06 Mini Mega Live simulator (P1, reduce if late)
Stage 5:                 P08 integration tests / bug fixes
Stage 6:                 P09 deploy / freeze / submission
```

## Branch ownership
- `feat/cases-policy` — Backend A: migrations, models, Case routes, action executor, Risk rules. The only engineer who edits Alembic migrations.
- `feat/ai-incidents` — AI B: model adapter, agent planner/runtime, read tool registry, incident taskforce, simulator events.
- `feat/web-demo` — Frontend C: Next.js pages, API client, CSS, Docker frontend config, public deployment.
- `main` — review/merge only. Migrations integrated first; all owners pull/rebase before integration.

## Example with Git worktrees
```bash
git switch -c feat/cases-policy
# In a separate directory from the first worktree:
git worktree add ../finresolve-ai -b feat/ai-incidents main
git worktree add ../finresolve-web -b feat/web-demo main
```
Do not blindly copy these commands if branch names already exist; adapt to current Git state.

## Handoff format after every coding-agent prompt
- What changed: files, endpoints, migration version, tests.
- How to run: exact commands, environment variables.
- Contract changes: backwards-compatible vs breaking; request approval before breaking changes.
- Risks: anything unfinished or mocked.
- Next prompt: task owner and dependency.

## Crisis rules
- If P0 Case E2E does not pass by Nov 26: stop P05 and large P06; finish Case/Policy/Verifier.
- If model API unreliable: keep explicit DEMO_RULES fallback and honest label, no fake AI claims.
- If UI not self-serve by Dec 1: stop new features, work on UI/deployment.
