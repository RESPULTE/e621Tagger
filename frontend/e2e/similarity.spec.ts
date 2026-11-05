import { createReviewProject } from "./project-fixture";
import { test, expect, type Page, type Locator } from "@playwright/test";

test("official query filter survives reload, filters new pages, clears and reports errors", async ({ page }) => {
  await open(page);
  await page.route("**/api/projects/*/similar", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await route.continue({ postData: JSON.stringify({ ...route.request().postDataJSON(), limit: 2 }) });
  });
  const card = page.getByRole("button", { name: "Open post 999", exact: true });
  await card.focus();
  await card.press("Shift+S");
  await page.getByRole("link", { name: "Open results", exact: true }).click();
  await expect(page.getByRole("button", { name: "Open post 998", exact: true })).toBeVisible();
  const filter = page.getByRole("combobox", { name: "Filter similar images", exact: true });
  await filter.fill("rating:s");
  await page.getByRole("button", { name: "Apply filter", exact: true }).click();
  await expect(page.getByText("No loaded matches fit this query.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Open post 998", exact: true })).toHaveCount(0);
  expect(new URL(page.url()).searchParams.get("similar_q")).toBe("rating:s");
  await page.getByRole("button", { name: "Load more matches", exact: true }).click();
  await expect(page.getByRole("button", { name: "Open post 996", exact: true })).toBeVisible();
  await page.reload();
  await expect(filter).toHaveValue("rating:s");
  await expect(page.getByRole("button", { name: "Open post 996", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Clear filter", exact: true }).click();
  await expect(filter).toHaveValue("");
  await expect(page.getByRole("button", { name: "Open post 998", exact: true })).toBeVisible();
  await page.route("**/api/projects/*/similar/*?*", async (route) => {
    if (new URL(route.request().url()).searchParams.get("query") === "score:>999")
      return route.fulfill({ status: 503, json: { detail: "e621 is busy; retry this operation later" } });
    return route.continue();
  });
  await filter.fill("score:>999");
  await page.getByRole("button", { name: "Apply filter", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "e621 is busy" })).toBeVisible();
  await expect(page.getByText("No loaded matches fit this query.", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Open post 998", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Clear filter", exact: true }).click();
  await expect(page.getByRole("button", { name: "Open post 998", exact: true })).toBeVisible();
});

