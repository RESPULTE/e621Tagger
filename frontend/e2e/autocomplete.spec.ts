import { test, expect } from "@playwright/test";

test("selecting an alias suggestion replaces the typed tag", async ({ page, isMobile }) => {
  await page.route("**/api/tags/autocomplete?*", async (route) => {
    const term = new URL(route.request().url()).searchParams.get("q");
    await route.fulfill({ json: term === "belch" ? [{
      canonical_name: "burp", matched_alias: "belch", category: 0,
      post_count: 42, tag_id: 1,
    }] : [] });
  });
  await page.goto("/");
  const options = await (await page.request.get("/api/auth/options")).json();
  if (options.password_required) {
    await page.getByLabel("Studio password").fill("studio-browser-test-password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  }
  await page.getByRole("button", { name: "Projects", exact: true }).click();
  await page.getByRole("button", { name: "+ New project", exact: true }).click();
  const query = page.getByRole("combobox", { name: "New project e621 tag" });
  await query.fill("belch");
  const suggestion = page.getByRole("option", { name: /belch.*burp/ });
  await expect(suggestion).toBeVisible();
  if (isMobile) await suggestion.tap();
  else await suggestion.click();
  await expect(query).toHaveValue("burp ");
  await expect(query).toBeFocused();
  await expect(page.getByRole("listbox", { name: "Tag suggestions" })).toHaveCount(0);

  // Replacing a tag in the middle must preserve its operator and neighbors.
  await query.fill("solo -belch rating:s");
  await query.press("Home");
  for (let i = 0; i < 11; i++) await query.press("ArrowRight");
  await expect(suggestion).toBeVisible();
  if (isMobile) await suggestion.tap();
  else await suggestion.click();
  await expect(query).toHaveValue("solo -burp rating:s");
  await expect(query).toBeFocused();
  expect(await query.evaluate((input: HTMLInputElement) => input.selectionStart)).toBe(10);
});
