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
    page.getByRole("heading", { name: "Find a tag. Build a review set." }),
  ).toBeVisible();
  const session = await page.request.get("/api/auth/session");
  expect(session.ok()).toBe(true);
  expect(
    (await session.json()).mock,
    "Browser regression tests require an isolated STUDIO_MOCK_E621=true deployment",
  ).toBe(true);
}

async function createProject(page: Page) {
  await page.getByLabel("Tag name").fill("dragon");
  await page
    .getByRole("button", { name: "Create project", exact: true })
    .first()
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

test("mobile selections and failed saves remain reviewable", async ({
  page,
}) => {
  await login(page);
  await createProject(page);
  await page.getByLabel("Select post 1000", { exact: true }).check();
  await page.getByLabel("Select post 999", { exact: true }).check();
  await page
    .getByRole("button", { name: "Mark negative", exact: true })
    .click();
  await expect(
    page.getByText("Select posts for batch review", { exact: true }),
  ).toBeVisible();
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
  await expect(video).toHaveAttribute("src", /video_mp4$/);
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
  await expect(page.locator("video")).toHaveAttribute("src", /video_mp4$/);
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

test("returning from a post restores the gallery scroll position", async ({
  page,
}) => {
  await login(page);
  await createProject(page);
  const gallery = page.locator(".gallery-scroll");
  await gallery.evaluate((element) => {
    element.scrollTop = 650;
  });
  await expect
    .poll(() => gallery.evaluate((element) => element.scrollTop))
    .toBeGreaterThan(500);
  const label = await gallery.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return Array.from(
      element.querySelectorAll<HTMLButtonElement>(".thumbnail-link"),
    )
      .find((button) => {
        const rect = button.getBoundingClientRect();
        return rect.top >= bounds.top && rect.bottom <= bounds.bottom;
      })
      ?.getAttribute("aria-label");
  });
  expect(label).toBeTruthy();
  const before = await gallery.evaluate((element) => element.scrollTop);
  await page.getByRole("button", { name: label!, exact: true }).click();
  await page.getByRole("link", { name: "Thumbnail gallery" }).click();
  await expect
    .poll(async () =>
      Math.abs(
        (await gallery.evaluate((element) => element.scrollTop)) - before,
      ),
    )
    .toBeLessThan(10);
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
    page.getByRole("heading", { name: "Find a tag. Build a review set." }),
  ).toBeVisible();
  const after = await (await page.request.get("/api/auth/session")).json();
  expect(after.csrf_token).not.toBe(before.csrf_token);
});

test("additional query starts with the project selection and remains fully editable", async ({
  page,
}) => {
  await login(page);
  const url = new URL(page.url());
  if (
    url.protocol === "http:" &&
    !["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
  ) {
    expect(await page.evaluate(() => window.isSecureContext)).toBe(false);
    expect(await page.evaluate(() => typeof crypto.randomUUID)).toBe(
      "undefined",
    );
  }
  await createProject(page);
  const query = page.getByRole("textbox", {
    name: "Additional discovery query",
  });
  await expect(query).toHaveValue("dragon");
  await query.fill("dragon score:>100");
  const importButton = page.getByRole("button", {
    name: "Import 50",
    exact: true,
  });
  await expect(importButton).toBeEnabled();
  const [request] = await Promise.all([
    page.waitForRequest(
      (request) =>
        request.url().endsWith("/discovery") && request.method() === "POST",
    ),
    importButton.click(),
  ]);
  expect(request.postDataJSON().query).toBe("dragon score:>100");
  await expect(importButton).toBeEnabled();
  await expect(query).toHaveValue("dragon score:>100");
  await query.fill("score:>100");
  const [withoutTag] = await Promise.all([
    page.waitForRequest(
      (request) =>
        request.url().endsWith("/discovery") && request.method() === "POST",
    ),
    importButton.click(),
  ]);
  expect(withoutTag.postDataJSON().query).toBe("score:>100");
  expect(withoutTag.postDataJSON().request_id).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
});

test("additional searches show overlaps, filtered results and exhaustion without losing reviews", async ({
  page,
}) => {
  await login(page);
  await createProject(page);
  await page.getByLabel("Select post 1000", { exact: true }).check();
  await page
    .getByRole("button", { name: "Mark positive", exact: true })
    .click();
  await expect(
    page.getByText("Select posts for batch review", { exact: true }),
  ).toBeVisible();
  const query = page.getByRole("textbox", {
    name: "Additional discovery query",
  });
  const submit = page.getByRole("button", { name: "Import 50", exact: true });
  await query.fill("dragon solo");
  await submit.click();
  await expect(page.locator(".import-status")).toContainText(
    "0 new · 50 already in project",
  );
  await expect(
    page.getByLabel("Search results", { exact: true }),
  ).not.toHaveValue("");
  const view = new URL(page.url());
  const imported = await (
    await page.request.get(`/api${view.pathname}/candidates${view.search}`)
  ).json();
  expect(
    imported.items.every((post: { sources: string[] }) =>
      post.sources.includes("query: dragon solo"),
    ),
  ).toBe(true);
  await query.fill("dragon score:>190");
  await submit.click();
  await expect(page.locator(".import-status")).toContainText(
    "No more results for this search",
  );
  await expect(
    page.getByRole("button", { name: "Open post 1000", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "27 matching",
  );
  await query.fill("no_such_mock_tag");
  await submit.click();
  await expect(page.locator(".import-status")).toContainText(
    "0 matches processed",
  );
  await submit.click();
  await expect(page.locator(".import-status")).toContainText(
    "No more results for this search",
  );
  await page.getByLabel("Search results", { exact: true }).selectOption("");
  await expect(
    page.getByRole("button", { name: "Open post 1000", exact: true }),
  ).toBeVisible();
  const card = page.locator(".post-card").filter({
    has: page.getByRole("button", { name: "Open post 1000", exact: true }),
  });
  await expect(card).toHaveClass(/positive/);
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
  await page.getByRole("link", { name: "Dataset quality & exports" }).click();
  await expect(
    page.getByRole("heading", { name: "Review, group, freeze" }),
  ).toBeVisible();
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
    page.getByText("Frozen: 2 supervised samples · 48 excluded.", {
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
  await page.getByRole("link", { name: "Dataset quality & exports" }).click();
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
  const reviewed = page
    .locator(".post-card")
    .filter({
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
    page
      .locator(".post-card")
      .filter({
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
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  expect(overflow).toBe(false);
  await page
    .getByRole("heading", { name: "Similar posts & tag suggestions" })
    .scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("neighbors.png") });
});
