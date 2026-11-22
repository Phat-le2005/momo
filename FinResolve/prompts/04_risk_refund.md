# PROMPT 04 — RISK CENTER AND REFUND SAFETY (Backend owner A)

Read project contracts and current code. Add **deterministic transaction/refund safety signals**, with AI only for explanatory or ambiguity work. Keep it small and demonstrable.

## Implement
- RiskSignal with categories `ERROR`, `ANOMALY`, `SUSPECTED_ABUSE`, `CONFIRMED_POLICY_VIOLATION`; responses `OBSERVE`, `REVIEW`, `HOLD_ACTION` (only a scoped action controlled by this demo).
- Provider/merchant ledger-backed refundable amount: `eligible_amount - settled_refunds - pending_or_held_refunds`, guard against partial, duplicate and cumulative over-refund; no negative remainder.
- Detect same transaction/refund request reuse, already refunded payment, mismatched order and amount, missing authoritative payment, expired/stale approvals and order version change.
- Risk signals create a CaseEvent with non-accusatory Vietnamese explanation and source refs. If ambiguity or conflicting narratives, AI may request evidence; NEVER label customer as proven fraud.
- API `GET /api/v1/risks` scoped to demo session; wire into existing proposals and Case flows.
- Seed two distinct cases: valid conditional refund proposal that can be manager-approved in MOCK; unsafe duplicate/over-limit refund that must be blocked. Demo refund effects simulated only, never connect production.

## Acceptance tests
T07–T12 and T21; verify valid partial refund once, invalid second attempt blocked, pending refund not duplicated, false block not triggered for a truly unrelated eligible order. DB states and audit evidence must reflect actual effect.

## Scope control
No fraud ML training, no photo-tamper detection, no bank-grade fraud claim. No separate ML service. Document rules, severity and known blind spots. Finish with exact test outcomes and changed files.
