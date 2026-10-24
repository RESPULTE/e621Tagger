import { test, expect, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";

async function decodesBundledVideo(page: Page) {
  // Probe a known local clip outside Studio: never skip on an app playback failure.
  const bytes = await readFile(
    new URL("../../backend/app/assets/demo.mp4", import.meta.url),
  );
  await page.goto("about:blank");
  return page.evaluate(
    async (source) => {
      const player = document.createElement("video");
      player.muted = true;
      player.src = source;
      document.body.appendChild(player);
      return new Promise<boolean>((resolve) => {
        const finish = (ok: boolean) => {
          clearTimeout(timer);
          player.onloadedmetadata = null;
          player.onerror = null;
          player.removeAttribute("src");
          player.load();
          player.remove();
          resolve(ok);
        };
        const timer = setTimeout(() => finish(false), 5000);
        player.onloadedmetadata = () => finish(true);
        player.onerror = () => finish(false);
      });
    },
    `data:video/mp4;base64,${bytes.toString("base64")}`,
  );
}

async function login(page: Page) {
  await page.goto("/");
  const options = await page.request.get("/api/auth/options");
  expect(options.ok()).toBe(true);
  if ((await options.json()).password_required) {
    await page
      .getByLabel("Studio password")
      .fill("studio-browser-test-password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  }
  await expect(
    page.getByRole("heading", { name: "Projects", level: 1 }),
  ).toBeVisible();
  const session = await page.request.get("/api/auth/session");
  expect(session.ok()).toBe(true);
  expect(
    (await session.json()).mock,
    "Browser regression tests require an isolated STUDIO_MOCK_E621=true deployment",
  ).toBe(true);
}

async function createProject(page: Page) {
  // Each workflow gets a distinct explicit target; canonical reuse itself is
  // tested in storage-review.spec.ts instead of relying on duplicate projects.
  await page.getByRole("button", { name: "Custom tag", exact: true }).click();
  await page
    .getByLabel("Target tag", { exact: true })
    .fill(
      `browser_target_${Date.now()}_${Math.random().toString(16).slice(2, 8)}`,
    );
  await page
    .getByRole("combobox", { name: "Discovery query", exact: true })
    .fill("dragon");
  await page
    .getByRole("button", { name: "Create project", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Open post 1000", exact: true }),
  ).toBeVisible();
}

test("search → import → original → labels → undo → gallery", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await login(page);
  await createProject(page);
  await expect(page.locator(".gallery-scroll img").first()).toBeVisible();
  await page
    .getByRole("button", { name: "Open post 999", exact: true })
    .click();
  const original = page.locator(".media-stage img");
  await expect(original).toBeVisible();
  await expect
    .poll(() =>
      original.evaluate((image: HTMLImageElement) => image.naturalWidth),
    )
    .toBe(1200);
  await page.getByRole("button", { name: "Actual size / zoom" }).click();
  await expect(page.locator(".media-stage")).toHaveClass(/actual-size/);
  await page
    .locator(".detail-review")
    .getByRole("button", { name: "Positive", exact: false })
    .click();
  // Acknowledged save advances to the next item. Previous returns to the reviewed post.
  await expect(
    page.getByRole("heading", { name: "Post #998", level: 1 }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Previous", exact: false }).click();
  await expect(
    page
      .locator(".detail-review")
      .getByRole("button", { name: "Positive", exact: false }),
  ).toHaveAttribute("aria-pressed", "true");
  await page.reload();
  await expect(
    page
      .locator(".detail-review")
      .getByRole("button", { name: "Positive", exact: false }),
  ).toHaveAttribute("aria-pressed", "true");
  await page
    .locator(".detail-review")
    .getByRole("button", { name: "Undo", exact: false })
    .click();
  await expect(page.getByText("Unreviewed", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Thumbnail gallery" }).click();
  await expect(
    page.getByRole("button", { name: "Open post 1000", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("gallery.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
  expect(
    await page
      .locator("body")
      .evaluate((body) => body.scrollWidth <= window.innerWidth),
  ).toBe(true);
});

test("quick labels and failed saves remain reviewable", async ({ page }) => {
  await login(page);
  await createProject(page);
  for (const id of [1000, 999]) {
    await page
      .getByRole("button", { name: `Mark post ${id} negative`, exact: true })
      .click();
    await expect(
      page.getByRole("button", {
        name: `Mark post ${id} negative`,
        exact: true,
      }),
    ).toHaveAttribute("aria-pressed", "true");
  }
  await expect(page.locator(".selection-bar")).toHaveCount(0);
  await page
    .getByRole("button", { name: "Open post 999", exact: true })
    .click();
  await expect(
    page
      .locator(".detail-review")
      .getByRole("button", { name: "Negative", exact: false }),
  ).toHaveAttribute("aria-pressed", "true");
  await page.route("**/api/projects/*/labels/999", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Simulated storage outage" }),
    }),
  );
  await page
    .locator(".detail-review")
    .getByRole("button", { name: "Positive", exact: false })
    .click();
  await expect(page.locator(".save-errors")).toContainText(
    "Simulated storage outage",
  );
  await expect(
    page
      .locator(".detail-review")
      .getByRole("button", { name: "Positive", exact: false }),
  ).toHaveAttribute("aria-pressed", "false");
  await page.unroute("**/api/projects/*/labels/999");
  await page.getByRole("button", { name: "Retry same save" }).click();
  await expect(
    page
      .locator(".detail-review")
      .getByRole("button", { name: "Positive", exact: false }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".save-errors")).toHaveCount(0);
});

test("video supports playback, seeking, and compatible MP4", async ({
  page,
}, testInfo) => {
  if (testInfo.project.name.includes("webkit")) {
    test.skip(
      !(await decodesBundledVideo(page)),
      "Host WebKit cannot decode the standalone H264 fixture; verify on macOS WebKit or physical iPhone.",
    );
  }
  await login(page);
  await createProject(page);
  await page
    .getByRole("button", { name: "Open post 1000", exact: true })
    .click();
  const video = page.locator("video");
  await expect(video).toBeVisible();
  await expect(video).toHaveAttribute("controls", "");
  await page.getByRole("button", { name: "Use compatible MP4" }).click();
  await expect(video).toHaveAttribute("src", /video_mp4(?:\?|$)/);
  await expect
    .poll(() => video.evaluate((player: HTMLVideoElement) => player.readyState))
    .toBeGreaterThanOrEqual(1);
  await video.evaluate(async (player: HTMLVideoElement) => {
    await player.play();
    player.pause();
    player.currentTime = 1;
  });
  await expect
    .poll(() =>
      video.evaluate((player: HTMLVideoElement) => player.currentTime),
    )
    .toBeGreaterThanOrEqual(0.9);
  await page.getByRole("button", { name: "Next / skip", exact: false }).click();
  await expect(page.locator("video")).toHaveCount(0);
});

test("keyboard undo restores the last review after auto-advance", async ({
  page,
}, testInfo) => {
  test.skip(
    testInfo.project.name !== "desktop-chromium",
    "Desktop keyboard workflow",
  );
  await login(page);
  await createProject(page);
  await page
    .getByRole("button", { name: "Open post 999", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Post #999", level: 1 }),
  ).toBeVisible();
  await expect(
    page
      .locator(".detail-review")
      .getByRole("button", { name: "Positive", exact: false }),
  ).toBeEnabled();
  await page.keyboard.press("1");
  await expect(
    page.getByRole("heading", { name: "Post #998", level: 1 }),
  ).toBeVisible();
  await expect(
    page
      .locator(".detail-review")
      .getByRole("button", { name: "Positive", exact: false }),
  ).toBeEnabled();
  await page.keyboard.press("z");
  await expect(
    page.getByRole("heading", { name: "Post #999", level: 1 }),
  ).toBeVisible();
  await expect(
    page.locator(".detail-review").getByText("Unreviewed", { exact: true }),
  ).toBeVisible();
});

test("unsupported video codec retains MP4 fallback and review controls", async ({
  page,
}, testInfo) => {
  test.skip(
    !testInfo.project.name.includes("webkit"),
    "Host WebKit unsupported codec path",
  );
  test.skip(
    await decodesBundledVideo(page),
    "This host can decode H264; playback scenario covers it.",
  );
  await login(page);
  await createProject(page);
  await page
    .getByRole("button", { name: "Open post 1000", exact: true })
    .click();
  await expect(page.locator("video")).toBeVisible();
  // Original decoding may succeed or fail independently of the MP4 codec.
  const compatible = page.getByRole("button", { name: "Use compatible MP4" });
  if (await compatible.isVisible()) await compatible.click();
  await expect(page.locator("video")).toHaveAttribute(
    "src",
    /video_mp4(?:\?|$)/,
  );
  await expect(page.getByRole("alert")).toContainText(
    "browser does not support its codec",
  );
  await expect(
    page
      .locator(".detail-review")
      .getByRole("button", { name: "Unsure", exact: false }),
  ).toBeEnabled();
  await expect(
    page.getByRole("link", { name: "Open the e621 post" }),
  ).toBeVisible();
});

test("private access reconnects without a password", async ({ page }) => {
  const options = await page.request.get("/api/auth/options");
  test.skip(
    (await options.json()).password_required,
    "Password authentication deployment",
  );
  const origin = new URL(options.url()).origin;
  if (origin.startsWith("http://")) {
    await page.context().addCookies([
      {
        name: "studio_session",
        value: "retained-https-cookie",
        url: origin.replace(/^http:/, "https:"),
        secure: true,
        httpOnly: true,
        sameSite: "Strict",
      },
    ]);
  }
  await login(page);
  if (origin.startsWith("http://")) {
    const cookies = await page.context().cookies(origin);
    expect(
      cookies.some(
        (cookie) => cookie.name === "studio_session_http" && !cookie.secure,
      ),
    ).toBe(true);
  }
  await expect(page.getByLabel("Studio password")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Sign out", exact: true }),
  ).toHaveCount(0);
  const before = await (await page.request.get("/api/auth/session")).json();
  const logout = await page.request.post("/api/auth/logout", {
    headers: {
      Origin: new URL(page.url()).origin,
      "X-CSRF-Token": before.csrf_token,
    },
  });
  expect(logout.ok()).toBe(true);
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Projects", level: 1 }),
  ).toBeVisible();
  const after = await (await page.request.get("/api/auth/session")).json();
  expect(after.csrf_token).not.toBe(before.csrf_token);
});

test("dataset quality freezes a downloadable version and preserves it after review edits", async ({
  page,
}) => {
  await login(page);
  await createProject(page);
  await page
    .getByRole("button", { name: "Mark post 999 positive", exact: true })
    .click();
  await expect(
    page.locator(".post-card").filter({
      has: page.getByRole("button", { name: "Open post 999", exact: true }),
    }),
  ).toHaveClass(/positive/);
  await page
    .getByRole("button", { name: "Mark post 998 negative", exact: true })
    .click();
  await expect(
    page.locator(".post-card").filter({
      has: page.getByRole("button", { name: "Open post 998", exact: true }),
    }),
  ).toHaveClass(/negative/);
  await page.getByRole("tab", { name: "Tagged", exact: true }).click();
  await page.getByRole("link", { name: "Dataset & export" }).click();
  await expect(page.getByRole("heading", { name: "Datasets" })).toBeVisible();
  const prepare = page.getByRole("button", {
    name: "Prepare next 64 previews",
  });
  await expect(prepare).toBeVisible();
  if (await prepare.isEnabled()) await prepare.click();
  await expect(
    page.getByRole("button", { name: "Freeze dataset", exact: true }),
  ).toBeEnabled();
  await page.getByLabel("Version name").fill("Browser frozen dataset");
  await page
    .getByRole("button", { name: "Freeze dataset", exact: true })
    .click();
  await expect(
    page.getByText("Frozen: 2 supervised samples · 0 excluded.", {
      exact: true,
    }),
  ).toBeVisible();
  const manifest = page
    .getByRole("link", { name: "Full manifest", exact: true })
    .last();
  const href = await manifest.getAttribute("href");
  const before = await (await page.request.get(href!)).text();
  expect(
    JSON.parse(before).samples.filter(
      (row: { target: number | null }) => row.target !== null,
    ),
  ).toHaveLength(2);
  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("link", { name: "CSV", exact: true }).last().click(),
  ]);
  expect(download.suggestedFilename()).toMatch(/^dataset-.*\.csv$/);
  await page
    .getByRole("link", { name: "Candidate gallery", exact: false })
    .click();
  await page
    .getByRole("button", { name: "Mark post 999 unsure", exact: true })
    .click();
  await expect(
    page.locator(".post-card").filter({
      has: page.getByRole("button", { name: "Open post 999", exact: true }),
    }),
  ).toHaveClass(/unsure/);
  expect(await (await page.request.get(href!)).text()).toBe(before);
  await page.getByRole("link", { name: "Dataset & export" }).click();
  expect(
    await page
      .locator("body")
      .evaluate((body) => body.scrollWidth <= window.innerWidth),
  ).toBe(true);
});

test("bounded neighbor discovery preserves human labels and exposes source tag counts", async ({
  page,
}, testInfo) => {
  await login(page);
  await createProject(page);
  await page
    .getByRole("button", { name: "Mark post 998 positive", exact: true })
    .click();
  const reviewed = page.locator(".post-card").filter({
    has: page.getByRole("button", { name: "Open post 998", exact: true }),
  });
  await expect(reviewed).toHaveClass(/positive/);
  await page
    .getByRole("button", { name: "Open post 999", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Post #999", exact: true, level: 1 }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Find similar posts", exact: true })
    .click();
  await expect(page.getByLabel("Select neighbor 998")).toBeVisible();
  await page.getByText("Embedding provenance", { exact: true }).click();
  await expect(
    page.getByText("Synthetic fixtures only; no semantic retrieval validation"),
  ).toBeVisible();
  await page.getByLabel("Select neighbor 998").check();
  await page.getByLabel("Select neighbor 997").check();
  await page
    .getByRole("button", {
      name: "Import selected neighbors (2/50)",
      exact: true,
    })
    .click();
  await page.getByRole("link", { name: "Open these search results" }).click();
  await expect(
    page.getByRole("button", { name: "Open post 998", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Open post 997", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Open post 999", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.locator(".post-card").filter({
      has: page.getByRole("button", { name: "Open post 998", exact: true }),
    }),
  ).toHaveClass(/positive/);
  await page
    .getByRole("button", { name: "Open post 998", exact: true })
    .click();
  await page.getByText("Discovered through", { exact: true }).click();
  await expect(
    page.getByText(/embedding: hydra35:synthetic:seed:999/),
  ).toBeVisible();
  await page.goto(page.url().replace(/posts\/998$/, "posts/999"));
  await expect(page.getByLabel("Select neighbor 998")).toBeVisible();
  await expect(
    page.getByText(/neighbors have available local metadata/),
  ).toBeVisible();
  const suggestions = page
    .getByRole("region", { name: "Embedding assistance" })
    .getByRole("list");
  await expect(suggestions).toBeVisible();
  await page.getByLabel("Hide tags").check();
  await expect(suggestions).toHaveCount(0);
  await page.reload();
  await expect(
    page.getByText("Tag suggestions are hidden for review."),
  ).toBeVisible();
  await page.getByLabel("Hide tags").uncheck();
  await expect(suggestions).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  expect(overflow).toBe(false);
  await page
    .getByRole("heading", { name: "Similar posts & tag suggestions" })
    .scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("neighbors.png") });
});

test("query autocomplete preserves tokens, aliases and prefixes", async ({
  page,
}) => {
  await login(page);
  await createProject(page);
  const query = page.getByRole("combobox", {
    name: "Discovery query",
  });
  await query.fill("solo -drak rating:s");
  await query.press("Home");
  for (let i = 0; i < 10; i++) await query.press("ArrowRight");
  await expect(
    page.getByRole("option", { name: /drake.*dragon/ }),
  ).toBeVisible();
  await query.press("ArrowDown");
  await query.press("Enter");
  await expect(query).toHaveValue("solo -dragon rating:s");
  await query.fill("~dra");
  await expect(
    page.getByRole("listbox", { name: "Tag suggestions" }),
  ).toBeVisible();
  await query.press("ArrowDown");
  await query.press("Tab");
  await expect(query).toHaveValue("~dragon ");
  await expect(query).not.toBeFocused();
  await query.fill("species:dra");
  await expect(
    page.getByRole("listbox", { name: "Tag suggestions" }),
  ).toBeVisible();
  await query.press("ArrowDown");
  await query.press("Enter");
  await expect(query).toHaveValue("dragon ");
  await query.fill("rating:");
  await page.getByRole("option", { name: "s", exact: true }).click();
  await expect(query).toHaveValue("rating:s ");
  await query.fill("dra");
  await expect(
    page.getByRole("listbox", { name: "Tag suggestions" }),
  ).toBeVisible();
  await query.press("Escape");
  await expect(
    page.getByRole("listbox", { name: "Tag suggestions" }),
  ).toHaveCount(0);
});

test("collection save is idempotent and removal is separately named", async ({
  page,
}) => {
  await login(page);
  await createProject(page);
  await page
    .getByRole("button", { name: "Save post 997 to Smut folder", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Open post 997", exact: true })
    .click();
  await expect(
    page.getByText("Original and e621 tags saved.", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Save post 997 to Smut folder", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Save post 997 to Smut folder",
      exact: true,
    }),
  ).toHaveAttribute("aria-pressed", "true");
  await page
    .getByRole("button", { name: "Remove collection files", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Save post 997 to Smut folder",
      exact: true,
    }),
  ).toHaveAttribute("aria-pressed", "false");
  await expect(
    page.locator(".detail-review").getByText("Unreviewed", { exact: true }),
  ).toBeVisible();
});
