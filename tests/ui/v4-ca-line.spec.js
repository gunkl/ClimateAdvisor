const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

// Issue #1400: the live 0.9.4 model is "_ca"-only (primary "_ca", sensors ["_ca"]). The Status chart must draw v4's
// "Climate Advisor reading" line, broken at every bedtime / wake-up swap, and the Prediction accuracy page must score
// v3 and "no change" even while v4 has no scored points.

const engines = {
  primary: 'v4', shadow: 'none', fallback: false, fitting: false, primary_state: 'ready', text: '',
  engines: [
    { id: 'v3', label: 'v3', running: true, primary: false, shadow: false, usable: true, confidence: 'high' },
    { id: 'v4', label: 'v4', running: true, primary: true, shadow: false, usable: true, confidence: 'ok' },
  ],
};

function caOnlyV4(now) {
  const iso = (ms) => new Date(ms).toISOString();
  const half = 1800000;
  const past = Array.from({ length: 48 }, (_, i) => ({ ts: iso(now - (48 - i) * half), temp: 68 + (i % 5) * 0.2 }));
  const future = Array.from({ length: 48 }, (_, i) => ({ ts: iso(now + (i + 1) * half), temp: 69 + (i % 7) * 0.2 }));
  return {
    ready: true, reason: 'ok', scores: null, model: { confidence: 'ok', fitted_at: iso(now - 9 * 3600000) },
    leads: ['1h', '4h', '12h', '24h'], sensors: ['_ca'],
    past: { _ca: { '1h': past, '4h': past, '12h': past, '24h': past } },
    future: { _ca: future }, future_at: iso(now), baseline: { _ca: future },
    // one swap 6 h ago (recorded) and one 6 h ahead (planned)
    breaks: { _ca: [iso(now - 6 * 3600000 + 60000), iso(now + 6 * 3600000 + 60000)] },
    corrected_outdoor: { past: [], future: [] },
  };
}

async function openWith(page, now) {
  await page.route('**/api/climate_advisor/chart_data**', async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    body.prediction_engines = engines;
    body.v4 = caOnlyV4(now);
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
  await page.goto('/');
  await page.waitForFunction(() => document.getElementById('temp-chart') && typeof Chart !== 'undefined'
    && !!Chart.getChart(document.getElementById('temp-chart')), { timeout: 8000 });
}

test.describe('v4 _ca line on the Status chart (Issue #1400)', () => {
  test('a _ca-only model draws the "Climate Advisor reading" line, shown by default', async ({ page }) => {
    await openWith(page, Date.now());
    await expect(page.locator('#chart-key-body')).toContainText('Climate Advisor reading');
    const ds = await page.evaluate(() => {
      const c = Chart.getChart(document.getElementById('temp-chart'));
      const d = c.data.datasets.find((x) => x.caId === 'v4:_ca');
      return d ? { hidden: !!d.hidden, label: d.label, n: d.data.filter((p) => p.y != null).length } : null;
    });
    expect(ds).not.toBeNull();
    expect(ds.hidden).toBe(false);
    expect(ds.label).toBe('v4 Climate Advisor reading');
    expect(ds.n).toBe(96); // 48 archived (4 h ahead) + 48 future points
  });

  for (const id of ['v4:_ca', 'ind:_ca', 'v3']) test(`no ${id} segment crosses a sensor swap`, async ({ page }) => {
    const now = Date.now();
    await openWith(page, now);
    const res = await page.evaluate(([swaps, id]) => {
      const c = Chart.getChart(document.getElementById('temp-chart'));
      const pts = c.data.datasets.find((x) => x.caId === id).data;
      return swaps.filter((s) => s < Date.now() || id !== 'ind:_ca').map((s) => {
        // the points either side of the swap must be separated by a null (a break)
        let i = pts.findIndex((p) => p.x >= s);
        const before = pts.slice(0, i).filter((p) => p.y != null).pop();
        const gap = pts.slice(pts.indexOf(before) + 1, i).some((p) => p.y == null);
        return { found: i > 0, gap };
      });
    }, [[now - 6 * 3600000 + 60000, now + 6 * 3600000 + 60000], id]);
    // the actual reading has only the past swap; v4's and v3's lines run past NOW and have both
    expect(res).toEqual(id === 'ind:_ca' ? [{ found: true, gap: true }] : [{ found: true, gap: true }, { found: true, gap: true }]);
  });

  test('the no-plan baseline is there for _ca and starts off', async ({ page }) => {
    await openWith(page, Date.now());
    const base = await page.evaluate(() => {
      const c = Chart.getChart(document.getElementById('temp-chart'));
      const d = c.data.datasets.find((x) => x.caId === 'base:_ca');
      return d ? { hidden: !!d.hidden, n: d.data.filter((p) => p.y != null).length } : null;
    });
    expect(base).toEqual({ hidden: true, n: 48 });
  });
});

test.describe('Prediction accuracy page with an empty v4 archive (Issue #1400)', () => {
  test('v3 and "no change" scores show while v4 has none, and "_ca" is listed once', async ({ page }) => {
    const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, '../fixtures/prediction_accuracy_sample.json'), 'utf8'));
    fixture.points = fixture.points.map((p) => { const q = Object.assign({}, p); delete q.v4; return q; });
    fixture.stats = Object.assign({}, fixture.stats, { n: 0, v4_mae: null, v4_bias: null });
    fixture.sensors = [fixture.sensors[0], { id: '_ca', name: '_ca', role: 'room sensor' }, ...fixture.sensors.slice(1)];
    // the window is the fixture's own: freeze the clock inside it
    await page.route('**/api/climate_advisor/prediction_accuracy**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(fixture) }));
    await page.route('**/api/climate_advisor/status**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ zone_count: 1, zones: [] }) }));
    await page.goto('/prediction_accuracy.html');
    const tiles = page.locator('#tiles');
    await expect(tiles).toContainText('v3 average miss');
    await expect(tiles).toContainText(fixture.stats.v3_mae.toFixed(2));
    await expect(tiles).toContainText('“No change” guess');
    await expect(tiles).toContainText(fixture.stats.flat_mae.toFixed(2));
    await expect(tiles).toContainText('no v4 predictions scored here yet');
    await expect(page.locator('#empty-state')).toHaveCount(0);
    const names = await page.locator('#grp-sensor button').allTextContents();
    expect(names.filter((n) => n === 'Climate Advisor reading').length).toBe(1);
    expect(names).not.toContain('_ca');
  });
});
