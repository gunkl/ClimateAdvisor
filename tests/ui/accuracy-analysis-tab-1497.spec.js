const { test, expect } = require('@playwright/test');

// Issue #1497: the occupant wants the full Prediction accuracy view (chart, score tiles, "How each model has scored")
// right on the new dashboard's Analysis tab, not only behind "Open on its own page". Issue #1491's rule stands: no
// frame inside a frame (a page framed inside the dashboard could not reach Home Assistant's login on a phone). So the
// view is one shared script, pa_view.js, that the stand-alone page and the Analysis card both draw directly.

const H = 3600e3;
const iso = (ms) => new Date(ms).toISOString();
const ZONES = [{ entry_id: 'zone-a', title: 'Downstairs' }, { entry_id: 'zone-b', title: 'Upstairs' }];

function accuracyBody(zone, range) {
  const span = { '6h': 6, '12h': 12, '24h': 24, '3d': 72, '7d': 168, '30d': 720, '1y': 8760 }[range] || 72;
  const end = Date.now(), start = end - span * H, points = [];
  for (let t = start; t <= end; t += 30 * 60e3) {
    const i = (t - start) / (30 * 60e3);
    points.push({ t: iso(t), actual: 68 + Math.sin(i / 6), v3: 68.5 + Math.sin(i / 6), v4: 67.8 + Math.sin(i / 6), flat: 68 });
  }
  return {
    zone, unit: '°F', sensor: '_ca', lead: '4h', range, start: iso(start), end: iso(end), downsample: 'raw',
    sensors: [{ id: '_ca', name: 'Climate Advisor reading', role: 'the reading Climate Advisor uses', has_v4: true, has_readings: true }],
    points, heat_runs: [],
    // zone-b scores differently, so a zone switch is visible in the tiles
    stats: { n: points.length, n_v3: points.length, n_flat: points.length, v4_mae: zone === 'zone-b' ? 0.7 : 0.4, v3_mae: 0.6, flat_mae: 0.9, v4_bias: 0.1, v3_bias: 0.3 },
    model: { confidence: 'ok', fitted_at: iso(start), span_days: 30 },
    gate: [{ zone, season: 'heating', lead: '4h', v4: 0.4, v3: 0.6, flat: 0.9, bias: 0.1, n: 200, scored: true, beats_v3: true, beats_flat: true, bias_ok: true }],
    gate_summary: { scored: 1, passed: 1, min_n: 72, window_days: 30 },
    primary_engine: 'v4', shadow_engine: 'v3', ca_swaps: [],
  };
}

// Two zones, prediction_accuracy answered per zone and range; every Climate Advisor request is recorded with its
// Authorization header. The rest falls through to the mock server.
async function mockCA(page) {
  const reqs = [];
  await page.route('**/api/climate_advisor/**', (route) => {
    const req = route.request(), u = new URL(req.url());
    reqs.push({ url: req.url(), path: u.pathname, authorization: req.headers()['authorization'] || null });
    const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    if (u.pathname.endsWith('/zones')) return json({ zones: ZONES, zone_count: 2, version: 'mock' });
    if (u.pathname.endsWith('/status')) { // the mock server's status, listing both zones (the dashboard reads them there too)
      return route.fetch().then((r) => r.json()).then((b) => json(Object.assign(b, { zones: ZONES, zone_count: 2 })));
    }
    if (u.pathname.endsWith('/prediction_accuracy')) {
      const zone = u.searchParams.get('zone') || u.searchParams.get('entry_id');
      return json(accuracyBody(zone, u.searchParams.get('range') || '3d'));
    }
    return route.fallback();
  });
  const view = () => reqs.filter((r) => r.path.endsWith('/prediction_accuracy') && r.url.includes('sensor='));
  return { reqs, view };
}
const storeZone = (page, z) => page.addInitScript((zone) => { localStorage.setItem('climate_advisor_selected_zone', zone); }, z);

