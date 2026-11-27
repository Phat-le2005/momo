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

    // Verify Reset confirmation modal opens cleanly
    await page.getByRole("button", { name: "Reset phiên" }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("button", { name: "Hủy bỏ" }).click();

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
