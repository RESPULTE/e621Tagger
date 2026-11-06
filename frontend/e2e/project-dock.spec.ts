import { test, expect, type Page } from '@playwright/test';
import { createReviewProject } from './project-fixture';
async function login(page: Page) {
  await page.goto('/');
  const options = await (await page.request.get('/api/auth/options')).json();
  if (options.password_required) {
    await page.getByLabel('Studio password').fill('studio-browser-test-password');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  }
  await expect(page.getByRole('button', { name: 'Projects', exact: true })).toBeVisible();
}
async function dock(page: Page) {
  await page.getByRole('button', { name: 'Projects', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Projects', exact: true })).toBeVisible();
}
async function tag(page: Page, name: string) {
  await dock(page);
  await page.getByRole('button', { name: '+ New project', exact: true }).click();
  await page.getByRole('combobox', { name: 'New project e621 tag' }).fill(name);
  // These fixture tags have suggestions; let the inline results settle before
  // clicking a submit button whose position changes as they arrive.
  await expect(page.getByRole('listbox', { name: 'Tag suggestions' })).toBeVisible();
  await page.getByRole('button', { name: 'Let’s collect', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Projects', exact: true })).toHaveCount(0);
}
test('no setup, alias reuse, last project, viewer switching and deletion', async ({ page }) => {
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  await login(page);
  await expect(page.getByText('No project created yet.', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Projects', exact: true })).toHaveCount(0);
  await tag(page, 'drake');
  const dragon = new URL(page.url()).pathname;
  await tag(page, 'dragon');
  expect(new URL(page.url()).pathname).toBe(dragon);
  await page.goto('/'); await expect(page).toHaveURL(new RegExp(dragon + '$'));
  await createReviewProject(page, 'dock_scope');
  const path = new URL(page.url()).pathname;
  const p = await (await page.request.get('/api' + path)).json();
  await page.getByRole('button', { name: 'Open post 999', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(path + '/posts/999$'));
  await dock(page);
  await page.locator(`.project-dock-row[data-project-id="${dragon.split('/').pop()}"] .project-dock-select`).click();
  await expect(page).toHaveURL(new RegExp(dragon + '/posts/999$'));
  await dock(page);
  await page.locator(`.project-dock-row[data-project-id="${p.id}"] .project-dock-select`).click();
  await expect(page).toHaveURL(new RegExp(path + '/posts/999$'));
  await page.getByRole('button', { name: 'Positive', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get('/api' + path)).json()).counts.positive).toBe(1);
  await dock(page);
  await expect(page.locator(`.project-dock-row[data-project-id="${p.id}"]`).getByLabel('1 positive', { exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('project-dock.png'), fullPage: true });
  await page.getByRole('button', { name: `Show delete action for ${p.display_name}`, exact: true }).click();
  let dialogs = 0; page.on('dialog', d => { dialogs++; void d.dismiss(); });
  await page.getByRole('button', { name: `Delete project ${p.display_name}`, exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect.poll(() => page.evaluate(() => localStorage.getItem('studio-last-project'))).toBeNull();
  expect((await page.request.get('/api' + path)).status()).toBe(404);
  expect(dialogs).toBe(0); expect(errors).toEqual([]);
});
test('deselecting keeps browse context and media actions without reviews', async ({ page }) => {
  await login(page);
  await createReviewProject(page, 'dock_browse');
  const gallery = new URL(page.url());
  const path = gallery.pathname;
  const project = await (await page.request.get('/api' + path)).json();

  await page.getByRole('button', { name: 'Open post 999', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(path + "/posts/999$"));
  const viewerUrl = page.url();
  const positive = page.getByRole('button', { name: 'Positive', exact: true });
  await expect(positive).toBeVisible();

  await dock(page);
  const row = page.locator(`.project-dock-row[data-project-id="${project.id}"]`);
  await expect(row.locator('.project-dock-select')).toHaveAttribute('aria-pressed', 'true');
  await row.locator('.project-dock-select').click();
  await expect(page).toHaveURL(viewerUrl);
  await expect(page.getByRole('button', { name: 'Projects', exact: true })).toHaveAttribute('title', 'Find your fixation');
  await expect(page.locator('.detail-review')).toHaveCount(0);
  await expect(page.locator('.quick-labels')).toHaveCount(0);
  await expect(page.locator('.media-stage')).toBeVisible();

  const browseProject = await page.evaluate(() => localStorage.getItem('studio-last-project'));
  expect(browseProject).toBe(String(project.id));
  expect(await page.evaluate(() => localStorage.getItem('studio-browse-only'))).toBe('true');

  let labelWrites = 0;
  page.on('request', request => {
    if (request.method() !== 'GET' && request.url().includes('/labels/')) labelWrites++;
  });
  await page.locator('h1').click();
  await page.keyboard.press('1');
  await page.keyboard.press('2');
  await page.keyboard.press('z');
  await expect.poll(async () => (await (await page.request.get('/api' + path)).json()).counts).toEqual(project.counts);

  // Collection favourites remain independent of whether this project is selected.
  const favoriteUrl = '/api/library/posts/999';
  const initialFavorite = await (await page.request.get(favoriteUrl)).json();
  const media = page.locator('.media-stage');
  await media.press('f');
  await expect.poll(async () => (await (await page.request.get(favoriteUrl)).json()).wanted).toBe(!initialFavorite.wanted);
  await media.press('f');
  await expect.poll(async () => (await (await page.request.get(favoriteUrl)).json()).wanted).toBe(initialFavorite.wanted);

  expect(labelWrites).toBe(0);

  // Reselecting restores review controls; deselect again to check the persisted state.
  await dock(page);
  await row.locator('.project-dock-select').click();
  await expect(positive).toBeVisible();
  await dock(page);
  await row.locator('.project-dock-select').click();
  await page.reload();
  await expect(page).toHaveURL(viewerUrl);
  await expect(page.locator('.detail-review')).toHaveCount(0);
  await expect(page.locator('.media-stage')).toBeVisible();

  await page.getByRole('link', { name: 'Thumbnail gallery', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`${path}(?:\\?.*)?$`));
  await expect(page.getByRole('button', { name: 'Open post 999', exact: true })).toBeVisible();
  await expect(page.locator('.quick-labels')).toHaveCount(0);

  await page.setViewportSize({ width: 320, height: 740 });
  await dock(page);
  await page.locator('.project-dock-panel').evaluate(async (element) => {
    await Promise.all(element.getAnimations().map((animation) => animation.finished));
  });
  const heading = page.locator('.project-dock-heading h2');
  const newProject = page.getByRole('button', { name: '+ New project', exact: true });
  await expect(page.getByRole('button', { name: 'Close projects', exact: true })).toHaveCount(0);
  const headingBox = await heading.boundingBox();
  const buttonBox = await newProject.boundingBox();
  expect(headingBox).not.toBeNull();
  expect(buttonBox).not.toBeNull();
  const overlaps = headingBox!.x < buttonBox!.x + buttonBox!.width && headingBox!.x + headingBox!.width > buttonBox!.x &&
    headingBox!.y < buttonBox!.y + buttonBox!.height && headingBox!.y + headingBox!.height > buttonBox!.y;
  expect(overlaps).toBe(false);
  expect(Math.abs(headingBox!.y + headingBox!.height / 2 - buttonBox!.y - buttonBox!.height / 2)).toBeLessThan(1);
  expect(headingBox!.x).toBeGreaterThanOrEqual(0);
  expect(buttonBox!.x + buttonBox!.width).toBeLessThanOrEqual(320);
  await page.screenshot({ path: test.info().outputPath('compact-project-dock.png'), fullPage: true });
});

test('swipe reveals trash', async ({ page, isMobile, browserName }) => {
  await login(page); await createReviewProject(page, 'dock_gesture');
  const path = new URL(page.url()).pathname;
  const p = await (await page.request.get('/api' + path)).json();
  await dock(page);
  const list = page.locator('.project-dock-list');
  const row = page.locator(`.project-dock-row[data-project-id="${p.id}"]`);
  await expect(row).toBeVisible();
  const cdp = isMobile && browserName === 'chromium' ? await page.context().newCDPSession(page) : null;
  async function input(type: 'touchStart' | 'touchMove' | 'touchEnd', x: number, y: number, target = list) {
    if (cdp) await cdp.send('Input.dispatchTouchEvent', { type, touchPoints: type === 'touchEnd' ? [] : [{ x, y }] });
    else if (isMobile) await target.evaluate((el, a) => {
      const t = { identifier: 1, target: el, clientX: a.x, clientY: a.y };
      const event = new Event(a.type.toLowerCase(), { bubbles: true, cancelable: true });
      Object.defineProperty(event, 'touches', { value: a.type === 'touchEnd' ? [] : [t] });
      Object.defineProperty(event, 'changedTouches', { value: [t] });
      el.dispatchEvent(event);
    }, { type, x, y });
    else if (type === 'touchStart') { await page.mouse.move(x, y); await page.mouse.down(); }
    else if (type === 'touchEnd') await page.mouse.up();
    else await page.mouse.move(x, y, { steps: 5 });
  }
  const r = (await row.boundingBox())!;
  await input('touchStart', r.x + 160, r.y + 20, row);
  await input('touchMove', r.x + 70, r.y + 20, row);
  await input('touchEnd', r.x + 70, r.y + 20, row);
  const trash = page.getByRole('button', { name: `Delete project ${p.display_name}`, exact: true });
  await expect(trash).toBeVisible(); expect(new URL(page.url()).pathname).toBe(path);
  await trash.click(); await expect(page).toHaveURL(/\/$/);
});
