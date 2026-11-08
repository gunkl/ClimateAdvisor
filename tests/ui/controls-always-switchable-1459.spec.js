const { test, expect } = require('@playwright/test');

// Issue #1459: every chart-key control can be switched on unless the home can't have it; an empty view says why.
// Occupant view: feels-like only has points on hot, humid or cold, windy readings, so a person could only switch it on
// when the weather happened to line up in view; a strip that was on vanished when nothing happened; a home with no
// outdoor sensor saw a grey chip saying "no readings in the loaded time" as if waiting would help. Now a control with
// nothing in view stays switchable (faint, with the reason) and its choice is kept; it is locked only when
// chart_data.capabilities says the home cannot have it, and the lock says where to set it up. Lines and strips a home
// can never have (no outdoor sensor, no fan, no door/window sensors, no occupancy switches) get no chip at all; one
// muted line in their group names them and where to set them up.

const KEY = 'ca_chart_v2:default';
const H = 3600000;
const SENSORS = [
  { id: 'sensor.living', name: 'Living room', role: 'thermostat' },
  { id: 'sensor.bed', name: 'Bedroom', role: 'bedroom sensor' },
];
const ALL_CAPS = { v4_running: true, outdoor_sensor: true, fan: true, door_sensors: true, occupancy: true, humidity_indoor: true, humidity_outdoor: true };

const engines = (v4Running) => ({
  primary: 'v3', shadow: 'v4', fallback: false, fitting: false, primary_state: 'ready', text: '',
  engines: [
    { id: 'v3', label: 'v3', running: true, primary: true, shadow: false, usable: true, confidence: 'high' },
    { id: 'v4', label: 'v4', running: v4Running, primary: false, shadow: true, usable: true, confidence: 'medium' },
  ],
});

