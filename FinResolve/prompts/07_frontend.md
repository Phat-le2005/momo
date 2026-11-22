# PROMPT 07 — REAL PRODUCT UI (Frontend/DevOps owner C)

Read `AGENTS.md`, `docs/PRODUCT_CONTRACT.md` and existing OpenAPI responses. Implement clean, Vietnamese-first, responsive, accessible Next.js UI. This is a real feature implementation, not a design mockup.

## Navigation (3 pages; use shared design system)
1. `/demo` Start page with short value promise, 3 large one-click buttons: 'Thanh toán chưa cập nhật', 'Yêu cầu hoàn tiền bất thường', 'Mega Live'. Also an optional free-text complaint input. Clearly show MOCK or MODEL_API/DEMO_RULES mode.
2. `/cases` Case Center: case list + detail view with status, customer complaint, evidence grouped by CLAIM/OBSERVED_FACT/UNKNOWN, timeline (Agent read step, tool result, policy, approval, verification); relevant CTA only if authorized. Show `WAITING_EVIDENCE`, `WAITING_APPROVAL`, `UNKNOWN_OUTCOME`, and retry/error states honestly.
3. `/risks` Risk Center and `/live` lightweight Mega Live route: risk signals with source, severity and action; live event counts, incident candidate and role tasks, verified/unverified cases. If backend P1 not ready, hide or show 'Đang triển khai' rather than fabricate simulated real-time statuses.

## Integration
- Use backend `/api/v1` endpoints and typed `fetch` API client. Create guest session once on load and keep token in safe session-scoped storage/cookie as supported; all case queries session scoped.
- Implement polling or SSE for investigation progress; no blank waiting pages. Include obvious timestamps, status, error messages, 'Reset thử nghiệm'.
- Real approval uses backend role/session authorization; do not enable approval bypass merely with frontend button state.
- Avoid excessive dependencies; Tailwind + a small component library if already installed, otherwise create primitives.
- Layout desktop/laptop first, mobile responsive. Vietnamese labels, tooltips for technical terms, readable contrast and keyboard buttons.

## Acceptance
Fresh incognito browser -> `/demo` -> click scenario -> see created Case -> timeline -> verified result or safely held action -> see Risk/Incident summary. TypeScript build passes; console has no uncaught errors; no API key in client bundle. Screenshots from real running UI if browser available.

## Deliver
Changed files, screenshots or run steps, confirmed endpoint behavior, build/test output. No fake cards that are disconnected from backend.
