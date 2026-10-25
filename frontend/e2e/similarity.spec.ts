import { test, expect, type Page, type Locator } from "@playwright/test";

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
  await page.getByRole("button", { name: "Custom tag", exact: true }).click();
  await page
    .getByLabel("Target tag", { exact: true })
    .fill(`similarity_${Date.now()}_${Math.random().toString(16).slice(2, 8)}`);
  await page
    .getByRole("combobox", { name: "Discovery query", exact: true })
    .fill("dragon");
  await page
    .getByRole("button", { name: "Create project", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Open post 999", exact: true }),
  ).toBeVisible();
}

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
    page.getByRole("button", { name: "Post #999", exact: true }),
  ).toBeVisible();
  const negative = page.getByRole("button", {
    name: "Mark post 998 negative",
    exact: true,
  });
  await expect(negative).toBeVisible();
  await negative.click();
  await expect(negative).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("progressbar")).toHaveCount(0);
  await page.reload();
  await expect(negative).toHaveAttribute("aria-pressed", "true");
  await page
    .getByRole("button", { name: "Open post 998", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Post #998", exact: true, level: 1 }),
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
    .getByRole("button", { name: "Undo this post", exact: true })
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
    page.getByRole("button", { name: "Post #999", exact: true }),
  ).toHaveCount(1);
});

test("completed downloads hide their progress bar", async ({ page }) => {
  await open(page);
  await page
    .getByLabel("Save post 998 to Smut folder", { exact: true })
    .click();
  await page.getByRole("tab", { name: "Downloads", exact: true }).click();
  await expect(
    page.getByText("Completed · verified", { exact: true }).first(),
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
      .getByRole("button", { name: `Post #${id}`, exact: true })
      .click();
  }
  await expect(
    page.getByRole("button", { name: "Post #998", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByRole("button", { name: "Open post 997", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Open post 998", exact: true }),
  ).toHaveCount(0);
});

test("offline processing machine is visible while retained results stay reviewable", async ({ page }) => {
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
    await route.fulfill({ response, json: { ...data, available: false, message: "Processing machine offline" } });
  });
  await expect(page.getByText("Processing machine offline", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Post #999", exact: true })).toBeVisible();
  await negative.click();
  await expect(negative).toHaveAttribute("aria-pressed", "true");
  await page.unroute("**/api/projects/*/similar");
  await expect(page.getByText("Processing machine offline", { exact: true })).toHaveCount(0);
});
