import { expect, test, type Locator, type Page } from "@playwright/test";
import { createReviewProject } from "./project-fixture";
import { decodesBundledVideo } from "./video-fixture";
import { touchGesture } from "./touch-gesture";

async function doubleTap(target: Locator, touch: boolean) {
  await target.scrollIntoViewIfNeeded();
  if (touch) {
    const box = (await target.boundingBox())!;
    const finger = await touchGesture(target.page());
    try {
      const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
      await finger.start(point); await finger.end();
      await finger.start(point); await finger.end();
    } finally { await finger.dispose(); }
  }
  else { await target.click(); await target.click(); }
}

async function startPull(page: Page, touch: boolean, reducedMotion = false, inspect = true) {
  const cursor = page.getByRole("button", { name: "Capture current frame" });
  await page.locator(".frame-timeline-wrap").scrollIntoViewIfNeeded();
  const box = (await cursor.boundingBox())!;
  const x = box.x + box.width / 2, y = box.y + box.height / 2;
  const finger = touch ? await touchGesture(page) : null;
  if (finger) {
    await cursor.evaluate(element => element.addEventListener("pointerdown", event => {
      element.setAttribute("data-test-input", `${event.pointerType}:${event.isTrusted}`);
    }, { once: true }));
    await finger.start({ x, y });
    await finger.move({ x, y: y + 28 });
  } else {
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x, y + 28, { steps: 4 });
  }
  const release = async () => {
    if (finger) { await finger.end(); await finger.dispose(); }
    else await page.mouse.up();
  };
  // Short-release/cancel scenarios must send input before the hold expires,
  // independent of how long remote geometry assertions take to return.
  if (!inspect) return release;
  if (finger) await expect(cursor).toHaveAttribute("data-test-input", "touch:true");
  const ring = cursor.locator(".hold-indicator");
  await expect(ring).toBeVisible();
  // One browser read keeps geometry checks from consuming the one-second hold
  // before short-release/cancellation assertions on slower WebKit hosts.
  const { notchBox, trackBox, ringBox, pulledCursor, animationDuration } = await cursor.evaluate(element => {
    const root = element.parentElement!;
    const ring = element.querySelector(".hold-indicator")!;
    const rect = (node: Element) => {
      const { x, y, width, height } = node.getBoundingClientRect();
      return { x, y, width, height };
    };
    return { notchBox: rect(root.querySelector(".frame-position")!), trackBox: rect(root.querySelector(".frame-track")!),
      ringBox: rect(ring), pulledCursor: rect(element),
      animationDuration: getComputedStyle(ring.querySelector(".hold-indicator-progress")!).animationDuration };
  });
  expect(animationDuration).toBe(reducedMotion ? "0s" : "1s");
  expect(Math.abs(notchBox.y + notchBox.height / 2 - (trackBox.y + trackBox.height / 2))).toBeLessThan(1);
  expect(Math.abs(notchBox.x + notchBox.width / 2 - (box.x + box.width / 2))).toBeLessThan(1);
  expect(Math.abs(ringBox.x + ringBox.width / 2 - (pulledCursor.x + pulledCursor.width / 2))).toBeLessThan(1);
  expect(Math.abs(ringBox.y + ringBox.height / 2 - (pulledCursor.y + pulledCursor.height / 2))).toBeLessThan(1);
  return release;
}

