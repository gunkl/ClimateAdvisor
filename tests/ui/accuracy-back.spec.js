const { test, expect } = require('@playwright/test');

// The "How each engine has scored" page (prediction_accuracy.html) has a "Back to dashboard" button that looks like
// the new dashboard's "Back to classic" button and returns to the dashboard this browser uses (via entry.html and
// the remembered 'climate_advisor_dashboard' choice), not always to classic.

const KEY = 'climate_advisor_dashboard';
const setPref = (page, v) => page.addInitScript(([k, val]) => { try { localStorage.setItem(k, val); } catch (e) {} }, [KEY, v]);
const PROPS = ['display', 'alignItems', 'minHeight', 'paddingLeft', 'paddingRight', 'borderTopWidth', 'borderTopStyle',
  'borderTopColor', 'borderTopLeftRadius', 'backgroundColor', 'color', 'fontSize', 'textDecorationLine'];
const look = (loc) => loc.evaluate((el, ps) => { const cs = getComputedStyle(el); return ps.map(p => cs[p]); }, PROPS);

test.describe('Prediction accuracy: Back to dashboard', () => {
  test('is a button with the shared .btn style, pointing at the entry', async ({ page }) => {
    await page.goto('/prediction_accuracy.html');
    const b = page.locator('#back-link');
    await expect(b).toBeVisible();
    await expect(b).toHaveText('Back to dashboard');
    await expect(b).toHaveClass(/\bbtn\b/);
    await expect(b).toHaveAttribute('href', 'entry.html');
  });

  for (const scheme of ['light', 'dark']) {
    test(`matches "Back to classic" in ${scheme} mode`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await page.goto('/new_dashboard.html');
      const ref = await look(page.locator('#back-classic'));
      await page.goto('/prediction_accuracy.html');
      expect(await look(page.locator('#back-link'))).toEqual(ref);
    });
  }

  test('fits at phone width without horizontal scroll', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 740 });
    await page.goto('/prediction_accuracy.html');
    const b = page.locator('#back-link');
    await expect(b).toBeVisible();
    const box = await b.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(375);
    expect(box.height).toBeGreaterThanOrEqual(36);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test('opens the new dashboard by default', async ({ page }) => {
    await page.goto('/prediction_accuracy.html');
    await page.locator('#back-link').click();
    await page.waitForURL(/new_dashboard\.html/);
    await expect(page.locator('#back-classic')).toBeVisible();
  });

  test('opens classic when this browser chose classic', async ({ page }) => {
    await setPref(page, 'classic');
    await page.goto('/prediction_accuracy.html');
    await page.locator('#back-link').click();
    await page.waitForURL(/index\.html/);
    await expect(page.locator('#new-dashboard-link')).toBeVisible();
    expect(new URL(page.url()).pathname).not.toMatch(/new_dashboard/);
  });

  test('opened from classic with nothing stored, it returns to classic', async ({ page }) => {
    // classic opened for one visit (?classic=1 saves nothing), then its own link to the scoring page
    await page.goto('/index.html?classic=1');
    const link = page.locator('#prediction-accuracy-link');
    await expect(link).toHaveAttribute('href', 'prediction_accuracy.html?from=classic');
    expect(await page.evaluate((k) => localStorage.getItem(k), KEY)).toBeNull();
    await page.goto('/' + (await link.getAttribute('href')));
    const b = page.locator('#back-link');
    await expect(b).toHaveAttribute('href', 'index.html');
    await b.click();
    await page.waitForURL(/index\.html$/);
    await expect(page.locator('#new-dashboard-link')).toBeVisible();
  });

  test('from=classic keeps the rest of the query', async ({ page }) => {
    await page.goto('/prediction_accuracy.html?from=classic&zone=abc');
    await expect(page.locator('#back-link')).toHaveAttribute('href', 'index.html?zone=abc');
  });
});
