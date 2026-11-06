import { test, expect } from '@playwright/test';

test('mascot reacts on opening and collapse from the bottom right', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' });
  await page.goto('/');
  const options = await (await page.request.get('/api/auth/options')).json();
  if (options.password_required) {
    await page.getByLabel('Studio password').fill('studio-browser-test-password');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  }
  const trigger = page.getByRole('button', { name: 'Projects', exact: true });
  const mascot = trigger.locator('.project-dock-mascot');
  const panel = page.locator('.project-dock-panel');
  await expect(trigger).toBeVisible();
  for (const image of await mascot.locator('img').all()) {
    await expect.poll(() => image.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0)).toBe(true);
    expect(await image.evaluate((el: HTMLImageElement) => {
      const canvas = document.createElement('canvas');
      canvas.width = el.naturalWidth;
      canvas.height = el.naturalHeight;
      const context = canvas.getContext('2d')!;
      context.drawImage(el, 0, 0);
      return context.getImageData(0, 0, 1, 1).data[3];
    })).toBe(0);
  }
  const box = (await trigger.boundingBox())!;
  const viewport = page.viewportSize()!;
  expect(viewport.width - box.x - box.width).toBeCloseTo(16, 0);
  expect(viewport.height - box.y - box.height).toBeCloseTo(16, 0);
  await trigger.hover();
  await expect(trigger).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
  await expect(trigger).toHaveCSS('border-width', '0px');
  await expect(trigger).toHaveCSS('box-shadow', 'none');
  await page.mouse.down();
  await expect(trigger).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
  // Move off the button before releasing so this check does not open the dock.
  await page.mouse.move(0, 0);
  await page.mouse.up();
  await expect(mascot.locator('.project-dock-mascot-normal')).toHaveCSS('opacity', '1');
  await trigger.click();
  await expect(mascot).toHaveClass(/is-blushing/);
  await expect(mascot.locator('.project-dock-mascot-blush')).toHaveCSS('opacity', '1');
  await expect(mascot).toHaveCSS('animation-name', 'project-mascot-bounce');
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
  await expect(mascot).not.toHaveClass(/is-blushing/);
  await expect(mascot.locator('.project-dock-mascot-normal')).toHaveCSS('opacity', '1');
  const panelBox = (await panel.boundingBox())!;
  expect(panelBox.x).toBeGreaterThanOrEqual(0);
  expect(panelBox.x + panelBox.width).toBeLessThanOrEqual(viewport.width);
  await page.screenshot({ path: test.info().outputPath('mascot-open.png') });

  // Tapping within the popup keeps it open; tapping outside collapses it.
  await page.locator('.project-dock-heading h2').click();
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
  await page.locator('.site-header').click({ position: { x: 5, y: 5 } });
  await expect(trigger).toHaveAttribute('aria-expanded', 'false');
  await expect(mascot).toHaveClass(/is-blushing/);
  await expect(panel).toHaveClass(/is-closing/);
  await expect(panel).toHaveCSS('animation-name', 'project-refurl');
  await expect(panel).toHaveCount(0);
  await expect(mascot).not.toHaveClass(/is-blushing/);
  // Reopening during an exit must cancel it and leave the panel usable.
  await trigger.click();
  await trigger.click();
  await trigger.click();
  await expect(trigger).toHaveAttribute('aria-expanded', 'true');
  await expect(panel).not.toHaveClass(/is-closing/);
  await expect(page.getByRole('dialog', { name: 'Projects', exact: true })).toBeVisible();

  await page.emulateMedia({ reducedMotion: 'reduce' });
  await expect(mascot).toHaveCSS('animation-duration', '0.001s');
  await page.keyboard.press('Escape');
  await expect(panel).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(mascot).not.toHaveClass(/is-blushing/);
});
