import { test, expect, type Page, type Locator } from "@playwright/test";
import { createReviewProject } from "./project-fixture";

async function open(page: Page) {
  await page.goto("/");
  if ((await (await page.request.get("/api/auth/options")).json()).password_required) {
    await page.getByLabel("Studio password").fill("studio-browser-test-password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  }
  await expect(page.getByRole("button", { name: "Projects", exact: true })).toBeVisible();
  await createReviewProject(page, "viewer");
  await expect(page.getByRole("button", { name: "Open post 999", exact: true })).toBeVisible();
}

async function pointer(element: Locator, type: string, outside = false) {
  const box = (await element.boundingBox())!;
  await element.dispatchEvent(type, {
    pointerId: 1, isPrimary: true, pointerType: "touch", button: 0,
    clientX: outside ? 1 : box.x + box.width / 2,
    clientY: outside ? 1 : box.y + box.height / 2,
  });
}

async function doubleTap(page: Page, element: Locator, mobile: boolean) {
  await element.scrollIntoViewIfNeeded();
  const box = (await element.boundingBox())!;
  const x = box.x + box.width / 2, y = box.y + Math.min(60, box.height / 3);
  if (mobile) { await page.touchscreen.tap(x, y); await page.touchscreen.tap(x, y); }
  else await page.mouse.dblclick(x, y, { delay: 70 });
}

test("double tap toggles completed favourites in thumbnails and originals", async ({ page, isMobile }, info) => {
  await open(page);
  const thumb = page.getByRole("button", { name: "Open post 999", exact: true });
  const gallery = page.url();
  const state = async () => (await (await page.request.get("/api/library/posts/999")).json());
  if ((await state()).wanted) { await thumb.press("f"); await expect.poll(async () => (await state()).status).toBe("idle"); }
  await doubleTap(page, thumb, isMobile);
  await expect(thumb.locator(".favorite-paw")).toBeVisible();
  await expect.poll(async () => (await state()).status).toBe("saved");
  expect(page.url()).toBe(gallery);
  const card = thumb.locator("..");
  await expect(card.locator(".quick-labels button")).toHaveCount(2);
  await expect(page.locator(".favorite-button")).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("paw-gallery.png") });
  await thumb.click();
  const media = page.locator(".media-stage");
  await expect(media.locator(".favorite-paw")).toBeVisible();
  await expect(page.locator(".artwork-save")).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("paw-viewer.png") });
  await doubleTap(page, media, isMobile);
  await expect(media.locator(".favorite-paw")).toHaveCount(0);
  await expect.poll(async () => { const s = await state(); return [s.wanted, s.present, s.status]; }).toEqual([false, false, "idle"]);
  await expect(page.getByRole("button", { name: "Positive", exact: true })).toHaveAttribute("aria-pressed", "false");
  await doubleTap(page, media, isMobile);
  await expect(media.locator(".favorite-paw")).toBeVisible();
  await expect.poll(async () => (await state()).status).toBe("saved");
  await page.reload();
  await expect(media.locator(".favorite-paw")).toBeVisible();
  await doubleTap(page, media, isMobile);
  await expect.poll(async () => (await state()).status).toBe("idle");
});

test("a second double tap queues removal even before the save response returns", async ({ page, isMobile }) => {
  await open(page);
  await page.getByRole("button", { name: "Open post 997", exact: true }).click();
  const media = page.locator(".media-stage");
  await expect(media.locator("img")).toBeVisible();
  let wanted = false;
  const intents: boolean[] = [];
  const state = () => ({ post_id: 997, wanted, present: false, version: intents.length, status: wanted ? "downloading" : "idle", error: null, updated_at: Date.now() });
  await page.route("**/api/library/posts/997", route => route.fulfill({ json: state() }));
  await page.route("**/api/projects/*/library/997", async route => {
    intents.push(route.request().postDataJSON().wanted);
    if (intents.length === 1) await new Promise(resolve => setTimeout(resolve, 1400));
    wanted = route.request().postDataJSON().wanted;
    await route.fulfill({ json: state() });
  });
  await doubleTap(page, media, isMobile);
  await doubleTap(page, media, isMobile);
  await expect.poll(() => intents).toEqual([true, false]);
  await expect(media.locator(".favorite-paw")).toHaveCount(0);
  await page.unrouteAll({ behavior: "wait" });
});

