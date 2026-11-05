import { test, expect, type Page } from '@playwright/test';
import { createReviewProject } from './project-fixture';
async function login(page: Page) {
  await page.goto('/');
  await page.getByLabel('Studio password').fill('studio-browser-test-password');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
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
test('pull needs continuous 1.5s hold and swipe reveals trash', async ({ page, isMobile, browserName }) => {
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
  const b = (await list.boundingBox())!, x = b.x + b.width / 2, y = b.y + 18;
  await input('touchStart', x, y); await input('touchMove', x, y + 80);
  await page.waitForTimeout(500); await input('touchEnd', x, y + 80);
  await page.waitForTimeout(1150);
  const query = page.getByRole('combobox', { name: 'New project e621 tag' });
  await expect(query).toHaveCount(0);
  await input('touchStart', x, y); await input('touchMove', x, y + 80);
  await page.waitForTimeout(1000); await expect(query).toHaveCount(0);
  await expect(query).toBeVisible({ timeout: 1600 });
  await input('touchEnd', x, y + 80);
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  const r = (await row.boundingBox())!;
  await input('touchStart', r.x + 160, r.y + 20, row);
  await input('touchMove', r.x + 70, r.y + 20, row);
  await input('touchEnd', r.x + 70, r.y + 20, row);
  const trash = page.getByRole('button', { name: `Delete project ${p.display_name}`, exact: true });
  await expect(trash).toBeVisible(); expect(new URL(page.url()).pathname).toBe(path);
  await trash.click(); await expect(page).toHaveURL(/\/$/);
});