// state.o is read on every fetch, so a test can change the payload and reload.
function routeChart(page, state) {
  return page.route('**/api/climate_advisor/chart_data**', async (route) => {
    const o = Object.assign({ caps: ALL_CAPS, ready: true, v4Running: true, feels: false }, state.o);
    const resp = await route.fetch();
    const body = await resp.json();
    const now = Date.now();
    const pts = (from, n, base) => Array.from({ length: n }, (_, i) => ({ ts: new Date(now + (from + i) * H).toISOString(), temp: base + i * 0.1 }));
    body.indoor_sensors = SENSORS;
    body.actual_indoor_sensors = {};
    SENSORS.forEach((s, k) => { body.actual_indoor_sensors[s.id] = body.actual_indoor.map(p => ({ time: p.time, temp: p.temp + k * 0.5 })); });
    body.prediction_engines = engines(o.v4Running);
    // feels-like: none at all, or a heat index on the last 3 h of rows
    body.state_log = body.state_log.map(e => {
      const r = Object.assign({}, e);
      delete r.feelslike; delete r.outdoor_feelslike;
      if (o.feels && Date.now() - Date.parse(r.ts) <= 3 * H) r.feelslike = 84;
      return r;
    });
    delete body.outdoor_feelslike_forecast;
    if (o.ready) {
      const past = {}, future = {}, baseline = {};
      SENSORS.concat([{ id: '_ca' }]).forEach(s => {
        past[s.id] = { '1h': pts(-12, 12, 69), '4h': pts(-12, 12, 69.5), '12h': [], '24h': [] };
        future[s.id] = pts(0.5, 12, 70);
        baseline[s.id] = pts(0.5, 12, 71);
      });
      body.v4 = { ready: true, sensors: SENSORS.map(s => s.id).concat(['_ca']), past, future, baseline,
        corrected_outdoor: { past: [], future: pts(0.5, 12, 60) } };
    } else {
      // still learning (or switched off): the CA-reading line exists but has nothing yet
      body.v4 = { ready: false, sensors: ['_ca'], past: {}, future: {}, baseline: {} };
    }
    if (o.caps) body.capabilities = o.caps; else delete body.capabilities;
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
}

async function open(page, url, o, seed) {
  const state = { o: o || {} };
  if (seed) {
    await page.addInitScript(([k, v]) => {
      if (sessionStorage.getItem('seeded')) return;
      sessionStorage.setItem('seeded', '1');
      localStorage.setItem(k, JSON.stringify(v));
    }, [KEY, seed]);
  }
  await routeChart(page, state);
  await page.goto(url);
  await ready(page);
  return state;
}
async function ready(page) {
  await page.waitForFunction(() => document.getElementById('temp-chart') && typeof Chart !== 'undefined'
    && !!Chart.getChart(document.getElementById('temp-chart')) && !!Chart.getChart(document.getElementById('out-chart')), { timeout: 8000 });
  await expect(page.locator('#chart-key-body [data-series="ind:_ca"]')).toHaveCount(1);
  if (!(await page.evaluate(() => document.getElementById('chart-key').open))) await page.locator('#chart-key > summary').click();
}
const chip = (page, id) => page.locator('#chart-key-body [data-series="' + id + '"]');
const stored = (page) => page.evaluate((k) => (JSON.parse(localStorage.getItem(k) || '{}').on || {}), KEY);
const ds = (page, id) => page.evaluate((sid) => {
  for (const cid of ['temp-chart', 'out-chart']) {
    const c = Chart.getChart(document.getElementById(cid));
    const i = c.data.datasets.findIndex(d => d.caId === sid);
    if (i >= 0) return { visible: c.isDatasetVisible(i), n: c.data.datasets[i].data.filter(p => p.y != null).length };
  }
  return null;
}, id);
const strips = (page) => page.evaluate(() => ({
  rows: window.__test_strips.rows, empty: window.__test_strips.empty,
  h: document.getElementById('activity-chart').style.height,
  shown: document.getElementById('activity-chart').parentElement.style.display !== 'none',
}));

for (const url of ['/', '/new_dashboard.html']) {
  test.describe(`Issue #1459: chart controls stay switchable (${url})`, () => {
    test('feels-like with nothing in view: switchable, remembered, and drawn once there is data', async ({ page }) => {
      const state = await open(page, url);
      const fl = chip(page, 'ind:feelslike');
      await expect(fl).toBeEnabled();
      await expect(fl).toHaveClass(/no-data/);
      await expect(fl).toHaveAttribute('aria-pressed', 'false');
      await expect(fl).toHaveAttribute('title', /heat index/);
      await expect(fl).toHaveAttribute('aria-label', /: Indoor feels-like only shows as a heat index/);
      await fl.click();
      await expect(fl).toHaveAttribute('aria-pressed', 'true');   // shows its real on state, though nothing is drawn
      await expect(fl).toHaveClass(/no-data/);
      expect((await stored(page))['ind:feelslike']).toBe(true);
      expect((await ds(page, 'ind:feelslike')).n).toBe(0);
      // the weather lines up: after a reload with feels-like in view, the remembered choice draws the line
      state.o = { feels: true };
      await page.reload();
      await ready(page);
      await expect(fl).toHaveAttribute('aria-pressed', 'true');
      await expect(fl).not.toHaveClass(/no-data/);
      const d = await ds(page, 'ind:feelslike');
      expect(d.visible).toBe(true);
      expect(d.n).toBeGreaterThan(3);
    });

    test('a strip that is on keeps its row, with the reason, when nothing is in view; the strip area does not jump', async ({ page }) => {
      await open(page, url);
      await expect(chip(page, 'strip:away')).toBeEnabled();
      await expect(chip(page, 'strip:away')).toHaveAttribute('aria-pressed', 'true');
      await expect(chip(page, 'strip:away')).toHaveClass(/no-data/);
      const live = await strips(page);
      expect(live.shown).toBe(true);
      expect(live.rows).toContain('away');                         // nothing away in the loaded time, row still there
      expect(live.empty.away).toMatch(/No away \/ vacation recorded/);
      expect(live.rows).toContain('heating');
      expect(live.empty.heating).toBeUndefined();
      await expect(page.locator('#activity-chart')).toHaveAttribute('aria-label', /Away \/ Vacation: No away/);
      for (let i = 0; i < 3; i++) await page.evaluate(() => shiftChartWindow(-1));   // into history with nothing in it
      await page.waitForFunction(() => window.__test_strips && window.__test_strips.empty.heating, null, { timeout: 8000 });
      const past = await strips(page);
      expect(past.rows).toEqual(live.rows);
      expect(past.h).toBe(live.h);
      await expect(chip(page, 'strip:heating')).toBeEnabled();
      await expect(chip(page, 'strip:heating')).toHaveAttribute('aria-pressed', 'true');
    });

    test('a look-ahead button with nothing in view is choosable and the row says "none in this view"', async ({ page }) => {
      await open(page, url);
      const b12 = page.locator('#chart-lead-row [data-lead="12h"]');
      await expect(b12).toBeEnabled();
      await b12.click();
      await expect(page.locator('#chart-lead-row [data-lead="12h"]')).toHaveAttribute('aria-pressed', 'true');
      await expect(page.locator('#chart-lead-row #chart-lead-empty')).toContainText('none in this view');
      await expect(page.locator('#chart-lead-row #chart-lead-help')).toContainText('left of Now');
      expect(await page.evaluate(() => window.__chartState().lead)).toBe('12h');
    });

    test('v4 still learning: its chips stay switchable, with the note', async ({ page }) => {
      await open(page, url, { ready: false });
      for (const id of ['v4:_ca', 'out:corrected']) {
        await expect(chip(page, id)).toBeEnabled();
        await expect(chip(page, id)).toHaveAttribute('title', /still learning/);
      }
      const ca = chip(page, 'v4:_ca');
      const was = await ca.getAttribute('aria-pressed');
      await ca.click();
      await expect(ca).toHaveAttribute('aria-pressed', was === 'true' ? 'false' : 'true');
      await expect(chip(page, 'base:_ca')).toBeEnabled();
    });

    test('no outdoor sensor: no Outdoor sensor chip, one line says where to set it up', async ({ page }) => {
      await open(page, url, { caps: Object.assign({}, ALL_CAPS, { outdoor_sensor: false }) });
      await expect(chip(page, 'out:sensor')).toHaveCount(0);
      await expect(chip(page, 'out:service')).toBeEnabled();
      await expect(page.locator('#chart-out-not-set-up')).toHaveText('Outdoor sensor: No outdoor sensor configured (Settings → Sensors)');
      expect((await ds(page, 'out:sensor')).visible).toBe(false);
    });

    test('no fan: no Fan or Nat-vent chip and no row even if stored on; one line says where to set it up', async ({ page }) => {
      await open(page, url, { caps: Object.assign({}, ALL_CAPS, { fan: false }) }, { on: { 'strip:fan': true, 'strip:natvent': true } });
      await expect(chip(page, 'strip:fan')).toHaveCount(0);
      await expect(chip(page, 'strip:natvent')).toHaveCount(0);
      await expect(chip(page, 'strip:heating')).toBeEnabled();
      await expect(page.locator('#chart-strips-not-set-up')).toHaveText('Fan, Nat-vent: No fan configured (Settings → Equipment)');
      const s = await strips(page);
      expect(s.rows).not.toContain('fan');
      expect(s.rows).not.toContain('natvent');
      expect(s.rows).toContain('heating');
    });

    test('v4 switched off: every v4 control is locked with the Settings reason', async ({ page }) => {
      await open(page, url, { caps: Object.assign({}, ALL_CAPS, { v4_running: false }), ready: false, v4Running: false });
      for (const id of ['v4:_ca', 'base:_ca', 'out:corrected']) {
        await expect(chip(page, id)).toBeDisabled();
        await expect(chip(page, id)).toHaveAttribute('aria-pressed', 'false');
        await expect(chip(page, id)).toHaveAttribute('title', 'v4 is switched off (Settings → Prediction engine)');
      }
      // the look-ahead buttons too (a v4 payload that still carries lines, e.g. switched off since the fetch)
      await page.unrouteAll({ behavior: 'ignoreErrors' });
      await routeChart(page, { o: { caps: Object.assign({}, ALL_CAPS, { v4_running: false }) } });
      await page.reload();
      await ready(page);
      await expect(page.locator('#chart-lead-row [data-lead]')).toHaveCount(4);
      await expect(page.locator('#chart-lead-row [data-lead]:disabled')).toHaveCount(4);
      await expect(page.locator('#chart-lead-row #chart-lead-help')).toHaveText('v4 is switched off (Settings → Prediction engine)');
      await expect(chip(page, 'v4:sensor.living')).toBeDisabled();
      expect((await ds(page, 'v4:sensor.living')).visible).toBe(false);
    });

    test('no capabilities (an older backend): nothing is locked, even with v4 off and nothing in view', async ({ page }) => {
      await open(page, url, { caps: null, ready: false, v4Running: false });
      await expect(page.locator('#chart-key-body [data-series]')).not.toHaveCount(0);
      await expect(page.locator('#chart-key-body [data-series]:disabled')).toHaveCount(0);
      await expect(chip(page, 'out:sensor')).toBeEnabled();
      await expect(chip(page, 'strip:fan')).toHaveCount(1);
      await expect(page.locator('#chart-strips-not-set-up, #chart-out-not-set-up')).toHaveCount(0);
      await expect(chip(page, 'v4:_ca')).toBeEnabled();
      // and in history with nothing at all in view, still nothing locks
      for (let i = 0; i < 3; i++) await page.evaluate(() => shiftChartWindow(-1));
      await page.waitForFunction(() => window.__test_strips && window.__test_strips.empty.heating, null, { timeout: 8000 });
      await expect(page.locator('#chart-key-body [data-series]:disabled')).toHaveCount(0);
    });
  });
}
