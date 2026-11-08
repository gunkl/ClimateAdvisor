const { test, expect } = require('@playwright/test');

// Issue #1431: the Prediction accuracy page (standalone and embedded on the dashboards' Analysis tab).
// Occupant view: the page must explain itself, name the engines by the role they have in Settings, never draw a line
// straight across the bedtime / wake-up sensor swap, never offer a sensor button that opens an empty chart, and draw
// consecutive predictions as a line (dots only where predictions really are spaced out).

const START = Date.parse('2026-11-01T00:00:00Z');
const H = 3600e3;
const SWAPS = [START + 6 * H, START + 22 * H]; // wake-up and bedtime inside the 24 h window
const iso = (ms) => new Date(ms).toISOString();

// v4: 'dense' = every 30-minute slot, 'hourly' = the backtest's hourly points, 'sparse' = one every 3 h,
// 'gap' = every slot except a 3 h hole, 'none' = no v4 at all.
function payload({ sensor = '_ca', v4 = 'dense', primary = 'v4', shadow = 'v3', swaps = SWAPS } = {}) {
  const points = [];
  for (let i = 0; i < 48; i++) {
    const t = START + i * 30 * 60e3;
    const p = { t: iso(t), actual: 68 + Math.sin(i / 6) };
    if (sensor === '_ca') p.v3 = 68.5 + Math.sin(i / 6);
    if (i >= 8) p.flat = 68 + Math.sin((i - 8) / 6);
    const hour = i % 2 === 0;
    const inHole = i >= 20 && i < 26;
    if ((v4 === 'dense') || (v4 === 'hourly' && hour) || (v4 === 'sparse' && i % 6 === 0) || (v4 === 'gap' && !inHole)) {
      p.v4 = 67.8 + Math.sin(i / 6);
    }
    points.push(p);
  }
  return {
    zone: 'z1', unit: '°F', sensor, lead: '4h', range: '24h',
    start: iso(START), end: iso(START + 24 * H), downsample: 'raw',
    sensors: [
      { id: '_ca', name: 'Climate Advisor reading', role: 'the reading Climate Advisor uses', has_v4: true, has_readings: true },
      { id: 'sensor.hallway', name: 'Hallway', role: 'thermostat', has_v4: true, has_readings: true },
      { id: 'sensor.bedroom', name: 'Bedroom', role: 'bedroom sensor', has_v4: false, has_readings: true },
      { id: 'sensor.attic', name: 'Attic', role: 'room sensor', has_v4: false, has_readings: false },
    ],
    points, heat_runs: [],
    stats: { n: 10, n_v3: 48, n_flat: 40, v4_mae: 0.4, v3_mae: 0.6, flat_mae: 0.9, v4_bias: 0.1, v3_bias: 0.3 },
    model: { confidence: 'ok', fitted_at: iso(START), span_days: 30 },
    gate: [], gate_summary: null, primary_engine: primary, shadow_engine: shadow,
    ca_swaps: swaps.map(iso),
  };
}

async function mock(page, opts = {}) {
  const seen = [];
  await page.route('**/api/climate_advisor/status**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ zone_count: 1, zones: [] }) }));
  await page.route('**/api/climate_advisor/prediction_accuracy**', (route) => {
    const q = new URL(route.request().url()).searchParams;
    const sensor = q.get('sensor') || '_ca';
    seen.push(sensor);
    const v4 = sensor === 'sensor.bedroom' ? 'none' : (opts.v4 || 'dense');
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload({ ...opts, sensor, v4 })) });
  });
  await page.goto('/prediction_accuracy.html');
  await expect(page.locator('#tiles')).toContainText('average miss');
  return seen;
}

const lines = (page) => page.evaluate(() => window.__paLines());

// Consecutive non-null x values of one line, i.e. the segments the chart draws.
function segments(data) {
  const out = [];
  for (let i = 1; i < data.length; i++) if (data[i - 1] != null && data[i] != null) out.push([data[i - 1], data[i]]);
  return out;
}

