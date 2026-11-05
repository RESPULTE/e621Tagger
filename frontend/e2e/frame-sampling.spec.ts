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

test("video checkpoints retain reviews until normal playback resumes", async ({ page }, testInfo) => {
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
  await expect(page.locator(".detail-review")).toHaveCount(0);
  await expect(video).toHaveAttribute("controls", "");
  await expect(video).toHaveAttribute("loop", "");
  // Parent favourites are collection-wide, so clear any earlier test's choice.
  if (await page.locator(".media-stage .favorite-paw").isVisible()) {
    await doubleTap(video, !!testInfo.project.use.hasTouch);
    await expect(page.locator(".media-stage .favorite-paw")).toHaveCount(0);
  }
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.readyState)).toBeGreaterThanOrEqual(2);
  await video.evaluate(async (v: HTMLVideoElement) => { v.muted = true; await v.play(); });
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime)).toBeGreaterThan(0);
  await video.evaluate((v: HTMLVideoElement) => { v.currentTime = v.duration - 0.15; });
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) =>
    !v.paused && v.currentTime < v.duration / 2,
  )).toBe(true);
  await page.getByRole("button", { name: "Enable frame sampling" }).click();
  const timeline = page.getByRole("slider", { name: "Animation position" });
  await expect(timeline).toBeVisible();
  await expect(page.locator(".detail-review")).toHaveCount(0);
  // Seeking while playing must pause and make the freshly rendered frame capturable.
  await timeline.focus();
  await timeline.press("Home");
  await timeline.press("ArrowRight");
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.paused && !v.seeking)).toBe(true);
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.currentTime)).toBeGreaterThan(0);
  // Hold the decoder in its seeking state to cover a tap before decoding finishes.
  await video.evaluate((v: HTMLVideoElement) => {
    Object.defineProperty(v, "seeking", { configurable: true, get: () => true });
  });
  await doubleTap(page.getByRole("button", { name: "Capture current frame" }), !!testInfo.project.use.hasTouch);
  await expect(page.locator(".sample-image")).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await video.evaluate((v: HTMLVideoElement) => {
    Reflect.deleteProperty(v, "seeking");
    v.dispatchEvent(new Event("seeked"));
  });
  await expect(page.locator(".sample-image")).toBeVisible();
  await expect(page.locator(".frame-checkpoint.undecided")).toHaveCount(1);
  await expect(page.getByRole("alert")).toHaveCount(0);
  const trackBox = await timeline.boundingBox();
  const videoBox = await video.boundingBox();
  const timeBox = await page.locator(".frame-time").boundingBox();
  expect(trackBox).not.toBeNull();
  expect(videoBox).not.toBeNull();
  expect(timeBox).not.toBeNull();
  expect(Math.abs(trackBox!.x + trackBox!.width / 2 - (videoBox!.x + videoBox!.width / 2))).toBeLessThan(2);
  expect(Math.abs(timeBox!.x + timeBox!.width / 2 - (videoBox!.x + videoBox!.width / 2))).toBeLessThan(2);
  expect(timeBox!.y).toBeGreaterThanOrEqual(trackBox!.y + trackBox!.height);
  await expect.poll(async () => (await samples()).filter(s => s.kind === "frame").length).toBe(1);
  const positive = page.locator(".detail-review").getByRole("button", { name: "Positive", exact: true });
  const negative = page.locator(".detail-review").getByRole("button", { name: "Negative", exact: true });
  await positive.click();
  await expect(positive).toHaveAttribute("aria-pressed", "true");
  await negative.click();
  await expect(negative).toHaveAttribute("aria-pressed", "true");
  await doubleTap(page.locator(".sample-image"), !!testInfo.project.use.hasTouch);
  await expect.poll(async () => (await samples()).find(s => s.kind === "frame")?.favorite).toBe(true);
  await expect(page.getByRole("button", { name: "Thumbnail", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Resume", exact: true })).toHaveCount(0);
  // Scrolling upwards must not hide decisions for the focused sample.
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  await page.evaluate(() => window.scrollBy(0, -100));
  await expect(page.locator(".detail-review")).not.toHaveClass(/is-collapsed/);
  await expect(negative).toHaveAttribute("aria-pressed", "true");
  await page.reload();
  await expect(video).toBeVisible();
  await page.getByRole("button", { name: "Enable frame sampling" }).click();
  await expect(page.locator(".frame-checkpoint.negative")).toHaveCount(1);
  // The checkpoint must remain selectable even with the cursor at its timestamp.
  await page.locator(".frame-checkpoint").click();
  await expect(negative).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".media-stage .favorite-paw")).toBeVisible();
  await video.evaluate(async (v: HTMLVideoElement) => { v.muted = true; await v.play(); });
  await expect(video).toBeVisible();
  await expect(page.locator(".media-stage .favorite-paw")).toHaveCount(0);
  await expect(page.locator(".detail-review")).toHaveCount(0);
  // A double tap on the video body targets the parent, not the saved frame.
  await doubleTap(video, !!testInfo.project.use.hasTouch);
  await expect(page.locator(".media-stage .favorite-paw")).toBeVisible();
  await expect.poll(async () => (await samples()).find(s => s.kind === "frame")?.favorite).toBe(true);
  await expect.poll(() => page.evaluate(() => !!document.fullscreenElement)).toBe(false);
  await expect(page.getByRole("slider", { name: "Animation position" })).toBeInViewport();
});

test("animated images show decisions only for a saved frame", async ({ page }, testInfo) => {
  await login(page);
  await createReviewProject(page, "animation_sampling");
  const projectId = new URL(page.url()).pathname.match(/projects\/(\d+)/)![1];
  await page.goto(`/projects/${projectId}/posts/1005`);
  await expect(page.getByRole("button", { name: "Enable frame sampling" })).toBeVisible();
  await expect(page.locator(".detail-review")).toHaveCount(0);
  await page.getByRole("button", { name: "Enable frame sampling" }).click();
  await expect(page.getByRole("slider", { name: "Animation position" })).toBeVisible();
  await expect(page.locator(".detail-review")).toHaveCount(0);
  await doubleTap(page.getByRole("button", { name: "Capture current frame" }), !!testInfo.project.use.hasTouch);
  await expect(page.locator(".frame-checkpoint.undecided")).toHaveCount(1);
  const positive = page.locator(".detail-review").getByRole("button", { name: "Positive", exact: true });
  await positive.click();
  await expect(positive).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await expect(page.locator(".detail-review")).toHaveCount(0);
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(page.locator(".detail-review")).toHaveCount(0);
  await page.locator(".frame-checkpoint.positive").click();
  await expect(positive).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByRole("alert")).toHaveCount(0);
});
