/**
 * Reproducible Playwright E2E Browser Acceptance Spec (Prompt 14.8 Part C).
 *
 * Run against the live 9-service Docker Compose stack:
 *   FINRESOLVE_WEB_URL=http://127.0.0.1:3000 npx playwright test e2e/guardian-experience.spec.ts
 *
 * Verifies:
 * - Landing page -> Start Experience -> Unified Guardian Command Center
 * - Session initialization & restoration across page reload
 * - Customer View, Sandbox View, Reset confirmation dialog
 * - Zero exposed credentials / secrets in browser API responses
 * - Desktop (1280x800) and Mobile (390x844) viewport usability
 */

import { expect, test } from "@playwright/test";

const BASE_URL = process.env.FINRESOLVE_WEB_URL ?? "http://localhost:3000";

test.describe("FinResolve Unified Guardian Experience E2E", () => {
  test("Anomaly details render structured evidence and legacy references without crashing", async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));
    const timestamp = new Date().toISOString();
    await page.route("**/api/v1/experience/bootstrap", (route) => route.fulfill({
      json: {
        status: "READY",
        restored: false,
        session: {
          session_id: "sess_browser_regression",
          token: "dsk_browser_regression_fixture",
          status: "ACTIVE",
          expires_at: new Date(Date.now() + 3_600_000).toISOString(),
          world_epoch: 1,
          workspace_role: "CONTROLLER",
        },
        sandbox: {
          environment_name: "FinShop LIVE Sandbox (MOCK)",
          workspace_role: "CONTROLLER",
          merchant_id: "MERCHANT_FINSHOP_01",
          commerce_session_id: "CSESS_FINSHOP_DEFAULT_01",
          commerce_world_epoch: 1,
          binding_id: "BIND-BROWSER-REGRESSION",
          connectivity_status: "CONNECTED",
          storefront_url: "http://localhost:3100",
          vnpay_enabled: false,
        },
        guardian: {
          worker_healthy: true,
          supervisor_status: "MONITORING",
          model_mode: "DEMO_RULES",
          model_provider: "DEMO_RULES",
          model_name: "deterministic-demo",
          autopilot_enabled: true,
        },
      },
    }));
    await page.route("**/api/v1/experience/overview", (route) => route.fulfill({
      json: {
        session_id: "sess_browser_regression",
        world_epoch: 1,
        workspace_role: "CONTROLLER",
        updated_at: timestamp,
        guardian: {
          visual_state: "MONITORING",
          supervisor_status: "MONITORING",
          worker_healthy: true,
          connectivity_status: "CONNECTED",
          model_mode: "DEMO_RULES",
          model_name: "deterministic-demo",
          queued_jobs: 0,
          last_scan_at: timestamp,
        },
        kpis: {
          orders_observed: 1,
          payments_observed: 1,
          active_anomalies: 1,
          verified_recoveries: 0,
          pending_approvals: 0,
          total_anomalies_detected: 1,
          total_cases: 0,
        },
        business_health: {
          telemetry_available: true,
          has_samples: true,
          window_id: "WINDOW-BROWSER-REGRESSION",
          rps: 0.1,
          p95_latency_ms: 12,
          error_rate: 0,
          outbox_lag: 0,
          unsynced_payments: 0,
          baseline_status: "WARMING_UP",
          baseline_message_vi: "Guardian đang thiết lập trạng thái hoạt động bình thường.",
          baselines: [],
        },
        anomalies: [{
          id: "ANOM-BROWSER-REGRESSION",
          detector_code: "D03",
          anomaly_type: "ORDER_STALLED",
          severity: "MEDIUM",
          lifecycle_state: "OBSERVED",
          affected_order_id: "ORD-REGRESSION-01",
          affected_payment_id: null,
          linked_case_id: null,
          rationale: "Đơn mẫu đang chờ xử lý.",
          verification_verdict: null,
          recovery_source: null,
          first_observed_at: timestamp,
          last_observed_at: timestamp,
          evidence_refs: [{
            source: "MINI_STORE_COMMERCE_HTTP",
            endpoint: "/api/v1/service/orders/ORD-REGRESSION-01",
            status: "CONFIRMED",
            stalled_seconds: 1801,
          }, "EVIDENCE-LEGACY-001"],
        }],
        approvals: [],
        recovery_commands: [],
        recent_business_events: [],
        timeline: [],
      },
    }));

    await page.setViewportSize({ width: 900, height: 800 });
    await page.goto(`${BASE_URL}/experience`, { waitUntil: "domcontentloaded" });
    await page.getByRole("button", { name: /ORDER_STALLED/ }).click();
    const dialog = page.getByRole("dialog", { name: "Chi tiết sự cố" });
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("MINI_STORE_COMMERCE_HTTP");
    await expect(dialog).toContainText("/api/v1/service/orders/ORD-REGRESSION-01");
    await expect(dialog).toContainText("CONFIRMED");
    await expect(dialog).toContainText("EVIDENCE-LEGACY-001");
    await dialog.getByRole("button", { name: "Đóng", exact: true }).click();
    await expect(dialog).not.toBeVisible();
    await expect(page.getByRole("button", { name: /ORDER_STALLED/ })).toBeVisible();
    expect(errors).toEqual([]);
  });

  test("Landing page renders hero, preview label, and navigates to /experience", async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));

    await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
    await expect(page.getByText("Doanh nghiệp vận hành.").first()).toBeVisible({ timeout: 15000 });
    await expect(page.getByText("Minh họa sản phẩm").first()).toBeVisible();

    await page.getByRole("link", { name: /Trải nghiệm ngay/i }).first().click();
    await page.waitForURL("**/experience", { timeout: 15000 });
    await expect(page.getByText("FinShop LIVE Sandbox (MOCK)").first()).toBeVisible({
      timeout: 15000,
    });
    expect(errors).toEqual([]);
  });

  test("Command Center bootstraps session, restores on reload, switches views, and exposes zero secrets", async ({
    page,
  }) => {
    const leakedSecrets: string[] = [];
    page.on("response", async (resp) => {
      if (resp.url().includes("/api/v1/experience/")) {
        const text = await resp.text().catch(() => "");
        for (const forbidden of [
          "GEMINI_API_KEY",
          "COMMERCE_WRITE_KEY",
          "COMMAND_AUTH_SIGNING_SECRET",
          "EXPERIENCE_OPERATOR_TOKEN",
          "chaos_admin_key",
        ]) {
          if (text.includes(forbidden)) {
            leakedSecrets.push(forbidden);
          }
        }
      }
    });

    await page.goto(`${BASE_URL}/experience`, { waitUntil: "domcontentloaded" });
    await expect(page.getByText("Sức khỏe vận hành (Business Health)").first()).toBeVisible({
      timeout: 20000,
    });

    // Switch to Customer View and Sandbox View without losing session
    await page.getByRole("tab", { name: "Khách hàng" }).click();
    await expect(page.getByText(/Mở FinShop LIVE/i).first()).toBeVisible();

    await page.getByRole("tab", { name: "Sandbox" }).click();
    await expect(page.getByText(/Nguyên tắc bảo mật Sandbox/i).first()).toBeVisible();

    await page.getByRole("tab", { name: "Guardian" }).click();
    await expect(page.getByText("Hoạt động AI Guardian trực tiếp").first()).toBeVisible();

    // Verify Reset confirmation modal opens cleanly when CONTROLLER, or is disabled in READ_ONLY_PREVIEW
    const resetBtn = page.getByRole("button", { name: "Reset phiên" });
    const isDisabled = await resetBtn.isDisabled();
    if (!isDisabled) {
      await resetBtn.click();
      await expect(page.getByRole("dialog")).toBeVisible();
      await page.getByRole("button", { name: "Hủy bỏ" }).click();
    } else {
      await expect(page.getByRole("button", { name: /Kích hoạt Operator/i })).toBeVisible();
    }

    // Reload preserves session via sessionStorage
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(page.getByText("Sức khỏe vận hành (Business Health)").first()).toBeVisible({
      timeout: 20000,
    });

    expect(leakedSecrets).toEqual([]);
  });

  test("Mobile viewport (390x844) renders cleanly without horizontal overflow", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${BASE_URL}/experience`, { waitUntil: "domcontentloaded" });
    await expect(page.getByText("FinShop LIVE Sandbox (MOCK)").first()).toBeVisible({
      timeout: 20000,
    });
    const hasOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth + 4,
    );
    expect(hasOverflow).toBe(false);
  });
});
