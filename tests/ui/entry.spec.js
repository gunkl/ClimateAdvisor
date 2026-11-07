const { test, expect } = require('@playwright/test');

// Issue #1230 R6a: the panel's entry (frontend/entry.html) opens the new dashboard by default, classic when this
// browser chose classic; the #hash and query survive; "Back to classic" is remembered; classic links to the new one
// from every tab.

const KEY = 'climate_advisor_dashboard';
const setPref = (page, v) => page.addInitScript(([k, val]) => { try { localStorage.setItem(k, val); } catch (e) {} }, [KEY, v]);
const path = (page) => new URL(page.url()).pathname;

test.describe('Panel entry', () => {
  test('the chooser decision table (the real function from entry.html)', async ({ page, request }) => {
    const src = await (await request.get('/entry.html')).text();
    const fn = src.match(/function chooseDashboard\(pref, search\) \{[\s\S]*?\n  \}\n/)[0];
    const cases = [
      [null, '', 'new_dashboard.html'], ['new', '', 'new_dashboard.html'], ['classic', '', 'index.html'],
      ['garbage', '', 'new_dashboard.html'], ['new', '?classic=1', 'index.html'], [null, '?v=abc&classic=1', 'index.html'],
      ['classic', '?classic=1', 'index.html'], [null, '?classic=10', 'new_dashboard.html'], [null, '?v=abc', 'new_dashboard.html'],
    ];
    await page.goto('/prediction_accuracy.html');
    const got = await page.evaluate(([f, cs]) => { const choose = new Function(f + '; return chooseDashboard;')(); return cs.map(c => choose(c[0], c[1])); }, [fn, cases]);
    expect(got).toEqual(cases.map(c => c[2]));
  });
  test('unset opens the new dashboard', async ({ page }) => {
    await page.goto('/entry.html');
    await page.waitForURL(/new_dashboard\.html/);
    await expect(page.locator('#back-classic')).toBeVisible();
  });
  test('"new" opens the new dashboard, unknown values too', async ({ page }) => {
    await setPref(page, 'garbage');
    await page.goto('/entry.html');
    await page.waitForURL(/new_dashboard\.html/);
  });
  test('"classic" opens classic', async ({ page }) => {
    await setPref(page, 'classic');
    await page.goto('/entry.html');
    await page.waitForURL(/\/($|\?|#)|index\.html/);
    await expect(page.locator('h1')).toHaveText(/Climate Advisor/);
    expect(path(page)).not.toMatch(/new_dashboard/);
  });
  test('?classic=1 opens classic even when new is remembered', async ({ page }) => {
    await setPref(page, 'new');
    await page.goto('/entry.html?classic=1');
    await expect(page.locator('#new-dashboard-link')).toBeVisible();
    expect(path(page)).not.toMatch(/new_dashboard/);
  });
  test('storage that throws still opens the new dashboard', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(window, 'localStorage', { get() { throw new Error('blocked'); } });
    });
    await page.goto('/entry.html');
    await page.waitForURL(/new_dashboard\.html/);
  });
  test('a deep link keeps its hash', async ({ page }) => {
    await page.goto('/entry.html#analysis');
    await page.waitForURL(/new_dashboard\.html#analysis/);
    await expect(page.locator('#tab-analysis, [data-tab="analysis"]').first()).toBeVisible();
  });
  test('Back to classic is one click and remembered; the new link brings it back', async ({ page }) => {
    await page.goto('/entry.html');
    await page.waitForURL(/new_dashboard\.html/);
    await page.click('#back-classic');
    await expect(page.locator('#new-dashboard-link')).toBeVisible();
    expect(await page.evaluate((k) => localStorage.getItem(k), KEY)).toBe('classic');
    await page.goto('/entry.html');
    await expect(page.locator('#new-dashboard-link')).toBeVisible(); // still classic on the next visit
    await page.click('#new-dashboard-link');
    await page.waitForURL(/new_dashboard\.html/);
    expect(await page.evaluate((k) => localStorage.getItem(k), KEY)).toBe('new');
  });
  test('classic shows the new-dashboard link on every tab', async ({ page }) => {
    await page.goto('/');
    for (const t of ['status', 'ai', 'settings', 'debug']) {
      await page.click(`.tab-btn[data-tab="${t}"]`);
      await expect(page.locator('#new-dashboard-link')).toBeVisible();
    }
  });
});
