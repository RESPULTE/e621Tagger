import { test, expect, type Page } from "@playwright/test";
import { createReviewProject } from "./project-fixture";

async function open(page: Page) {
  await page.goto("/");
  if ((await (await page.request.get("/api/auth/options")).json()).password_required) {
    await page.getByLabel("Studio password").fill("studio-browser-test-password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  }
  await expect(page.getByRole("button", { name: "Projects", exact: true })).toBeVisible();
  await createReviewProject(page, "media_performance");
  await expect(page.getByRole("button", { name: "Open post 999", exact: true }).locator("img")).toBeVisible();
}

test("gallery and blobs survive viewer navigation, including scroll offset", async ({ page }) => {
  await open(page);
  const gallery = page.getByLabel("Candidate gallery", { exact: true });
  await gallery.evaluate(el => { el.scrollTop = 280; });
  let name: string | null | undefined;
  await expect.poll(async () => {
    name = await gallery.evaluate(el => {
      const bounds = el.getBoundingClientRect();
      return [...el.querySelectorAll<HTMLButtonElement>("button.thumbnail-link")]
        .find(button => {
          const rect = button.getBoundingClientRect();
          const center = rect.top + rect.height / 2;
          return center >= bounds.top && center <= bounds.bottom;
        })?.getAttribute("aria-label");
    });
    return name;
  }).toBeTruthy();
  const button = page.getByRole("button", { name: name!, exact: true });
  await expect(button.locator("img")).toBeVisible();
  await button.focus();
  // Finish the cold loads before measuring reuse; interrupted first-time
  // requests are correctly cancelled and cannot be counted as warm cache hits.
  await expect(gallery.locator(".thumbnail-placeholder")).toHaveCount(0);
  const before = await gallery.evaluate(el => el.scrollTop);
  expect(before).toBeGreaterThan(0);
  let thumbnails = 0, queries = 0;
  page.on("request", request => {
    if (/\/media\/\d+\/preview/.test(request.url())) thumbnails++;
    if (/\/query\?/.test(request.url())) queries++;
  });
  // Revalidation is held so this proves the cached candidate renders independently.
  let release!: () => void;
  const waiting = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/projects/*/candidates/*", async route => {
    await waiting;
    await route.continue();
  });
  try {
    await button.press("Enter");
    await expect(page.locator(".media-stage")).toBeVisible();
    await page.getByRole("link", { name: "Thumbnail gallery", exact: true }).click();
    await expect(gallery).toBeVisible();
    await expect.poll(() => gallery.evaluate(el => el.scrollTop)).toBeCloseTo(before, 0);
    await expect(page.getByRole("button", { name: /^Open post/ }).first().locator("img")).toBeVisible();
    expect(queries).toBe(0);
    expect(thumbnails).toBe(0);
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
  }
});

test("sample renders before an explicitly requested original and survives its delay", async ({ page }) => {
  await page.route("**/api/projects/*/query?*", async route => {
    const response = await route.fetch();
    const body = await response.json();
    body.items = body.items.map((item: { has_sample: boolean }) => ({ ...item, has_sample: true }));
    await route.fulfill({ response, json: body });
  });
  await open(page);
  await page.route("**/api/projects/*/candidates/999", async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), has_sample: true } });
  });
  let originals = 0;
  let release!: () => void;
  const waiting = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/media/999/original?*", async route => {
    originals++;
    await waiting;
    await route.continue();
  });
  try {
    await page.getByRole("button", { name: "Open post 999", exact: true }).press("Enter");
    await expect(page.getByAltText("Sample post 999", { exact: true })).toBeVisible();
    expect(originals).toBe(0);
    await page.getByRole("button", { name: "Load original", exact: true }).click();
    await expect.poll(() => originals).toBe(1);
    await expect(page.getByAltText("Sample post 999", { exact: true })).toBeVisible();
    release();
    await expect(page.getByAltText("Original post 999", { exact: true })).toBeVisible();
    await expect(page.getByAltText("Sample post 999", { exact: true })).toHaveCount(0);
  } finally {
    release();
    await page.unrouteAll({ behavior: "wait" });
  }
});

test("unsupported WebM chooses the MP4 alternate before a failing source request", async ({ page }) => {
  let originals = 0;
  page.on("request", request => {
    if (/\/api\/media\/1000\/original(?:\?|$)/.test(request.url())) originals++;
  });
  await page.addInitScript(() => {
    const original = HTMLMediaElement.prototype.canPlayType;
    HTMLMediaElement.prototype.canPlayType = function(type) {
      return type.includes("webm") ? "" : original.call(this, type);
    };
  });
  await open(page);
  await page.goto(page.url().replace(/\?.*$/, "") + "/posts/1000");
  const video = page.locator("video");
  await expect(video).toHaveAttribute("src", /\/video_mp4\?/);
  expect(originals).toBe(0);
});