test("artist-first viewer, full tags, reversible reviews and scroll-aware dock", async ({ page }, info) => {
  await open(page);
  await expect(page.getByRole("link", { name: "Dataset export" })).toBeVisible();
  await page.getByRole("button", { name: "Open post 999", exact: true }).click();
  await expect(page.getByRole("heading", { name: "studio demo", level: 1 })).toBeVisible();
  const originalURL = page.url();
  await expect(page.locator(".artwork-facts")).toContainText("Score");
  await expect(page.locator(".artwork-facts")).toContainText("Rating");
  const tags = page.getByRole("region", { name: "Artwork tags" });
  await expect(tags.locator("details")).toHaveCount(0);
  const artwork = await (await page.request.get(`/api${new URL(page.url()).pathname.replace("/posts/", "/candidates/")}`)).json();
  expect(await tags.locator("a").count()).toBe(Object.values(artwork.tags as Record<string, string[]>).flat().length);
  for (const removed of ["Clear review", "Undo this post", "Find More Like This"])
    await expect(page.getByRole("button", { name: removed, exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "View on e621" })).toHaveCount(0);
  await expect(page.getByLabel("Hide tags", { exact: true })).toHaveCount(0);
  const positive = page.getByRole("button", { name: "Positive", exact: true });
  const negative = page.getByRole("button", { name: "Negative", exact: true });
  for (const button of [positive, negative]) {
    await button.click();
    await expect(page.getByText("Saved", { exact: true })).toBeVisible();
    await expect(button).toHaveAttribute("aria-pressed", "true");
    expect(page.url()).toBe(originalURL);
    await button.click();
    await expect(page.getByText("Unreviewed", { exact: true })).toBeVisible();
    await expect(button).toHaveAttribute("aria-pressed", "false");
  }
  await positive.click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  await negative.click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  await expect(positive).toHaveAttribute("aria-pressed", "false");
  await expect(negative).toHaveAttribute("aria-pressed", "true");
  await page.reload();
  await expect(negative).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".artwork-save, .favorite-button")).toHaveCount(0);
  const dock = page.getByRole("group", { name: "Review artwork" });
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await expect(dock).not.toHaveClass(/is-collapsed/);
  await page.evaluate(() => window.scrollBy(0, -100));
  await expect(dock).toHaveClass(/is-collapsed/);
  await expect(dock).toHaveCSS("opacity", "0");
  await page.evaluate(() => window.scrollBy(0, 80));
  await expect(dock).not.toHaveClass(/is-collapsed/);
  await expect(dock).toHaveCSS("opacity", "1");
  const dockBox = (await dock.boundingBox())!;
  expect(dockBox.y + dockBox.height).toBeLessThan(page.viewportSize()!.height);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath("viewer-tags.png") });
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(dock).toHaveCSS("opacity", "1");
  await page.screenshot({ path: info.outputPath("viewer.png") });
});

test("one-second hold progresses, releases once, and cancels without opening", async ({ page }) => {
  await open(page);
  const url = page.url();
  const card = page.getByRole("button", { name: "Open post 999", exact: true });
  await card.scrollIntoViewIfNeeded();
  let searches = 0;
  page.on("request", (r) => { if (r.method() === "POST" && /\/similar$/.test(r.url())) searches++; });
  await pointer(card, "pointerdown");
  await expect(card.locator(".hold-indicator")).toBeAttached();
  await page.waitForTimeout(300); // A release before the specified 1s threshold must not search.
  await expect(card).not.toHaveClass(/similarity-armed/);
  await pointer(card, "pointerup");
  await expect(card.locator(".hold-indicator")).toHaveCount(0);
  expect(searches).toBe(0);
  expect(page.url()).toBe(url);
  for (const cancellation of ["outside", "scroll", "pointercancel", "movement"]) {
    await pointer(card, "pointerdown");
    await expect(card).toHaveClass(/similarity-armed/);
    expect(searches).toBe(0); // Timer alone never submits.
    if (cancellation === "scroll") await page.evaluate(() => window.dispatchEvent(new Event("scroll")));
    if (cancellation === "pointercancel") await pointer(card, "pointercancel");
    if (cancellation === "movement") await pointer(card, "pointermove", true);
    await pointer(card, "pointerup", cancellation === "outside");
    await expect(card.locator(".hold-indicator")).toHaveCount(0);
    expect(searches).toBe(0);
    expect(page.url()).toBe(url);
  }
  await pointer(card, "pointerdown");
  await expect(card).toHaveClass(/similarity-armed/);
  expect(searches).toBe(0);
  await pointer(card, "pointerup");
  await expect(page.getByRole("link", { name: "Open results" })).toBeVisible();
  expect(searches).toBe(1);
  expect(page.url()).toBe(url);
  await page.getByRole("link", { name: "Open results" }).click();
  await expect(page.locator(".selected-image")).toContainText("studio demo");
  await expect(page.getByText(/Post #/)).toHaveCount(0);
});

test("similarity names distinguish artworks by the same artist", async ({ page }) => {
  await open(page);
  const projectName = await page.locator(".project-heading h1").innerText();
  for (const id of [999, 998]) {
    await page.getByRole("button", { name: `Open post ${id}`, exact: true }).press("Shift+S");
    await expect(page.getByRole("link", { name: "Open results" })).toHaveCount(id === 999 ? 1 : 2);
  }
  await page.getByRole("tab", { name: "Similarity", exact: true }).click();
  await expect(page.locator('.similarity-folder[data-source-id="999"] > button').first()).toHaveText(`${projectName} · studio demo · 1`);
  await expect(page.locator('.similarity-folder[data-source-id="998"] > button').first()).toHaveText(`${projectName} · studio demo · 2`);
  await page.reload();
  await expect(page.locator('.similarity-folder[data-source-id="998"] > button').first()).toHaveText(`${projectName} · studio demo · 2`);
  await expect(page.getByText("Search details", { exact: false })).toHaveCount(0);
});

