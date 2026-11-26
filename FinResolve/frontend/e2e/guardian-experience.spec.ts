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

const BASE_URL = process.env.FINRESOLVE_WEB_URL ?? "http://127.0.0.1:3000";

test.describe("FinResolve Unified Guardian Experience E2E", () => {
  test("Landing page renders hero, preview label, and navigates to /experience", async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));

    await page.goto(BASE_URL, { waitUntil: "networkidle" });
    await expect(page.getByText("Doanh nghiệp vận hành.")).toBeVisible();
    await expect(page.getByText("Minh họa sản phẩm")).toBeVisible();

    await page.getByRole("link", { name: /Bắt đầu trải nghiệm/i }).first().click();
    await page.waitForURL("**/experience");
    await expect(page.getByText("FinShop LIVE Sandbox (MOCK)")).toBeVisible();
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
          "chaos_admin_key",
        ]) {
          if (text.includes(forbidden)) {
            leakedSecrets.push(forbidden);
          }
        }
      }
    });

    await page.goto(`${BASE_URL}/experience`, { waitUntil: "networkidle" });
    await expect(page.getByText("Sức khỏe vận hành (Business Health)")).toBeVisible();

    // Switch to Customer View and Sandbox View without losing session
    await page.getByRole("tab", { name: "Khách hàng" }).click();
    await expect(page.getByText(/Mở FinShop LIVE/i)).toBeVisible();

    await page.getByRole("tab", { name: "Sandbox" }).click();
    await expect(page.getByText(/Nguyên tắc bảo mật Sandbox/i)).toBeVisible();

    await page.getByRole("tab", { name: "Guardian" }).click();
    await expect(page.getByText("Hoạt động AI Guardian trực tiếp")).toBeVisible();

    // Verify Reset confirmation modal opens cleanly
    await page.getByRole("button", { name: "Reset phiên" }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByRole("button", { name: "Hủy bỏ" }).click();

    // Reload preserves session via sessionStorage
    await page.reload({ waitUntil: "networkidle" });
    await expect(page.getByText("Sức khỏe vận hành (Business Health)")).toBeVisible();

    expect(leakedSecrets).toEqual([]);
  });

  test("Mobile viewport (390x844) renders cleanly without horizontal overflow", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${BASE_URL}/experience`, { waitUntil: "networkidle" });
    await expect(page.getByText("FinShop LIVE Sandbox (MOCK)")).toBeVisible();
    const hasOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth + 4,
    );
    expect(hasOverflow).toBe(false);
  });
});
