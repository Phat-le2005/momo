# FinResolve acceptance & regression matrix

## P0 test IDs — must pass before public demo
| ID | Scenario | Required behavior |
|---|---|---|
| T01 | Valid paid provider record, order pending | Create Case, propose sync, policy allows, apply once, postcondition verifies |
| T02 | Screenshot claims paid, provider has no record | No payment confirmation; WAITING_EVIDENCE/ESCALATE |
| T03 | Two candidate transactions same amount | Ask for reference or review; never randomly pick |
| T04 | Provider query times out | UNKNOWN_OUTCOME; no blind retry or resolved |
| T05 | Same provider IPN twice | Deduplicate effects; audit both receipts as appropriate |
| T06 | Same action requested twice | Idempotency produces one effect |
| T07 | Refund already completed | Duplicate proposed refund blocked |
| T08 | Refund pending | Reconcile; do not create additional refund blindly |
| T09 | Refund over remaining eligible balance | Block safely |
| T10 | Legitimate refundable amount with valid approval | Mock executes exactly once and verifier checks |
| T11 | Approval bound to different order or amount | Reject action |
| T12 | Approval expired | Reject action |
| T13 | Order version changes before write | Conflict; re-read/replan, no stale write |
| T14 | 2 concurrent stock reservations, 1 unit left | No oversell/double reservation |
| T15 | Prompt injection in customer message | Never change policy or privileged state |
| T16 | Invalid LLM JSON / invalid tool | Reject safely and escalate/fallback, no unvalidated tool call |
| T17 | Agent hits step/time/token budget | Stop, record reason, no infinite loop |
| T18 | Guest session A reads B's Case | 403/404; no data leak |
| T19 | Public user repeatedly starts costly AI calls | Rate limit/quota reached; clear UX |
| T20 | Tool returns 200 but DB postcondition not met | NOT RESOLVED, emit verification failure |
| T21 | Risk signal only (possible anomaly) | Mark REVIEW, do not accuse user of fraud |
| T22 | No LLM API key | Deterministic demo works; UI visibly labels `DEMO_RULES` not actual AI |
| T23 | Invalid upload type/size (if upload enabled) | Reject without storing untrusted content |
| T24 | Frontend new session → scenario → case timeline | Works in fresh/incognito browser on deployed HTTPS URL |

## P1 tests
- I01: 8–15 cases with common cause grouped by evidence, no exposed oracle labels.
- I02: unrelated lookalike cases not merged (avoid false merge).
- I03: shared incident resolution never auto-resolves unverified child Cases.
- I04: taskforce assigns logical roles with scoped read access only.
- I05: simulator seeded run repeatable and resettable.

## Reporting
For each test, record expected, actual, pass/fail, screenshot/log or pytest output. Keep honest list of known limitations in README. Do not claim a test passed if it was not executed.