test.describe('Issue #1497: the full accuracy view on the Analysis tab, no frame', () => {
  test.describe.configure({ timeout: 30000 });

  test('the chart, the score tiles and "How each model has scored" are drawn in the card', async ({ page }) => {
    await storeZone(page, 'zone-a');
    await mockCA(page);
    await page.goto('/new_dashboard.html#analysis');
    const card = page.locator('#panel-analysis section[aria-labelledby="an-pa-h"]');
    await expect(card.locator('#tiles')).toContainText('v4 average miss', { timeout: 15000 });
    await expect(card.locator('#tiles')).toContainText('0.40 °F');
    await expect(card.locator('#pa-chart')).toBeVisible();
    expect(await page.evaluate(() => window.__paLines().map((l) => l.label))).toEqual(expect.arrayContaining(['Actual', 'v4']));
    await expect(card.locator('#gate-title')).toHaveText('How each model has scored');
    await expect(card.locator('#gate-body')).toContainText('Heating');
    await expect(card.locator('#grp-sensor')).toContainText('Climate Advisor reading');
    expect(await page.locator('iframe').count()).toBe(0);
    // the view has no zone picker or "Back" of its own on the dashboard (the dashboard's header has the zone picker)
    await expect(card.locator('#back-link')).toHaveCount(0);
    await expect(page.locator('#zone-select')).toHaveCount(1);
  });

  test('its controls work in place: a range click asks for that range, scoped to the zone', async ({ page }) => {
    await storeZone(page, 'zone-a');
    const ca = await mockCA(page);
    await page.goto('/new_dashboard.html#analysis');
    const card = page.locator('#panel-analysis section[aria-labelledby="an-pa-h"]');
    await expect(card.locator('#tiles')).toContainText('average miss', { timeout: 15000 });
    await card.locator('#grp-range button[data-value="24h"]').click();
    await expect(card.locator('#grp-range button[data-value="24h"]')).toHaveAttribute('aria-pressed', 'true');
    await expect.poll(() => ca.view().some((r) => r.url.includes('range=24h') && r.url.includes('zone=zone-a'))).toBe(true);
    await expect(page).toHaveURL(/new_dashboard\.html#analysis$/); // still on the dashboard
  });

  test('it follows the dashboard zone picker', async ({ page }) => {
    await storeZone(page, 'zone-a');
    const ca = await mockCA(page);
    await page.goto('/new_dashboard.html#analysis');
    const card = page.locator('#panel-analysis section[aria-labelledby="an-pa-h"]');
    await expect(card.locator('#tiles')).toContainText('0.40 °F', { timeout: 15000 });
    await page.locator('#zone-select').selectOption('zone-b');
    await expect(card.locator('#tiles')).toContainText('0.70 °F');
    expect(ca.view().some((r) => r.url.includes('zone=zone-b'))).toBe(true);
  });

  test('a zone changed on another tab is picked up when Analysis is shown again', async ({ page }) => {
    await storeZone(page, 'zone-a');
    const ca = await mockCA(page);
    await page.goto('/new_dashboard.html#analysis');
    const card = page.locator('#panel-analysis section[aria-labelledby="an-pa-h"]');
    await expect(card.locator('#tiles')).toContainText('0.40 °F', { timeout: 15000 });
    await page.locator('#tab-btn-status').click();
    await page.locator('#zone-select').selectOption('zone-b');
    await page.locator('#tab-btn-analysis').click();
    await expect(card.locator('#tiles')).toContainText('0.70 °F');
    expect(ca.view().every((r) => /[?&]zone=zone-[ab]/.test(r.url))).toBe(true);
  });

  // The real geometry: HA's page on top, the dashboard in HA's panel frame. The view sends HA's own login.
  test('inside a panel frame it sends the login of the Home Assistant around it', async ({ page }) => {
    const ca = await mockCA(page);
    await page.route('**/ha-top-1497.html', (r) => r.fulfill({
      status: 200, contentType: 'text/html',
      body: '<!doctype html><meta charset="utf-8"><home-assistant></home-assistant><script>' +
        'document.querySelector("home-assistant").hass = { auth: { data: { access_token: "ha-token" }, refreshAccessToken: async function () {} } };' +
        '</script><iframe src="/new_dashboard.html#analysis" style="width:1200px;height:900px"></iframe>',
    }));
    await page.goto('/ha-top-1497.html');
    const dash = page.frameLocator('iframe');
    await expect(dash.locator('#an-pa-view #tiles')).toContainText('average miss', { timeout: 15000 });
    expect(await dash.locator('iframe').count()).toBe(0);
    const pa = ca.view();
    expect(pa.length).toBeGreaterThan(0);
    expect(pa.every((r) => r.authorization === 'Bearer ha-token')).toBe(true);
  });

  test('the stand-alone page still draws the same view with its own header', async ({ page }) => {
    await storeZone(page, 'zone-b');
    await mockCA(page);
    await page.goto('/prediction_accuracy.html');
    await expect(page.locator('#tiles')).toContainText('0.70 °F', { timeout: 15000 });
    await expect(page.locator('#pa-title')).toHaveText('Prediction accuracy');
    await expect(page.locator('#zone-wrap')).toBeVisible(); // two zones: the page's own picker shows
    await expect(page.locator('#zone-select')).toHaveValue('zone-b');
    await page.locator('#zone-select').selectOption('zone-a');
    await expect(page.locator('#tiles')).toContainText('0.40 °F');
    await expect(page.locator('#back-link')).toHaveAttribute('href', 'entry.html');
  });

  test('at phone width the card fits without sideways scroll', async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 });
    await storeZone(page, 'zone-a');
    await mockCA(page);
    await page.goto('/new_dashboard.html#analysis');
    await expect(page.locator('#an-pa-view #tiles')).toContainText('average miss', { timeout: 15000 });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });
});
