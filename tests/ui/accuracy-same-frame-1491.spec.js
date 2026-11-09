const { test, expect } = require('@playwright/test');

// Issue #1491: on a phone (the iOS Home Assistant app, and Safari) the Analysis tab's "Prediction accuracy" card showed
// a 401 and no data. The new dashboard already sits inside HA's panel frame; the accuracy page embedded one frame
// deeper looked for HA's login in window.parent (the dashboard, not HA) and fell back to localStorage 'hassTokens',
// which the iOS app never writes. "Open on its own page" and Debug's "Open the full accuracy view" opened a new tab
// with no HA around it at all. Owner decision: no frame inside a frame. The card shows a summary, both links open the
// page in the same frame, "Back to dashboard" returns to the tab the occupant left, and the login lookup also tries
// the top window.

const H = 3600e3;
const iso = (ms) => new Date(ms).toISOString();

function accuracyBody() {
  const start = Date.now() - 24 * H, points = [];
  for (let i = 0; i < 48; i++) {
    points.push({ t: iso(start + i * 30 * 60e3), actual: 68 + Math.sin(i / 6), v3: 68.5 + Math.sin(i / 6), v4: 67.8 + Math.sin(i / 6), flat: 68 });
  }
  return {
    zone: 'mock-zone', unit: '°F', sensor: '_ca', lead: '4h', range: '24h', start: iso(start), end: iso(Date.now()), downsample: 'raw',
    sensors: [{ id: '_ca', name: 'Climate Advisor reading', role: 'the reading Climate Advisor uses', has_v4: true, has_readings: true }],
    points, heat_runs: [],
    stats: { n: 48, n_v3: 48, n_flat: 48, v4_mae: 0.4, v3_mae: 0.6, flat_mae: 0.9, v4_bias: 0.1, v3_bias: 0.3 },
    model: { confidence: 'ok', fitted_at: iso(start), span_days: 30 },
    gate: [], gate_summary: null, primary_engine: 'v4', shadow_engine: 'v3', ca_swaps: [],
  };
}

// Answers prediction_accuracy (the mock server has no fixture for it) and records the Authorization header of every
// Climate Advisor request; everything else falls through to the mock server.
async function mockAccuracy(page) {
  const auth = [];
  await page.route('**/api/climate_advisor/**', (route) => {
    const req = route.request(), u = new URL(req.url());
    auth.push({ path: u.pathname, authorization: req.headers()['authorization'] || null });
    if (u.pathname.endsWith('/prediction_accuracy')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(accuracyBody()) });
    }
    return route.fallback();
  });
  return auth;
}

test.describe('Issue #1491: the accuracy page is never framed and never opened in a new tab', () => {
  test.describe.configure({ timeout: 30000 });

  test('Analysis card: the scores show with no frame (Issue #1497 draws the full view in the card)', async ({ page }) => {
    await mockAccuracy(page);
    await page.goto('/new_dashboard.html#analysis');
    const card = page.locator('#panel-analysis section[aria-labelledby="an-pa-h"]');
    await expect(card.locator('#tiles')).toContainText('average miss', { timeout: 15000 });
    expect(await page.locator('iframe').count()).toBe(0);
  });

  for (const [tab, linkId] of [['analysis', '#prediction-accuracy-link'], ['debug', '#dbg-bt-link']]) {
    test(`${tab}: the link opens the page in the same frame and Back returns to ${tab}`, async ({ page, context }) => {
      await mockAccuracy(page);
      let newTabs = 0;
      context.on('page', () => { newTabs += 1; });
      await page.goto('/new_dashboard.html#' + tab);
      await expect(page.locator('#health-text')).toContainText('Healthy', { timeout: 15000 });
      const link = page.locator(linkId);
      await expect(link).not.toHaveAttribute('target', /.+/);
      await link.click();
      await page.waitForURL(/prediction_accuracy\.html\?zone=mock-zone/);
      await expect(page.locator('#tiles')).toContainText('average miss', { timeout: 15000 });
      expect(newTabs).toBe(0);
      // "Back to dashboard" goes to entry.html (no #tab): the dashboard reopens on the tab the occupant left
      await page.locator('#back-link').click();
      await page.waitForURL(/new_dashboard\.html#/);
      await expect(page.locator('#tab-btn-' + tab)).toHaveAttribute('aria-selected', 'true');
      expect(new URL(page.url()).hash).toBe('#' + tab);
      // used once: the next plain open of the dashboard is the default tab again
      await page.goto('/entry.html');
      await page.waitForURL(/new_dashboard\.html/);
      await expect(page.locator('#tab-btn-status')).toHaveAttribute('aria-selected', 'true');
    });
  }

  // HA's panel frame, then one more frame: the old lookup (window.parent only) found no login there and, with no
  // 'hassTokens' in localStorage (the iOS app), sent no token at all.
  test('a page one frame deeper than HA still sends the login from the top window', async ({ page }) => {
    const auth = await mockAccuracy(page);
    const html = (body) => ({ status: 200, contentType: 'text/html', body: '<!doctype html><meta charset="utf-8">' + body });
    await page.route('**/nest-top-1491.html', (r) => r.fulfill(html(
      '<home-assistant></home-assistant><script>document.querySelector("home-assistant").hass =' +
      ' { auth: { data: { access_token: "top-token" }, refreshAccessToken: async function () {} } };</script>' +
      '<iframe src="/nest-mid-1491.html" style="width:1000px;height:800px"></iframe>')));
    await page.route('**/nest-mid-1491.html', (r) => r.fulfill(html(
      '<iframe src="/prediction_accuracy.html?zone=mock-zone" style="width:980px;height:780px"></iframe>')));
    await page.goto('/nest-top-1491.html');
    const inner = page.frameLocator('iframe').frameLocator('iframe');
    await expect(inner.locator('#tiles')).toContainText('average miss', { timeout: 15000 });
    const pa = auth.filter((a) => a.path.endsWith('/prediction_accuracy'));
    expect(pa.length).toBeGreaterThan(0);
    expect(pa.every((a) => a.authorization === 'Bearer top-token')).toBe(true);
  });
});
