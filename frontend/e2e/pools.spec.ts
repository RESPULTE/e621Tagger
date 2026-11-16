import { expect, test } from "@playwright/test";
import { createReviewProject } from "./project-fixture";

test("browse a pool in sequence and favourite its full search", async ({ page }) => {
  await page.goto("/");
  if ((await (await page.request.get("/api/auth/options")).json()).password_required) {
    await page.getByLabel("Studio password").fill("studio-browser-test-password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  }
  await expect(page.getByRole("button", { name: "Projects", exact: true })).toBeVisible();
  await createReviewProject(page, "pool_navigation");
  await page.getByRole("button", { name: "Open post 999", exact: true }).click();
  const pool = page.getByRole("navigation", { name: /Pool navigation for Studio demo collection/i });
  await expect(pool.getByRole("button", { name: "Previous in pool" })).toBeDisabled();
  await pool.getByRole("button", { name: "Next in pool" }).click();
  await expect(page).toHaveURL(/\/posts\/997$/);
  await expect(pool).toContainText("2 / 3");
  await pool.getByRole("button", { name: "Previous in pool" }).click();
  await expect(page).toHaveURL(/\/posts\/999$/);
  await pool.getByRole("link", { name: "Studio demo collection" }).click();
  await expect(page.getByLabel("Discovery query", { exact: true })).toHaveValue("pool:1");
  await expect(page.getByRole("button", { name: /^Open post / })).toHaveCount(3);
  const queued = page.waitForResponse(r => r.url().endsWith("/api/pools/1/favorite") && r.request().method() === "POST");
  await page.getByRole("button", { name: "Favourite pool", exact: true }).click();
  expect((await (await queued).json()).total).toBe(3);
});
