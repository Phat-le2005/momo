# FINRESOLVE AI — VISUAL EXPERIENCE & DESIGN SYSTEM REPORT (PROMPT 15.1)

- **Branch**: `feat/prompt-15-1-signature-experience` (branched from `main` `8254466`; NOT merged into `main`)
- **Creative Direction**: **Premium Futuristic Fintech — Light Blue Edition**
- **VNPAY Status**: Intentionally disabled (`LOCAL_MOCK` sandbox only)
- **Truthfulness Guarantee**: Zero fabricated KPIs, zero fabricated LLM thoughts, zero fake verifications, zero exposed secrets.

---

## 1. Design System & Visual Identity

Implemented in `FinResolve/frontend/src/app/globals.css`:

- **Core Palette (70% Light Surface / 20% Blue Spectrum / 10% Semantic Status)**:
  - Electric Blue: `#2563EB` (`--fr-electric-blue`)
  - Deep Royal Blue: `#1D4ED8` (`--fr-royal-blue`)
  - Ocean Blue: `#0284C7` (`--fr-ocean-blue`)
  - Sky Blue: `#38BDF8` (`--fr-sky-blue`)
  - Ice Blue: `#EAF3FF` (`--fr-ice-blue`)
  - Cloud White: `#F8FAFF` (`--fr-cloud-white`)
  - Pure White: `#FFFFFF` (`--fr-pure-white`)
  - Deep Navy: `#0B1739` (`--fr-deep-navy`)
  - Muted Slate: `#64748B` (`--fr-muted-slate`)
  - Semantic Status: Emerald `#10B981`, Amber `#F59E0B`, Coral Red `#EF4444`
- **Typography**: `Be Vietnam Pro` loaded via `next/font/google` in both `FinResolve/frontend` and `MiniStore/frontend` for cohesive Vietnamese fintech legibility.
- **Motion System**: GPU-accelerated CSS transforms, opacity, SVG `stroke-dashoffset` directional signal pulses (`.fr-signal-line`, `.fr-signal-line-fast`), counter-rotating telemetry orbits (`.fr-spin-slow`, `.fr-spin-reverse-slow`), ambient glow breathe, and verification ripple — all strictly respecting `@media (prefers-reduced-motion: reduce)`.

---

## 2. Signature Component Architecture

| Component | File | Purpose & Backend Connection |
| --- | --- | --- |
| **Signature Guardian Core** | `FinResolve/frontend/src/components/GuardianCore.tsx` | Pure high-DPI vector SVG + subtle mouse-tilt emblem with luminous shield, neural lattice, and dual orbital rings. Supports all 7 truthful states (`MONITORING`, `ANOMALY_DETECTED`, `INVESTIGATING`, `RECOVERING`, `WAITING_APPROVAL`, `VERIFIED`, `DISCONNECTED`) with zero WebGL overhead. |
| **Living Business Network** | `FinResolve/frontend/src/components/LivingBusinessNetwork.tsx` | Interactive topology hero map placing `GuardianCore` at center surrounded by 7 real business nodes (`Customer`, `Orders`, `Payment`, `Refund`, `Inventory`, `Event Outbox`, `Policy`). Driven strictly by `ExperienceOverviewResponse` real events, anomalies, tool reads, and `VERIFIED_RECOVERY` verdicts. Clicking an anomaly node opens the Incident Detail Drawer. Includes an accessible **Bản đồ mạng / Bảng dữ liệu** toggle. |
| **6-Chapter Scroll Story** | `FinResolve/frontend/src/components/LandingScrollStory.tsx` | Non-scroll-locking `IntersectionObserver` + click-navigable 6-stage walkthrough (*Doanh nghiệp hoạt động → Bất thường xuất hiện → Guardian phát hiện → AI điều tra → Hành động an toàn → Kiểm chứng*), explicitly labeled `Minh họa sản phẩm`. |
| **Cinematic Landing Page** | `FinResolve/frontend/src/app/page.tsx` | Editorial split-screen Hero (*"Your business never sleeps. Neither does FinResolve."* + *"Doanh nghiệp không ngừng vận hành. AI không ngừng bảo vệ."*), right-side Guardian Core connected to 6 business streams, working primary CTA (`Trải nghiệm Guardian` → `/experience`) and secondary CTA (`Khám phá cách hoạt động` → `#story`). |
| **Immersive Command Center** | `FinResolve/frontend/src/components/ExperienceCommandCenter.tsx` | Collapsible minimalist sidebar (`72px` ↔ `236px`), visual hierarchy led by `LivingBusinessNetwork` + Real-Time Narrative Card, compact supporting KPI summary, live Vietnamese timeline with expandable Technical Trace, Operator Auth modal (`X-Operator-Token`), and Focus Mode launcher. |
| **Focus Mode (`Guardian Live Experience`)** | `FinResolve/frontend/src/components/GuardianFocusMode.tsx` | Full-screen judge presentation mode hiding sidebar chrome, centering `LivingBusinessNetwork`, real-time MiniStore stream, Before/After verification summary, humanized AI investigation narrative, and collapsible authorized Sandbox telemetry drawer. |
| **MiniStore Visual Alignment** | `MiniStore/frontend/src/app/layout.tsx`, `globals.css`, `StoreShell.tsx` | Unified `Be Vietnam Pro` typography, Cloud White surface palette (`#F8FAFF`), and subtle `Protected by FinResolve AI` trust badge while preserving MiniStore's e-commerce brand identity. |

---

## 3. Verification & Quality Gate Results

- **`FinResolve/frontend`**:
  - `npm run lint` (`eslint .`): **PASS** (0 warnings, 0 errors)
  - `npm run typecheck` (`tsc --noEmit`): **PASS** (0 errors)
  - Unit tests (`node --experimental-strip-types --test`): **13/13 PASS**
  - `npm run build` (`next build` Turbopack): **PASS** (9/9 routes built cleanly)
- **`MiniStore/frontend`**:
  - `npm run lint` (`eslint .`): **PASS**
  - `npm run typecheck` (`tsc --noEmit`): **PASS**
  - `npm run build` (`next build` Turbopack): **PASS** (10/10 routes built cleanly)
- **Playwright Chromium E2E**:
  - Updated `FinResolve/frontend/e2e/guardian-experience.spec.ts` to verify the new editorial hero (`Doanh nghiệp không ngừng vận hành.`), `Minh họa sản phẩm` label, `Trải nghiệm Guardian` navigation to `/experience`, zero secret leakage, and Mobile `390×844` zero horizontal overflow.
  - Executed via GitHub Actions `Runtime Acceptance (Docker Compose)` on `feat/prompt-15-1-signature-experience`.
- **Local Chrome Screenshot Capture Note (Honest Report)**:
  - Local interactive Chrome DevTools connection was unavailable on the workstation (`DevToolsActivePort` not enabled). Automated real Chromium rendering and viewport overflow verification are enforced via Playwright in GitHub Actions CI.

---

## 4. Remaining Items / Future Polish

- Interactive multi-viewport screenshot capture (`1920×1080`, `1440×900`, `1280×720`, tablet, mobile) can be run directly on any workstation with Chrome Remote Debugging enabled or by saving Playwright screenshots as CI artifacts.
- Further deep storytelling micro-charts (e.g., sparklines for RPS/P95 latency over multiple windows) can be layered into the Business Health panel once multi-window history is exposed by the backend overview API.
