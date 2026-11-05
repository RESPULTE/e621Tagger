import { expect, test, type Locator, type Page } from "@playwright/test";
import { createReviewProject } from "./project-fixture";
import { decodesBundledVideo } from "./video-fixture";

async function doubleTap(target: Locator, touch: boolean) {
  await target.scrollIntoViewIfNeeded();
  if (touch) { await target.tap(); await target.tap(); }
  else { await target.click(); await target.click(); }
}

async function login(page: Page) {
  await page.goto("/");
  const options = await (await page.request.get("/api/auth/options")).json();
  if (options.password_required) {
    await page.getByLabel("Studio password").fill("studio-browser-test-password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  }
  await expect(page.getByRole("button", { name: "Projects", exact: true })).toBeVisible();
  expect((await (await page.request.get("/api/auth/session")).json()).mock).toBe(true);
}

test("video frames and thumbnail retain independent reviews and favourites", async ({ page }, testInfo) => {
  if (testInfo.project.name.includes("webkit")) {
    test.skip(
      !(await decodesBundledVideo(page)),
      "Host WebKit cannot decode the standalone H264 fixture; verify on macOS WebKit or physical iPhone.",
    );
  }
  await login(page);
  await createReviewProject(page, "frame_sampling");
  const projectId = new URL(page.url()).pathname.match(/projects\/(\d+)/)![1];
  const samples = async () => {
    const response = await page.request.get(`/api/projects/${projectId}/posts/1000/samples`);
    expect(response.ok()).toBe(true);
    return await response.json() as { kind: string; state: string | null; favorite: boolean; timestamp_ms: number | null }[];
  };
  await page.getByRole("button", { name: "Open post 1000", exact: true }).click();
  const video = page.locator("video");
  await expect(video).toBeVisible();
  await expect(video).toHaveAttribute("controls", "");
  // Parent favourites are collection-wide, so clear any earlier test's choice.
  if (await page.locator(".media-stage .favorite-paw").isVisible()) {
    await doubleTap(video, !!testInfo.project.use.hasTouch);
    await expect(page.locator(".media-stage .favorite-paw")).toHaveCount(0);
  }
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.readyState)).toBeGreaterThanOrEqual(2);
  await video.evaluate(async (v: HTMLVideoElement) => { v.muted = true; await v.play(); });
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime)).toBeGreaterThan(0);
  await page.getByRole("button", { name: "Enable frame sampling" }).click();
  const timeline = page.getByRole("slider", { name: "Animation position" });
  await expect(timeline).toBeVisible();
  // Seeking while playing must pause and make the freshly rendered frame capturable.
  await timeline.focus();
  await timeline.press("Home");
  await timeline.press("ArrowRight");
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.paused && !v.seeking)).toBe(true);
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime)).toBeGreaterThan(0);
  await doubleTap(page.getByRole("button", { name: "Capture current frame" }), !!testInfo.project.use.hasTouch);
  await expect(page.locator(".sample-image")).toBeVisible();
  await expect.poll(async () => (await samples()).filter(s => s.kind === "frame").length).toBe(1);
  const positive = page.locator(".detail-review").getByRole("button", { name: "Positive", exact: true });
  const negative = page.locator(".detail-review").getByRole("button", { name: "Negative", exact: true });
  await positive.click();
  await expect(positive).toHaveAttribute("aria-pressed", "true");
  await negative.click();
  await expect(negative).toHaveAttribute("aria-pressed", "true");
  await doubleTap(page.locator(".sample-image"), !!testInfo.project.use.hasTouch);
  await expect.poll(async () => (await samples()).find(s => s.kind === "frame")?.favorite).toBe(true);
  await page.getByRole("button", { name: "Thumbnail", exact: true }).click();
  await expect(page.getByAltText("Sampled thumbnail")).toBeVisible();
  await positive.focus();
  await positive.click();
  await expect(positive).toHaveAttribute("aria-pressed", "true");
  await expect.poll(async () => (await samples()).find(s => s.kind === "thumbnail")?.state).toBe("positive");
  await expect.poll(async () => (await samples()).find(s => s.kind === "frame")?.state).toBe("negative");
  await expect.poll(async () => (await samples()).find(s => s.kind === "thumbnail")?.favorite).toBe(false);
  await page.reload();
  await expect(video).toBeVisible();
  await page.getByRole("button", { name: "Enable frame sampling" }).click();
  await expect(page.locator(".frame-checkpoint.negative")).toHaveCount(1);
  await page.getByRole("button", { name: "Thumbnail", exact: true }).click();
  await expect(positive).toHaveAttribute("aria-pressed", "true");
  // The checkpoint must remain selectable even with the cursor at its timestamp.
  await page.locator(".frame-checkpoint").click();
  await expect(negative).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".media-stage .favorite-paw")).toBeVisible();
  await page.getByRole("button", { name: "Resume" }).click();
  await expect(video).toBeVisible();
  await expect(page.locator(".media-stage .favorite-paw")).toHaveCount(0);
  // A double tap on the video body targets the parent, not the saved frame.
  await doubleTap(video, !!testInfo.project.use.hasTouch);
  await expect(page.locator(".media-stage .favorite-paw")).toBeVisible();
  await expect.poll(async () => (await samples()).find(s => s.kind === "frame")?.favorite).toBe(true);
  await expect.poll(async () => (await samples()).find(s => s.kind === "thumbnail")?.favorite).toBe(false);
});
