const { test, expect } = require('@playwright/test');

// Issue #1454: an "Ambiguous zone selection" Repairs card appeared after a Home Assistant restart although nothing
// was misconfigured. Climate Advisor's own dashboards raised it: a status call that got a 503 while Climate Advisor
// was starting was retried with no zone, and a first visit (nothing stored) asked an unscoped /status for the zone
// list. The owner wants the card raised on every unscoped call, so the dashboards must never send one: the zone list
// comes from GET /zones (names no zone by design, raises nothing) and a failed scoped call is retried, scoped, after a
// back-off (2 s, 5 s, 10 s), then the page says "Climate Advisor is starting".
//
// Every page here records every request it (and its iframes) makes; ZERO Climate Advisor requests other than /zones
// may lack entry_id / zone, and the page must end up showing the zone's data.

const ZONES = [
  { entry_id: 'zone-a', title: 'Downstairs' },
  { entry_id: 'zone-b', title: 'Upstairs' },
];
const H = 3600e3;
const START = Date.now() - 24 * H;
const iso = (ms) => new Date(ms).toISOString();

function statusBody() {
  return {
    day_type: 'mild', hvac_mode: 'off', indoor_temp: 71, outdoor_temp: 65, automation_enabled: true,
    occupancy_mode: 'home', automation_status: 'active', compliance_score: 1.0, unit: 'fahrenheit',
    zones: ZONES, zone_count: ZONES.length, version: 'mock',
  };
}
function accuracyBody(zone) {
  const points = [];
  for (let i = 0; i < 48; i++) {
    const t = START + i * 30 * 60e3;
    points.push({ t: iso(t), actual: 68 + Math.sin(i / 6), v3: 68.5 + Math.sin(i / 6), v4: 67.8 + Math.sin(i / 6), flat: 68 });
  }
  return {
    zone, unit: '°F', sensor: '_ca', lead: '4h', range: '3d', start: iso(START), end: iso(START + 24 * H), downsample: 'raw',
    sensors: [{ id: '_ca', name: 'Climate Advisor reading', role: 'the reading Climate Advisor uses', has_v4: true, has_readings: true }],
    points, heat_runs: [],
    stats: { n: 48, n_v3: 48, n_flat: 48, v4_mae: 0.4, v3_mae: 0.6, flat_mae: 0.9, v4_bias: 0.1, v3_bias: 0.3 },
    model: { confidence: 'ok', fitted_at: iso(START), span_days: 30 },
    gate: [], gate_summary: null, primary_engine: 'v4', shadow_engine: 'v3', ca_swaps: [],
  };
}

const SCOPED = /[?&](entry_id|zone)=[^&]+/;

// down: how many scoped status / prediction_accuracy calls fail with 503 (a restart) before Climate Advisor answers;
// while it is down every other scoped call 503s too and /zones lists nothing (no zone loaded yet). down = Infinity
// keeps it down.
async function mockCA(page, { down = 0 } = {}) {
  const urls = [];
  const state = { down, scopedStatus: 0 };
  page.on('request', (req) => {
    const u = new URL(req.url());
    if (u.pathname.startsWith('/api/climate_advisor/')) urls.push(req.url());
  });
  await page.route('**/api/climate_advisor/**', (route) => {
    const u = new URL(route.request().url());
    const name = u.pathname.replace('/api/climate_advisor/', '');
    const zone = u.searchParams.get('zone') || u.searchParams.get('entry_id');
    const json = (status, body) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (name === 'zones') return json(200, state.down > 0 ? { zones: [], zone_count: 0 } : { zones: ZONES, zone_count: 2 });
    if (!zone) { // the real backend raises the Repairs card here; the page must never get this far
      return json(name === 'status' ? 200 : 400, { zone_selection_required: true, zones: ZONES, zone_count: 2 });
    }
    if (state.down > 0) {
      if (name === 'status' || name === 'prediction_accuracy') state.down -= 1;
      return json(503, { error: 'Climate Advisor not loaded' });
    }
    if (name === 'status') { state.scopedStatus += 1; return json(200, statusBody()); }
    if (name === 'prediction_accuracy') return json(200, accuracyBody(zone));
    return route.fallback(); // the mock server's fixtures (chart_data, timeline, night_strategy, briefing, ...)
  });
  const unscoped = () => urls.filter((x) => !new URL(x).pathname.endsWith('/zones') && !SCOPED.test(x));
  return { urls, unscoped, state };
}

const storeZone = (page, z) => page.addInitScript((zone) => { localStorage.setItem('climate_advisor_selected_zone', zone); }, z);
const stored = (page) => page.evaluate(() => localStorage.getItem('climate_advisor_selected_zone'));

async function visitNewTabs(page) { // fire every tab's own fetches (Analysis embeds the accuracy page)
  for (const tab of ['analysis', 'debug', 'settings', 'status']) {
    await page.locator('#tab-btn-' + tab).click();
    await page.waitForTimeout(400);
  }
}

