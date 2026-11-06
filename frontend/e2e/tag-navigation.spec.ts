import { test, expect } from "@playwright/test";
import { createReviewProject } from "./project-fixture";

test("post tags search the current project's main gallery", async ({ page, context }) => {
  await page.goto("/");
  if ((await (await page.request.get("/api/auth/options")).json()).password_required) {
    await page.getByLabel("Studio password").fill("studio-browser-test-password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  }
  await expect(page.getByRole("button", { name: "Projects", exact: true })).toBeVisible();
  await createReviewProject(page, "tag_navigation");
  const gallery = new URL(page.url()).pathname;
  await page.getByRole("button", { name: "Open post 999", exact: true }).click();
  const tag = page.getByRole("region", { name: "Artwork tags" })
    .getByRole("link", { name: "studio demo", exact: true });
  await expect(tag).toHaveAttribute("href", `${gallery}?q=studio_demo`);
  const search = page.waitForRequest(request => {
    const url = new URL(request.url());
    return url.pathname === `/api${gallery}/query` && url.searchParams.get("query") === "studio_demo";
  });
  await tag.click();
  await search;
  await expect(page).toHaveURL(`${gallery}?q=studio_demo`);
  await expect(page.getByLabel("Discovery query", { exact: true })).toHaveValue("studio_demo");
  expect(context.pages()).toHaveLength(1);
});
