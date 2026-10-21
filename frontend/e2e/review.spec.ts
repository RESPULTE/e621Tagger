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
  await page.getByLabel("Studio password").fill("studio-browser-test-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Find a tag. Build a review set." }),
  ).toBeVisible();
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
  await page.keyboard.press("1");
  await expect(
    page.getByRole("heading", { name: "Post #998", level: 1 }),
  ).toBeVisible();
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