test.describe('Issue #1454: the dashboards never send an unscoped request', () => {
  test.describe.configure({ timeout: 40000 });

  // (a) restart: the stored zone's calls 503 a few times, then succeed
  test('classic, restart: the 503 is retried with the same zone and the zone data shows', async ({ page }) => {
    const ca = await mockCA(page, { down: 2 });
    await storeZone(page, 'zone-b');
    await page.goto('/');
    await expect(page.locator('#status-grid')).toBeVisible({ timeout: 20000 });
    await expect(page.locator('#status-grid')).toContainText('Following today’s plan');
    await expect(page.locator('#zone-selector-row .zone-tab-btn.active')).toHaveText('Upstairs');
    await page.waitForTimeout(800); // the re-fired loads after the recovery
    expect(ca.unscoped()).toEqual([]);
    expect(ca.urls.some((u) => u.includes('chart_data') && u.includes('entry_id=zone-b'))).toBe(true);
    expect(await stored(page)).toBe('zone-b');
  });

  test('new dashboard, restart: the 503 is retried with the same zone and every tab stays scoped', async ({ page }) => {
    const ca = await mockCA(page, { down: 2 });
    await storeZone(page, 'zone-b');
    await page.goto('/new_dashboard.html');
    await expect(page.locator('#health-text')).toContainText('Healthy', { timeout: 20000 });
    await expect(page.locator('#zone-select')).toHaveValue('zone-b');
    await visitNewTabs(page);
    await page.locator('#tab-btn-analysis').click();
    await expect(page.frameLocator('#pa-frame').locator('#tiles')).toContainText('average miss');
    expect(ca.unscoped()).toEqual([]);
    // the Status chart's request (temperature, and the #1414 humidity / feels-like series) names the zone
    expect(ca.urls.some((u) => u.includes('chart_data') && u.includes('entry_id=zone-b'))).toBe(true);
    expect(await stored(page)).toBe('zone-b');
  });

  test('accuracy page, restart: retried with the same zone, then the scores show', async ({ page }) => {
    const ca = await mockCA(page, { down: 2 });
    await storeZone(page, 'zone-b');
    await page.goto('/prediction_accuracy.html');
    await expect(page.locator('#tiles')).toContainText('average miss', { timeout: 20000 });
    expect(ca.unscoped()).toEqual([]);
    expect(ca.urls.filter((u) => u.includes('prediction_accuracy')).every((u) => u.includes('zone=zone-b'))).toBe(true);
  });

  // (b) first load, empty localStorage: the zone list first, then only scoped calls
  test('classic, first visit: /zones first, the first zone is picked and stored, no unscoped call', async ({ page }) => {
    const ca = await mockCA(page);
    await page.goto('/');
    await expect(page.locator('#status-grid')).toContainText('Following today’s plan');
    await page.waitForTimeout(800);
    expect(ca.urls[0]).toContain('/api/climate_advisor/zones');
    expect(ca.unscoped()).toEqual([]);
    expect(await stored(page)).toBe('zone-a');
  });

  test('new dashboard, first visit: /zones first, then every tab scoped to the first zone', async ({ page }) => {
    const ca = await mockCA(page);
    await page.goto('/new_dashboard.html');
    await expect(page.locator('#health-text')).toContainText('Healthy');
    await visitNewTabs(page);
    await page.locator('#tab-btn-analysis').click();
    await expect(page.frameLocator('#pa-frame').locator('#tiles')).toContainText('average miss');
    expect(ca.urls[0]).toContain('/api/climate_advisor/zones');
    expect(ca.unscoped()).toEqual([]);
    expect(await stored(page)).toBe('zone-a');
  });

  test('accuracy page, first visit: /zones first, then the scores for the first zone', async ({ page }) => {
    const ca = await mockCA(page);
    await page.goto('/prediction_accuracy.html');
    await expect(page.locator('#tiles')).toContainText('average miss');
    expect(ca.urls[0]).toContain('/api/climate_advisor/zones');
    expect(ca.unscoped()).toEqual([]);
    expect(ca.urls.some((u) => u.includes('prediction_accuracy') && u.includes('zone=zone-a'))).toBe(true);
  });

  // (a) + (b): a first visit while Climate Advisor is still starting (no zone listed yet): nothing goes out unscoped
  for (const [label, path] of [['classic', '/'], ['new dashboard', '/new_dashboard.html']]) {
    test(`${label}, first visit during a restart: waits for a zone, then loads it`, async ({ page }) => {
      const ca = await mockCA(page, { down: 1 });
      // the first status attempt is never sent (no zone yet), so let /zones flip on its own after 1 s
      setTimeout(() => { ca.state.down = 0; }, 1000);
      await page.goto(path);
      if (path === '/') await expect(page.locator('#status-grid')).toContainText('Following today’s plan', { timeout: 20000 });
      else await expect(page.locator('#health-text')).toContainText('Healthy', { timeout: 20000 });
      await page.waitForTimeout(800);
      expect(ca.unscoped()).toEqual([]);
      expect(await stored(page)).toBe('zone-a');
    });
  }

  // still down after the back-off: a visible "starting" state, still nothing unscoped
  test('classic and new dashboard: still down after 2 s, 5 s, 10 s says Climate Advisor is starting', async ({ browser }) => {
    for (const [path, sel] of [['/', '#status-loading'], ['/new_dashboard.html', '#health-text']]) {
      const page = await browser.newPage();
      const ca = await mockCA(page, { down: Infinity });
      await storeZone(page, 'zone-a');
      await page.goto(path);
      await expect(page.locator(sel)).toContainText('Climate Advisor is starting', { timeout: 25000 });
      expect(ca.unscoped()).toEqual([]);
      await page.close();
    }
  });

  test('a single-zone install names its zone too, and its chart choices saved before are kept', async ({ page }) => {
    await page.addInitScript(() => {
      localStorage.setItem('ca_chart_v2:default', JSON.stringify({ on: {}, range: '6h' }));
    });
    const urls = [];
    page.on('request', (req) => { if (req.url().includes('/api/climate_advisor/')) urls.push(req.url()); });
    await page.goto('/'); // the mock server's /zones lists one zone, 'mock-zone'
    await expect(page.locator('#status-grid')).toBeVisible();
    await page.waitForTimeout(800);
    expect(urls.filter((u) => !u.includes('/zones') && !SCOPED.test(u))).toEqual([]);
    const saved = await page.evaluate(() => JSON.parse(localStorage.getItem('ca_chart_v2:mock-zone') || 'null'));
    expect(saved && saved.range).toBe('6h');
  });
});