test("uploaded reference, binary review, skip and repeat search", async ({ page }) => {
  await open(page);
  const projectPath = new URL(page.url()).pathname;
  const pid = projectPath.split("/").at(-1);
  const preview = await page.request.get("/api/media/999/preview");
  expect(preview.ok()).toBe(true);
  await page.getByRole("tab", { name: "Similarity", exact: true }).click();
  await page.locator(".reference-tools > summary").click();
  await page.getByLabel("Reference image", { exact: true }).setInputFiles({
    name: "reference.png", mimeType: "image/png", buffer: await preview.body(),
  });
  await page.getByRole("button", { name: "Search reference image", exact: true }).click();
  await expect(page.locator(".selected-image")).toContainText("Uploaded reference");
  await expect(page.getByRole("button", { name: "Open post 999", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /unsure/i })).toHaveCount(0);
  await page.getByRole("button", { name: "Open post 999", exact: true }).click();
  await expect(page.getByRole("button", { name: "Positive", exact: true })).toBeVisible();
  let reviewWrites = 0;
  const observe = (request: { method(): string; url(): string }) => {
    if (["POST", "PATCH", "PUT", "DELETE"].includes(request.method()) && /\/labels\/|\/library\//.test(request.url())) reviewWrites++;
  };
  page.on("request", observe);
  await page.keyboard.press("3");
  await page.getByRole("button", { name: "Next / skip →", exact: true }).click();
  await expect(page.getByLabel("Post 998 media; hold to find similar", { exact: true })).toBeVisible();
  expect(reviewWrites).toBe(0);
  expect((await (await page.request.get(`/api/projects/${pid}`)).json()).counts.total).toBe(0);
  await page.reload(); // search evidence survives reload via the URL; no fabricated source ID
  await expect(page.getByRole("button", { name: "Negative", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Negative", exact: true }).click();
  await expect(page).toHaveURL(/posts\/998/);
  await expect.poll(async () => (await (await page.request.get(`/api/projects/${pid}/candidates/998`)).json()).state).toBe("negative");
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Negative", exact: true }).click();
  await expect(page.getByText("Unreviewed", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Negative", exact: true }).click();
  await expect(page.getByText("Saved", { exact: true })).toBeVisible();
  await page.locator(".media-stage").press("Shift+S");
  await expect(page.getByRole("link", { name: "Open results", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Open results", exact: true }).click();
  await page.locator(".reference-tools > summary").click();
  await page.getByLabel("Source post ID", { exact: true }).fill("999");
  await page.getByRole("button", { name: "Search post image", exact: true }).click();
  await expect(page.locator('.similarity-folder[data-source-id="999"]').getByRole("button", { name: "Refresh search", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  page.off("request", observe);
});

async function open(page: Page) {
  await page.goto("/");
  const options = await (await page.request.get("/api/auth/options")).json();
  if (options.password_required) {
    await page
      .getByLabel("Studio password")
      .fill("studio-browser-test-password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  }
  await expect(
    page.getByRole("button", { name: "Projects", exact: true }),
  ).toBeVisible();
  expect(
    (await (await page.request.get("/api/auth/session")).json()).mock,
  ).toBe(true);
  await createReviewProject(page, "similarity");
  await expect(
    page.getByRole("button", { name: "Open post 999", exact: true }),
  ).toBeVisible();
}

test("image controls preserve a usable results area on every viewport", async ({ page }) => {
  await open(page);
  const card = page.getByRole("button", { name: "Open post 999", exact: true });
  await card.focus();
  await card.press("Shift+S");
  await page.getByRole("link", { name: "Open results", exact: true }).click();
  await expect(page.getByRole("button", { name: "Open post 998", exact: true })).toBeVisible();
  await expect(page.getByText("Search details", { exact: false })).toHaveCount(0);
  const tools = page.locator(".reference-tools");
  await expect(tools).not.toHaveAttribute("open", "");
  for (const expanded of [false, true]) {
    if (expanded) await page.locator(".reference-tools > summary").click();
    const bounds = await page.getByLabel("Candidate gallery", { exact: true }).boundingBox();
    const viewport = page.viewportSize()!;
    expect(bounds).not.toBeNull();
    expect(bounds!.height).toBeGreaterThanOrEqual(viewport.height * 0.4);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height + 1);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
});

test("load more appends ranked images and refresh does not shuffle them", async ({ page }) => {
  await open(page);
  await page.route("**/api/projects/*/similar", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await route.continue({ postData: JSON.stringify({ ...route.request().postDataJSON(), limit: 2 }) });
  });
  const card = page.getByRole("button", { name: "Open post 999", exact: true });
  await card.focus();
  await card.press("Shift+S");
  await page.getByRole("link", { name: "Open results", exact: true }).click();
  const folder = page.locator('.similarity-folder[data-source-id="999"]');
  const url = new URL(page.url());
  const detailPath = `/api${url.pathname}/similar/${url.searchParams.get("search")}`;
  await expect(folder.getByRole("button", { name: "Load more matches", exact: true })).toBeVisible();
  const controlsBox = await page.getByLabel("Image search controls", { exact: true }).boundingBox();
  const moreBox = await folder.getByRole("button", { name: "Load more matches", exact: true }).boundingBox();
  expect(moreBox!.y + moreBox!.height).toBeLessThanOrEqual(controlsBox!.y + controlsBox!.height);
  const initial = (await (await page.request.get(detailPath)).json()).results.map((r: { post_id: number }) => r.post_id);
  expect(initial).toEqual([998, 997]);
  await folder.getByRole("button", { name: "Load more matches", exact: true }).click();
  await expect.poll(async () => (await (await page.request.get(detailPath)).json()).results.length).toBe(4);
  await expect(folder.getByRole("button", { name: "Refresh search", exact: true })).toBeVisible();
  const expanded = (await (await page.request.get(detailPath)).json()).results.map((r: { post_id: number }) => r.post_id);
  expect(expanded).toEqual([998, 997, 996, 995]);
  await folder.getByRole("button", { name: "Refresh search", exact: true }).click();
  await expect.poll(async () => (await (await page.request.get(detailPath)).json()).results.map((r: { post_id: number }) => r.post_id)).toEqual(initial);
  for (let i = 0; i < 3; i++) {
    await folder.getByRole("button", { name: "Load more matches", exact: true }).click();
    await expect.poll(async () => (await (await page.request.get(detailPath)).json()).status).toBe("done");
  }
  const completed = await (await page.request.get(detailPath)).json();
  expect(completed.results).toHaveLength(7);
  expect(new Set(completed.results.map((r: { post_id: number }) => r.post_id)).size).toBe(7);
  expect(completed.has_more).toBe(false);
  await expect(folder.getByRole("button", { name: "Load more matches", exact: true })).toHaveCount(0);
  await expect(page.getByText("No more matches in this result set.", { exact: true })).toBeAttached();
});

test("history keeps the newest 20 searches in vertical rows", async ({ page }, testInfo) => {
  await open(page);
  const card = page.getByRole("button", { name: "Open post 999", exact: true });
  await card.focus();
  await card.press("Shift+S");
  await page.getByRole("link", { name: "Open results", exact: true }).click();
  await expect(page.locator('.similarity-folder[data-source-id="999"] > button').first()).toBeVisible();
  const path = `/api${new URL(page.url()).pathname}/similar`;
  const original = (await (await page.request.get(path)).json()).items[0];
  await page.route("**/api/projects/*/similar", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    await route.fulfill({ json: {
      available: true, message: null, corpus_size: 8,
      items: Array.from({ length: 25 }, (_, i) => ({
        ...original, id: i === 24 ? original.id : `history-${i}`,
        source_post_id: 975 + i, unfiltered_count: 7,
      })),
    } });
  });
  const rows = page.locator(".similarity-folder");
  await expect(rows).toHaveCount(20);
  await expect(rows.first().locator("button").first()).toBeVisible();
  await expect(rows.last().locator("button").first()).toBeAttached();
  await expect(rows.first().getByText("7 matches", { exact: true })).toBeAttached();
  expect(await rows.evaluateAll((elements) => elements.every((element, i) =>
    i === 0 || element.getBoundingClientRect().top >= elements[i - 1].getBoundingClientRect().bottom
  ))).toBe(true);
  expect(await page.locator(".similarity-folder-list").evaluate((element) =>
    element.scrollHeight > element.clientHeight && element.scrollWidth <= element.clientWidth
  )).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("similarity-history.png") });
  await page.getByText("Recent searches", { exact: false }).click();
  await expect(page.getByRole("combobox", { name: "Filter similar images" })).toBeVisible();
  await expect(page.getByLabel("Candidate gallery", { exact: true })).toBeVisible();
});

async function hold(
  page: Page,
  element: Locator,
  mobile: boolean,
  release = true,
) {
  await element.scrollIntoViewIfNeeded();
  const box = (await element.boundingBox())!;
  const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  if (mobile) {
    await element.dispatchEvent("pointerdown", {
      pointerId: 1,
      isPrimary: true,
      pointerType: "touch",
      button: 0,
      clientX: point.x,
      clientY: point.y,
    });
  } else {
    await page.mouse.move(point.x, point.y);
    await page.mouse.down();
  }
  await expect(element).toHaveClass(/similarity-armed/);
  if (release) {
    if (mobile)
      await element.dispatchEvent("pointerup", {
        pointerId: 1,
        isPrimary: true,
        pointerType: "touch",
        button: 0,
        clientX: point.x,
        clientY: point.y,
      });
    else await page.mouse.up();
  }
}

test("long press queues in place, reviews results and opens full post", async ({
  page,
  isMobile,
}) => {
  await open(page);
  const origin = page.url();
  const card = page.getByRole("button", { name: "Open post 999", exact: true });
  await hold(page, card, isMobile);
  await expect(
    page.getByRole("link", { name: "Open results", exact: true }),
  ).toBeVisible();
  expect(page.url()).toBe(origin);
  await page.getByRole("link", { name: "Open results", exact: true }).click();
  await expect(
    page.getByRole("tab", { name: "Similarity", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(
    page.locator('.similarity-folder[data-source-id="999"] > button').first(),
  ).toBeVisible();
  const negative = page.getByRole("button", {
    name: "Mark post 998 negative",
    exact: true,
  });
  await expect(negative).toBeVisible();
  const savedReview = page.waitForResponse(
    (response) =>
      response.request().method() === "PATCH" &&
      response.url().includes("/labels/998"),
  );
  await negative.click();
  await expect(negative).toHaveAttribute("aria-pressed", "true");
  const reviewResponse = await savedReview;
  expect(reviewResponse.status()).toBe(200);
  expect((await reviewResponse.json()).state).toBe("negative");
  await expect(page.getByRole("progressbar")).toHaveCount(0);
  await page.reload();
  await expect(negative).toHaveAttribute("aria-pressed", "true");
  await page
    .getByRole("button", { name: "Open post 998", exact: true })
    .click();
  await expect(
    page.getByLabel("Post 998 media; hold to find similar", { exact: true }),
  ).toBeVisible();
  expect(new URL(page.url()).searchParams.get("seed")).toBe("999");
  const copied = page.url();
  await page.evaluate(() => history.replaceState(null, "", location.href));
  await page.goto(copied); // No React Router navigation state.
  const edit = page.waitForRequest(
    (r) => r.method() === "PATCH" && r.url().includes("/labels/998"),
  );
  await page.getByRole("button", { name: /Positive/ }).click();
  expect((await edit).postDataJSON().similarity).toEqual({
    seed_post_id: 999,
    index_revision: "synthetic",
    search_id: new URL(copied).searchParams.get("similar_search"),
  });
  await expect(page.getByRole("button", { name: /Positive/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(
    page.getByRole("button", { name: "Find similar posts", exact: true }),
  ).toHaveCount(0);
  const media = page.getByLabel("Post 998 media; hold to find similar", {
    exact: true,
  });
  await hold(page, media, isMobile);
  await expect(
    page.getByRole("link", { name: "Open results", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Negative", exact: true })
    .click();
  await expect(page.getByRole("button", { name: /Negative/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
});

test("moving cancels a hold and keyboard activation deduplicates sources", async ({
  page,
  isMobile,
}) => {
  await open(page);
  const card = page.getByRole("button", { name: "Open post 999", exact: true });
  await hold(page, card, isMobile, false);
  await card.dispatchEvent("pointercancel", { pointerId: 1 });
  if (!isMobile) await page.mouse.up();
  await expect(card).not.toHaveClass(/similarity-armed/);
  await expect(
    page.getByRole("link", { name: "Open results", exact: true }),
  ).toHaveCount(0);
  await card.focus();
  await card.press("Shift+S");
  await expect(
    page.getByRole("link", { name: "Open results", exact: true }),
  ).toBeVisible();
  await card.press("Shift+S");
  await page.getByRole("link", { name: "Open results", exact: true }).click();
  await expect(
    page.locator('.similarity-folder[data-source-id="999"] > button').first(),
  ).toHaveCount(1);
});

test("completed downloads hide their progress bar", async ({ page }) => {
  await open(page);
  const media = page.getByRole("button", { name: "Open post 998", exact: true });
  if (!(await (await page.request.get("/api/library/posts/998")).json()).wanted) await media.press("f");
  await page.getByRole("tab", { name: "Downloads", exact: true }).click();
  await expect(
    page.getByText("Completed", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByText(/^0 active/)).toBeVisible();
  await expect(page.getByRole("progressbar")).toHaveCount(0);
});

test("switching source folders while previews load keeps the selected results", async ({
  page,
}) => {
  await open(page);
  for (const id of [999, 998]) {
    const card = page.getByRole("button", {
      name: `Open post ${id}`,
      exact: true,
    });
    await card.focus();
    await card.press("Shift+S");
  }
  await expect(
    page.getByRole("link", { name: "Open results", exact: true }),
  ).toHaveCount(2);
  await page.route("**/api/media/*/preview*", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 400));
    await route.continue().catch(() => {});
  });
  await page.getByRole("tab", { name: "Similarity", exact: true }).click();
  for (const id of [999, 998, 999, 998]) {
    await page
      .locator(`.similarity-folder[data-source-id="${id}"] > button`).first()
      .click();
  }
  await expect(
    page.locator('.similarity-folder[data-source-id="998"] > button').first(),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByRole("button", { name: "Open post 999", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Open post 998", exact: true }),
  ).toHaveCount(0);
  // A shared nearest neighbor remains visible for each independently selected image.
  await expect(page.getByRole("button", { name: "Open post 997", exact: true })).toBeVisible();
  await page.locator('.similarity-folder[data-source-id="999"] > button').first().click();
  await expect(page.getByRole("button", { name: "Open post 997", exact: true })).toBeVisible();
});

test("offline processing machine is visible while retained results stay reviewable", async ({ page }) => {
  const unavailable = "Image search unavailable: RTX 4070 laptop offline";
  await open(page);
  const card = page.getByRole("button", { name: "Open post 999", exact: true });
  await card.focus();
  await card.press("Shift+S");
  await page.getByRole("link", { name: "Open results", exact: true }).click();
  const negative = page.getByRole("button", { name: "Mark post 998 negative", exact: true });
  await expect(negative).toBeVisible();
  await page.route("**/api/projects/*/similar", async (route) => {
    if (route.request().method() !== "GET") return route.continue();
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({ response, json: { ...data, available: false, message: unavailable } });
  });
  await expect(page.getByText(unavailable, { exact: true })).toBeVisible();
  await page.locator(".reference-tools > summary").click();
  await page.getByLabel("Source post ID", { exact: true }).fill("998");
  await expect(page.getByRole("button", { name: "Search post image", exact: true })).toBeDisabled();
  const preview = await page.request.get("/api/media/999/preview");
  await page.getByLabel("Reference image", { exact: true }).setInputFiles({
    name: "reference.png", mimeType: "image/png", buffer: await preview.body(),
  });
  await expect(page.getByRole("button", { name: "Search reference image", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Refresh search", exact: true })).toBeDisabled();
  await expect(page.locator('.similarity-folder[data-source-id="999"] > button').first()).toBeVisible();
  await negative.click();
  await expect(negative).toHaveAttribute("aria-pressed", "true");
  let imageSearchWrites = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/similar(?:\/reference)?(?:\?|$)/.test(request.url())) imageSearchWrites++;
  });
  await page.getByRole("tab", { name: "Query", exact: true }).click();
  const offlineCard = page.getByRole("button", { name: "Open post 999", exact: true });
  await offlineCard.focus();
  await offlineCard.press("Shift+S");
  await expect(page.getByText(unavailable, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Open post 998", exact: true }).click();
  await page.locator(".media-stage").press("Shift+S");
  await expect(page.getByText(unavailable, { exact: true })).toBeVisible();
  expect(imageSearchWrites).toBe(0);
  await page.unroute("**/api/projects/*/similar");
  await page.locator(".media-stage").press("Shift+S");
  await expect(page.getByRole("link", { name: "Open results", exact: true })).toBeVisible();
  expect(imageSearchWrites).toBe(1);
});
