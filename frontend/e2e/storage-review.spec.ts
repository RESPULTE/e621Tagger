import { test, expect, type Page } from "@playwright/test";

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
    page.getByRole("heading", { name: "Projects", exact: true, level: 1 }),
  ).toBeVisible();
  expect(
    (await (await page.request.get("/api/auth/session")).json()).mock,
  ).toBe(true);
  await page.getByLabel("Tag name").fill("dragon");
  await page.getByLabel("Tag name").press("Enter");
  await expect(
    page.getByRole("button", { name: "Open post 999", exact: true }),
  ).toBeVisible();
}

test("fresh Query, persistent reviews, compact tabs and infinite loading", async ({
  page,
}) => {
  await open(page);
  const url = page.url();
  await expect(
    page.getByRole("tab", { name: "Query", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(page.getByText("All imported posts")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Undo", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByText("New", { exact: true })).toHaveCount(0);
  await page
    .getByRole("button", { name: "Mark post 999 positive", exact: true })
    .click();
  await expect(page.getByLabel("positive: 1", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Tagged", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Open post 999", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Open post 1000", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "Dataset & export", exact: true }),
  ).toBeVisible();
  let queries = 0;
  page.on("request", (r) => {
    if (r.url().includes("/query?")) queries++;
  });
  await page.goto(url);
  await expect(
    page.getByRole("button", { name: "Mark post 999 positive", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  expect(queries).toBeGreaterThan(0);
  const next = page.waitForRequest(
    (r) => r.url().includes("/query?") && r.url().includes("cursor="),
  );
  await page.getByLabel("Candidate gallery").evaluate((el) => {
    el.scrollTop = el.scrollHeight;
  });
  await next;
  await expect(page.locator(".post-card img").first()).toBeVisible();
  await page.screenshot({
    path: test.info().outputPath("query.png"),
    fullPage: true,
  });
  await page.getByRole("tab", { name: "Downloads", exact: true }).click();
  await expect(page.getByText(/active · Collection originals/)).toBeVisible();
  await page.screenshot({
    path: test.info().outputPath("downloads.png"),
    fullPage: true,
  });
});

test("double pointer tap is an idempotent save; single click and keyboard open", async ({
  page,
  isMobile,
}) => {
  await open(page);
  const url = page.url();
  const thumb = page.getByRole("button", {
    name: "Open post 998",
    exact: true,
  });
  if (isMobile) {
    await thumb.tap();
    await thumb.tap();
  } else await thumb.dblclick({ delay: 75 });
  await expect(page).toHaveURL(url);
  await expect(thumb.locator(".save-heart")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Mark post 998 negative", exact: true }),
  ).toHaveAttribute("aria-pressed", "false");
  if (isMobile) {
    await thumb.tap();
    await thumb.tap();
  } else await thumb.dblclick({ delay: 75 });
  await page.getByRole("tab", { name: "Downloads", exact: true }).click();
  await expect(
    page.getByText("Collection", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByText("Completed · verified").first()).toBeVisible();
  await page.getByRole("tab", { name: "Query", exact: true }).click();
  const openThumb = page.getByRole("button", {
    name: "Open post 999",
    exact: true,
  });
  await openThumb.click();
  await expect(page).toHaveURL(/posts\/999$/);
  await page.getByRole("link", { name: "Back to posts" }).click();
  const keyboard = page.getByRole("button", {
    name: "Open post 997",
    exact: true,
  });
  await keyboard.focus();
  await keyboard.press("Enter");
  await expect(page).toHaveURL(/posts\/997$/);
});

test("dragging does not save or open; reduced motion and keyboard tab navigation", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await open(page);
  const url = page.url();
  const thumb = page.getByRole("button", {
    name: "Open post 996",
    exact: true,
  });
  await thumb.scrollIntoViewIfNeeded();
  await thumb.dispatchEvent("pointerdown", {
    isPrimary: true,
    button: 0,
    clientX: 50,
    clientY: 50,
    pointerId: 1,
  });
  await thumb.dispatchEvent("pointermove", {
    isPrimary: true,
    clientX: 50,
    clientY: 95,
    pointerId: 1,
  });
  await thumb.dispatchEvent("pointerup", {
    isPrimary: true,
    button: 0,
    clientX: 50,
    clientY: 95,
    pointerId: 1,
  });
  await page.waitForTimeout(400);
  await expect(page).toHaveURL(url);
  await expect(
    page.getByRole("button", {
      name: "Save post 996 to Smut folder",
      exact: true,
    }),
  ).toHaveAttribute("aria-pressed", "false");
  await page.getByRole("tab", { name: "Query", exact: true }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(
    page.getByRole("tab", { name: "Tagged", exact: true }),
  ).toBeFocused();
  await expect(
    page.getByRole("tab", { name: "Tagged", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
});
