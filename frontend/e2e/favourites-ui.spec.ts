import { createReviewProject } from "./project-fixture";
import { test, expect, type Page } from "@playwright/test";
test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: "wait" }); });
async function open(page: Page) {
  await page.goto("/");
  const options = await (await page.request.get("/api/auth/options")).json();
  if (options.password_required) {
    await page.getByLabel("Studio password").fill("studio-browser-test-password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  }
  await expect(page.getByRole("button", { name: "Projects", exact: true })).toBeVisible();
  expect((await (await page.request.get("/api/auth/session")).json()).mock).toBe(true);
  await createReviewProject(page, "favourite");
  await expect(page.getByRole("button", { name: "Open post 999", exact: true })).toBeVisible();
}
test("keyboard favourite cancels a queued save without a heart button", async ({ page }) => {
  await open(page);
  const project = new URL(page.url()).pathname;
  const current = await (await page.request.get("/api/library/posts/999")).json();
  await page.request.put(`/api${project}/library/999`, { data: { wanted: false, expected_version: current.version, request_id: crypto.randomUUID() } });
  await expect.poll(async () => (await (await page.request.get("/api/library/posts/999")).json()).present).toBe(false);
  await page.reload();
  let wanted = false;
  const intents: boolean[] = [];
  const state = () => ({ post_id: 999, wanted, present: false, version: intents.length + 1, status: wanted ? "queued" : "idle", error: null, updated_at: Date.now() });
  await page.route("**/api/library/posts/999", (route) => route.fulfill({ json: state() }));
  await page.route("**/api/projects/*/library/999", async (route) => {
    wanted = route.request().postDataJSON().wanted;
    intents.push(wanted);
    await route.fulfill({ json: state() });
  });
  const media = page.getByRole("button", { name: "Open post 999", exact: true });
  await media.press("f");
  await expect(media.locator(".favorite-paw")).toBeVisible();
  await expect(media.locator(".favorite-paw")).toHaveCSS("color", "rgb(255, 126, 182)");
  await media.press("f");
  await expect(media.locator(".favorite-paw")).toHaveCount(0);
  expect(intents).toEqual([true, false]);
});
test("downloads show upvotes and delete favourites with heart cache reconciliation", async ({ page }) => {
  await open(page);
  let deleted = false;
  const saved = { post_id: 999, wanted: true, present: true, version: 10, status: "saved", error: null, updated_at: Date.now() + 1000 };
  const off = { ...saved, wanted: false, present: false, status: "idle", version: 11, updated_at: saved.updated_at + 1 };
  const jobs = [
    { id: 101, post_id: 999, purpose: "collection", destination: "smut", status: "done", progress_bytes: 10, total_bytes: 10, dataset_id: null, upvotes: 123, search_tag: "dragon", favorite: saved, can_delete: true },
    { id: 102, post_id: 998, purpose: "collection", destination: "smut", status: "running", progress_bytes: 4, total_bytes: 10, dataset_id: null, upvotes: null, search_tag: null, can_delete: true },
    { id: 104, post_id: 996, purpose: "collection", destination: "smut", status: "done", progress_bytes: 10, total_bytes: 10, dataset_id: null, favorite: { ...off, post_id: 996, status: "queued" }, can_delete: false },
    { id: 103, post_id: 997, purpose: "dataset", destination: "frozen", status: "done", progress_bytes: 10, total_bytes: 10, dataset_id: "frozen-1", can_delete: false },
    { id: 105, post_id: 995, purpose: "collection", destination: "smut", status: "failed", progress_bytes: 0, total_bytes: null, dataset_id: null, can_delete: true },
  ];
  await page.route("**/api/downloads", (route) => route.fulfill({ json: { items: deleted ? jobs.slice(1) : jobs, active: 1 } }));
  await page.route("**/api/library/posts/999", (route) => route.fulfill({ json: deleted ? off : saved }));
  // Candidate initial data also seeds the heart cache.
  await page.route("**/api/projects/*/query?*", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({ response, json: { ...data, items: data.items.map((post: { post_id: number }) => post.post_id === 999 ? { ...post, favorite: saved } : post) } });
  });
  await page.reload();
  await expect(page.getByRole("button", { name: "Open post 999", exact: true }).locator(".favorite-paw")).toBeVisible();
  await page.route("**/api/downloads/101/delete", async (route) => {
    expect(route.request().postDataJSON().request_id).toBeTruthy();
    deleted = true;
    await route.fulfill({ json: { id: 101, status: "done", favorite: off } });
  });
  await page.getByRole("tab", { name: "Downloads", exact: true }).click();
  const complete = page.getByRole("article", { name: "Post 999 favourite", exact: true });
  await expect(complete.getByText("123 upvotes", { exact: true })).toBeVisible();
  await expect(complete.getByText("dragon", { exact: true })).toBeVisible();
  await expect(complete.getByRole("img")).toHaveAttribute("src", "/api/media/999/preview");
  await expect(complete.getByRole("progressbar")).toHaveCount(0);
  await expect(complete.getByText("Completed", { exact: true })).toHaveCSS("color", "rgb(156, 229, 173)");
  const running = page.getByRole("article", { name: "Post 998 favourite", exact: true });
  await expect(running.getByText("In progress", { exact: true })).toBeVisible();
  await expect(running.getByText("In progress", { exact: true })).toHaveCSS("color", "rgb(244, 209, 138)");
  const failed = page.getByRole("article", { name: "Post 995 favourite", exact: true });
  await expect(failed.getByText("Failed", { exact: true })).toHaveCSS("color", "rgb(255, 165, 173)");
  await expect(failed.getByRole("button", { name: "Retry", exact: true })).toBeVisible();
  await expect(running.getByText("Upvotes unavailable", { exact: true })).toBeVisible();
  await expect(running.getByRole("button", { name: "Delete post 998 favourite", exact: true })).toBeVisible();
  await expect(page.getByRole("article", { name: "Post 996 favourite", exact: true }).getByText("Deleting…", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Checked manifest", exact: true })).toBeVisible();
  await expect(page.getByText("collection", { exact: true })).toHaveCount(0);
  await complete.getByRole("button", { name: "Delete post 999 favourite", exact: true }).click();
  await expect(complete).toHaveCount(0);
  await page.getByRole("tab", { name: "Query", exact: true }).click();
  await expect(page.getByRole("button", { name: "Open post 999", exact: true }).locator(".favorite-paw")).toHaveCount(0);
});

test("real favourite download deletion removes its row and resets the gallery heart", async ({ page }) => {
  await open(page);
  const projectPath = new URL(page.url()).pathname;
  const project = await (await page.request.get(`/api${projectPath}`)).json();
  const heart = page.getByRole("button", { name: "Open post 998", exact: true });
  const existing = await (await page.request.get("/api/library/posts/998")).json();
  if (existing.wanted) await heart.press("f");
  if (existing.wanted || existing.present) {
    await expect.poll(async () => {
      const saved = await (await page.request.get("/api/library/posts/998")).json();
      return [saved.wanted, saved.present, saved.status];
    }).toEqual([false, false, "idle"]);
  }
  await expect(heart.locator(".favorite-paw")).toHaveCount(0);
  await heart.press("f");
  await expect(heart.locator(".favorite-paw")).toBeVisible();
  await expect.poll(async () => (await (await page.request.get("/api/library/posts/998")).json()).status).toBe("saved");
  await page.getByRole("tab", { name: "Downloads", exact: true }).click();
  const row = page.getByRole("article", { name: "Post 998 favourite", exact: true });
  await expect(row.getByText("Completed", { exact: true })).toBeVisible();
  await expect(row.getByText("203 upvotes", { exact: true })).toBeVisible();
  await expect(row.getByText(project.display_name, { exact: true })).toBeVisible();
  await expect(row.getByRole("img")).toHaveAttribute("src", "/api/media/998/preview");
  await row.getByRole("button", { name: "Delete post 998 favourite", exact: true }).click();
  await expect(row).toHaveCount(0);
  await expect.poll(async () => {
    const saved = await (await page.request.get("/api/library/posts/998")).json();
    return [saved.wanted, saved.present, saved.status];
  }).toEqual([false, false, "idle"]);
  await page.getByRole("tab", { name: "Query", exact: true }).click();
  await expect(heart.locator(".favorite-paw")).toHaveCount(0);
});