async function pullToCapture(page: Page, touch: boolean) {
  const release = await startPull(page, touch);
  try {
    // Capture fires on completion while the pointer is still held down.
    await expect(page.locator(".frame-cursor .hold-indicator")).toHaveCount(0, { timeout: 1800 });
  } finally {
    await release();
  }
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
  // A stalled decoder must not delay or overwrite requested timeline positions.
  await video.evaluate((v: HTMLVideoElement) => {
    v.pause();
    const previous = v.currentTime;
    Object.defineProperty(v, "seeking", { configurable: true, get: () => true });
    Object.defineProperty(v, "currentTime", { configurable: true, get: () => previous, set: () => {} });
  });
  await timeline.press("End");
  await expect(timeline).toHaveAttribute("aria-valuenow", await timeline.getAttribute("aria-valuemax") as string);
  await video.dispatchEvent("timeupdate");
  await video.dispatchEvent("seeked");
  await expect(timeline).toHaveAttribute("aria-valuenow", await timeline.getAttribute("aria-valuemax") as string);
  await timeline.press("Home");
  await timeline.press("ArrowRight");
  await timeline.press("ArrowRight");
  await video.dispatchEvent("timeupdate");
  await expect(timeline).toHaveAttribute("aria-valuenow", "200");
  await video.evaluate((v: HTMLVideoElement) => {
    Reflect.deleteProperty(v, "seeking");
    Reflect.deleteProperty(v, "currentTime");
  });
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
  await pullToCapture(page, !!testInfo.project.use.hasTouch);
  await expect(page.locator(".sample-image")).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await video.evaluate((v: HTMLVideoElement) => {
    Reflect.deleteProperty(v, "seeking");
    v.dispatchEvent(new Event("seeked"));
  });
  await expect(page.locator(".sample-image")).toBeVisible();
  await expect(page.locator(".frame-checkpoint.undecided")).toHaveCount(1);
  // Saved notches stay at the captured time while the live notch follows seeking.
  const savedNotch = page.locator(".frame-checkpoint.undecided");
  const savedLeft = await savedNotch.evaluate((element) => element.getBoundingClientRect().x);
  await timeline.press("ArrowRight");
  await expect.poll(() => page.locator(".frame-position").evaluate((element) => element.getBoundingClientRect().x)).toBeGreaterThan(savedLeft + 14);
  expect(Math.abs(await savedNotch.evaluate((element) => element.getBoundingClientRect().x) - savedLeft)).toBeLessThan(1);
  await savedNotch.click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  const trackBox = await timeline.boundingBox();
  const videoBox = await video.boundingBox();
  const timeBox = await page.locator(".frame-time").boundingBox();
  expect(trackBox).not.toBeNull();
  expect(videoBox).not.toBeNull();
  expect(timeBox).not.toBeNull();
  expect(Math.abs(trackBox!.x + trackBox!.width / 2 - (videoBox!.x + videoBox!.width / 2))).toBeLessThan(2);
  expect(Math.abs(trackBox!.width - (videoBox!.width - 32))).toBeLessThan(2);
  // The endpoint handles must stay clear of browser edge-navigation gestures.
  await timeline.press("Home");
  const endpoint = (await page.locator(".frame-cursor").boundingBox())!;
  expect(endpoint.x + endpoint.width / 2).toBeGreaterThanOrEqual(24);
  await timeline.press("End");
  const lastEndpoint = (await page.locator(".frame-cursor").boundingBox())!;
  expect(lastEndpoint.x + lastEndpoint.width / 2).toBeLessThanOrEqual(page.viewportSize()!.width - 24);
  await page.locator(".frame-checkpoint").click();
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

test("deselected project keeps video capture and frame favourites without tagging", async ({ page }, testInfo) => {
  if (testInfo.project.name.includes("webkit")) test.skip(
    !(await decodesBundledVideo(page)), "Host WebKit cannot decode the H264 fixture.",
  );
  await login(page);
  await createReviewProject(page, "browse_sampling");
  const projectPath = new URL(page.url()).pathname;
  const projectId = projectPath.split("/").pop()!;
  await page.getByRole("button", { name: "Open post 1000", exact: true }).click();
  const video = page.locator("video");
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.readyState)).toBeGreaterThanOrEqual(2);
  await page.getByRole("button", { name: "Projects", exact: true }).click();
  await page.locator(`.project-dock-row[data-project-id="${projectId}"] .project-dock-select`).click();
  await expect(page).toHaveURL(new RegExp(projectPath + "/posts/1000$"));
  await page.getByRole("button", { name: "Enable frame sampling" }).click();
  await page.getByRole("slider", { name: "Animation position" }).press("Home");
  await pullToCapture(page, !!testInfo.project.use.hasTouch);
  const sample = page.locator(".sample-image");
  await expect(sample).toBeVisible();
  await expect(page.locator(".detail-review")).toHaveCount(0);
  let labelWrites = 0;
  page.on("request", request => {
    if (request.method() === "PATCH" && request.url().includes("/samples/") &&
      Object.hasOwn(request.postDataJSON(), "state")) labelWrites++;
  });
  await page.locator("h1").click();
  await page.keyboard.press("1");
  await page.keyboard.press("2");
  await page.keyboard.press("z");
  await doubleTap(sample, !!testInfo.project.use.hasTouch);
  const samples = async () => (await (await page.request.get(`/api${projectPath}/posts/1000/samples`)).json()) as { favorite: boolean; state: string | null }[];
  await expect.poll(async () => (await samples()).some(frame => frame.favorite)).toBe(true);
  expect((await samples()).every(frame => frame.state === null)).toBe(true);
  expect(labelWrites).toBe(0);
  await page.reload();
  await expect(video).toBeVisible();
  await page.getByRole("button", { name: "Enable frame sampling" }).click();
  await page.locator(".frame-checkpoint").click();
  await expect(sample).toBeVisible();
  await expect(page.locator(".media-stage .favorite-paw")).toBeVisible();
  await expect(page.locator(".detail-review")).toHaveCount(0);
  await page.getByRole("button", { name: "Projects", exact: true }).click();
  await page.locator(`.project-dock-row[data-project-id="${projectId}"] .project-dock-select`).click();
  await page.getByRole("button", { name: "Positive", exact: true }).click();
  await expect.poll(async () => (await samples()).some(frame => frame.state === "positive")).toBe(true);
});

