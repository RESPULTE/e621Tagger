import { expect, type Page } from "@playwright/test";
import { randomUUID } from "node:crypto";

// Seed isolated review data through the API; custom targets have no creation UI.
export async function createReviewProject(page: Page, prefix: string) {
  const session = await (await page.request.get("/api/auth/session")).json();
  const response = await page.request.post("/api/projects", {
    headers: {
      "X-CSRF-Token": session.csrf_token,
      Origin: new URL(page.url()).origin,
    },
    data: {
      request_id: randomUUID(),
      target_kind: "custom",
      name: `${prefix}_${randomUUID()}`,
      query: "dragon",
    },
  });
  expect(response.ok()).toBe(true);
  const project = await response.json();
  await page.goto(`/projects/${project.id}?q=dragon`);
}