test.describe('Prediction accuracy (Issue #1431)', () => {
  test('1. engine labels follow the primary / comparison roles, no pre-O3 wording', async ({ page }) => {
    await mock(page, { primary: 'v4', shadow: 'v3' });
    await expect(page.locator('#legend-v4-name')).toHaveText('v4 (main engine)');
    await expect(page.locator('#legend-v3-name')).toHaveText('v3 (comparison)');
    const body = await page.locator('body').innerText();
    for (const stale of ['today’s line', "today's line", '(new)', 'Preview', 'preview', 'runs in shadow']) {
      expect(body).not.toContain(stale);
    }
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await mock(page, { primary: 'v3', shadow: 'v4' });
    await expect(page.locator('#legend-v3-name')).toHaveText('v3 (main engine)');
    await expect(page.locator('#legend-v4-name')).toHaveText('v4 (comparison)');
    await expect(page.locator('#tiles .tile').first()).toContainText('v3 average miss');
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await mock(page, { primary: 'v3', shadow: 'none' });
    await expect(page.locator('#legend-v4-name')).toHaveText('v4 (not running now)');
  });

  test('1. the dashboards no longer call it a preview of a shadow engine', async ({ page }) => {
    await page.goto('/new_dashboard.html');
    const sub = page.locator('#panel-analysis section[aria-labelledby="an-pa-h"] .sub');
    const text = await sub.textContent();
    expect(text).not.toMatch(/Preview|shadow/);
    expect(text).toContain('main engine');
    await page.goto('/index.html?classic=1');
    await expect(page.locator('#prediction-accuracy-link')).not.toContainText('preview');
  });

  test('2. a plain line explains the points; a sensor without v4 says so in words', async ({ page }) => {
    await mock(page);
    await expect(page.locator('#pa-explain')).toContainText('Each point is a past prediction');
    await expect(page.locator('#pa-explain')).toContainText('lone dots mean the predictions there are spaced out');
    await expect(page.locator('#pa-nov4')).toBeHidden();
    await page.locator('#grp-sensor button[data-value="sensor.bedroom"]').click();
    await expect(page.locator('#pa-nov4')).toBeVisible();
    await expect(page.locator('#pa-nov4')).toContainText('v4 hasn’t learned this sensor yet (see #1422)');
    // the chart is not empty: the actual line is still drawn
    const ls = await lines(page);
    expect(ls.find((l) => l.label === 'Actual').data.filter((x) => x != null).length).toBe(48);
    expect(ls.find((l) => l.label === 'v4').data.length).toBe(0);
  });

  test('3. no line crosses a bedtime / wake-up sensor swap', async ({ page }) => {
    await mock(page);
    await expect(page.locator('#legend-swap')).toBeVisible();
    await expect(page.locator('#pa-explain')).toContainText('bedtime and wake-up');
    const ls = await lines(page);
    expect(ls.map((l) => l.label).sort()).toEqual(['Actual', 'No change', 'v3', 'v4']);
    for (const l of ls) {
      for (const [a, b] of segments(l.data)) {
        for (const s of SWAPS) expect(a < s && s <= b, `${l.label} crosses ${iso(s)}`).toBe(false);
      }
      expect(l.data.filter((x) => x == null).length, l.label).toBe(2); // one break per swap, nothing else
    }
  });

  test('3. a single sensor (not the CA reading) is not broken at the swaps', async ({ page }) => {
    await mock(page);
    await page.locator('#grp-sensor button[data-value="sensor.hallway"]').click();
    await expect(page.locator('#grp-sensor button[data-value="sensor.hallway"]')).toHaveAttribute('aria-pressed', 'true');
    const ls = await lines(page);
    expect(ls.find((l) => l.label === 'Actual').data.filter((x) => x == null).length).toBe(0);
    await expect(page.locator('#legend-swap')).toBeHidden();
  });

  test('4. sensor buttons with no data are labelled or disabled, with the reason on hover', async ({ page }) => {
    const seen = await mock(page);
    const bed = page.locator('#grp-sensor button[data-value="sensor.bedroom"]');
    await expect(bed).toContainText('no v4 data yet');
    await expect(bed).toHaveAttribute('title', /#1422/);
    await expect(bed).not.toHaveAttribute('aria-disabled', 'true');
    const attic = page.locator('#grp-sensor button[data-value="sensor.attic"]');
    await expect(attic).toHaveAttribute('aria-disabled', 'true');
    await expect(attic).toHaveAttribute('title', /Nothing recorded for this sensor yet/);
    await attic.click({ force: true }); // aria-disabled: Playwright (rightly) refuses a normal click
    await page.waitForTimeout(300);
    expect(seen).not.toContain('sensor.attic');
    await expect(page.locator('#grp-sensor button[data-value="_ca"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('#grp-sensor button[data-value="_ca"]')).not.toContainText('no v4 data');
  });

  test('5. dense 30-minute v4 points draw one line between the swaps', async ({ page }) => {
    await mock(page, { v4: 'dense', swaps: [] });
    const v4 = (await lines(page)).find((l) => l.label === 'v4');
    expect(v4.data.length).toBe(48);
    expect(v4.data.filter((x) => x == null).length).toBe(0);
  });

  test('5. hourly (backtest) v4 points also join into a line, not dots', async ({ page }) => {
    await mock(page, { v4: 'hourly', swaps: [] });
    const v4 = (await lines(page)).find((l) => l.label === 'v4');
    expect(v4.data.filter((x) => x != null).length).toBe(24);
    expect(v4.data.filter((x) => x == null).length).toBe(0);
  });

  test('5. a real gap breaks the line once; sparse points stay dots', async ({ page }) => {
    await mock(page, { v4: 'gap', swaps: [] });
    let v4 = (await lines(page)).find((l) => l.label === 'v4');
    expect(v4.data.filter((x) => x == null).length).toBe(1);
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await mock(page, { v4: 'sparse', swaps: [] });
    v4 = (await lines(page)).find((l) => l.label === 'v4');
    const real = v4.data.filter((x) => x != null);
    expect(real.length).toBe(8);
    expect(segments(v4.data).length).toBe(0); // every point stands alone: drawn as a dot
  });

  test('5. a backend without ca_swaps / has_v4 still draws (older payload)', async ({ page }) => {
    await page.route('**/api/climate_advisor/status**', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ zone_count: 1, zones: [] }) }));
    await page.route('**/api/climate_advisor/prediction_accuracy**', (route) => {
      const d = payload();
      delete d.ca_swaps;
      d.sensors = d.sensors.map(({ has_v4, has_readings, ...s }) => s);
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(d) });
    });
    await page.goto('/prediction_accuracy.html');
    await expect(page.locator('#tiles')).toContainText('average miss');
    const v4 = (await lines(page)).find((l) => l.label === 'v4');
    expect(v4.data.filter((x) => x == null).length).toBe(0);
    await expect(page.locator('#grp-sensor button[aria-disabled="true"]')).toHaveCount(0);
  });
});
