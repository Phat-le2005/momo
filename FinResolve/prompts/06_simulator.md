# PROMPT 06 — MINI MEGA LIVE SIMULATOR (AI/Simulator owner B, coordinated with Backend A)

Implement a genuine self-contained synthetic commerce world for judges. Keep scope minimal; do not build an academic digital twin framework. Must reuse FinResolve's existing entities, adapters and policy actions.

## Requirements
- `ScenarioRegistry` with seeded `payment_sync`, `refund_risk`, `mega_live` modes. Each run uses an isolated guest DemoSession and deterministic seed; reset is possible.
- For `mega_live` create 50–100 orders, payment records, simple SKU inventory, promo quota and order events; inject a small mix of dropped order-sync, duplicate events, refund attempt, one stock contention and several unrelated lookalikes. Reproduce exact outcomes from same seed.
- Separate **true World State** from `Observable API`: agent sees only relevant authorized order/payment/stock/events; never expose injected fault labels, ground truth fields, expected solution or Oracle to model.
- Faults should alter actual DB/state event delivery, not just display risk counters on UI. ActionExecutor changes simulated state, Verifier reads it back.
- Keep Oracle as a simple test-only scorer with `PASS/FAIL/UNKNOWN`, unsafe effects, mismatched/verified count; not an independent new platform. Aggregate results labeled MOCK.
- Endpoints as `docs/PRODUCT_CONTRACT.md`: create/run scenario, status/report; run async with progress or polling, no blocking browser during batch.
- Two distinct controls: 'Demo paid order mismatch' and 'Demo duplicate refund'; `mega_live` starts burst event stream and shows incident candidates.

## Mandatory safety
No real-world platform connections; use fake business IDs, synthetic persons and sample images explicitly marked SAMPLE. Do not create realistic transferable fake financial proof documents. No huge number of LLM calls for bulk orders; process normal events in code/batches and call AI only for selected exceptions.

## Acceptance
T01/T05/T14/T24 + I05; DB diff after action, seed repeatability, no oracle leakage. Document performance under 100-order demo with actual measurement, if run. Report limitations.