test("touch sampling survives rotation, cancellation and reduced motion", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.use.hasTouch, "Touch viewport workflow");
  if (testInfo.project.name.includes("webkit")) test.skip(
    !(await decodesBundledVideo(page)), "Host WebKit cannot decode the H264 fixture.",
  );
  await login(page);
  await createReviewProject(page, "rotated_sampling");
  const projectId = new URL(page.url()).pathname.match(/projects\/(\d+)/)![1];
  await page.goto(`/projects/${projectId}/posts/1000`);
  const video = page.locator("video");
  await expect.poll(() => video.evaluate((v: HTMLVideoElement) => v.readyState)).toBeGreaterThanOrEqual(2);
  await page.getByRole("button", { name: "Enable frame sampling" }).tap();
  const timeline = page.getByRole("slider", { name: "Animation position" });
  const samples = async () => (await (await page.request.get(`/api/projects/${projectId}/posts/1000/samples`)).json()) as { timestamp_ms: number }[];
  for (const size of [{ width: 844, height: 390 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(size);
    await timeline.scrollIntoViewIfNeeded();
    await expect.poll(async () => {
      const track = (await timeline.boundingBox())!, media = (await video.boundingBox())!;
      return Math.max(Math.abs(track.x - media.x - 16), Math.abs(track.width - media.width + 32));
    }).toBeLessThan(1);
  }
  await page.emulateMedia({ reducedMotion: "reduce" });
  await timeline.press("Home");
  await timeline.press("ArrowRight");
  const cursor = page.getByRole("button", { name: "Capture current frame" });
  await page.locator(".frame-timeline-wrap").scrollIntoViewIfNeeded();
  const box = (await cursor.boundingBox())!, finger = await touchGesture(page);
  const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await finger.start(point);
  await finger.move({ x: point.x, y: point.y + 28 });
  await expect(cursor.locator(".hold-indicator")).toBeVisible();
  await finger.cancel();
  await finger.dispose();
  await expect(cursor.locator(".hold-indicator")).toHaveCount(0);
  await page.waitForTimeout(1100);
  expect(await samples()).toHaveLength(0);
  const release = await startPull(page, true, true);
  try { await expect(page.locator(".frame-checkpoint")).toHaveCount(1); }
  finally { await release(); }
  await expect(page.locator(".frame-checkpoint")).toHaveCSS("animation-name", "none");
  expect(await page.locator(".frame-checkpoint").evaluate(element => getComputedStyle(element, "::before").animationName)).toBe("none");
  const captured = (await samples())[0].timestamp_ms;
  // Touch-scrub away from the saved frame, then select its persistent notch.
  const track = (await timeline.boundingBox())!, scrub = await touchGesture(page);
  await scrub.start({ x: track.x + track.width * .7, y: track.y + 19 });
  await scrub.move({ x: track.x + track.width * .8, y: track.y + 19 });
  await scrub.end();
  await scrub.dispose();
  await expect(page.locator(".sample-image")).toHaveCount(0);
  expect((await samples())[0].timestamp_ms).toBe(captured);
  const saved = (await page.locator(".frame-checkpoint").boundingBox())!, drag = await touchGesture(page);
  const savedPoint = { x: saved.x + saved.width / 2, y: saved.y + saved.height / 2 };
  await drag.start(savedPoint);
  await drag.move({ x: savedPoint.x + 40, y: savedPoint.y });
  await drag.end();
  await drag.dispose();
  await expect(page.locator(".sample-image")).toHaveCount(0);
  await page.locator(".frame-checkpoint").tap();
  await expect(page.locator(".sample-image")).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("iphone-timeline-notches.png") });
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
  // Taps no longer sample, and a short pull must cancel without leaving a marker.
  await doubleTap(page.getByRole("button", { name: "Capture current frame" }), !!testInfo.project.use.hasTouch);
  await expect(page.locator(".frame-checkpoint")).toHaveCount(0);
  const release = await startPull(page, !!testInfo.project.use.hasTouch, false, false);
  await page.waitForTimeout(250);
  await release();
  await expect(page.locator(".frame-cursor .hold-indicator")).toHaveCount(0);
  await expect(page.locator(".frame-checkpoint")).toHaveCount(0);
  const releaseCancelled = await startPull(page, !!testInfo.project.use.hasTouch, false, false);
  await page.getByRole("button", { name: "Capture current frame" }).dispatchEvent("pointercancel");
  await releaseCancelled();
  await page.waitForTimeout(1100);
  await expect(page.locator(".frame-cursor .hold-indicator")).toHaveCount(0);
  await expect(page.locator(".frame-checkpoint")).toHaveCount(0);
  await pullToCapture(page, !!testInfo.project.use.hasTouch);
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
