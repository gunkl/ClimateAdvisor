const { test, expect } = require('@playwright/test');

// Issue #1230 (UI batch, lane B): the Status chart remembers every chip per browser and zone (localStorage
// 'ca_chart_v2:<zone>'), starts with "CA reading" as the only indoor reading line, remembers engine lines by ROLE
// (primary / shadow) so switching engines keeps the choice, and says in words what the "Past v4 lines" row does.

const KEY = 'ca_chart_v2:default';
const SENSORS = [
  { id: 'sensor.living', name: 'Living room', role: 'main' },
  { id: 'sensor.bed', name: 'Bedroom', role: 'other' },
];

const engines = (primary, shadow) => ({
  primary, shadow, fallback: false, fitting: false, primary_state: 'ready', text: '',
  engines: [
    { id: 'v3', label: 'v3', running: true, primary: primary === 'v3', shadow: shadow === 'v3', usable: true, confidence: 'high' },
    { id: 'v4', label: 'v4', running: true, primary: primary === 'v4', shadow: shadow === 'v4', usable: true, confidence: 'medium' },
  ],
});

// Adds two indoor sensors and (optionally) v4 lines with archived past predictions to the mock chart payload.
const withSensors = (opts) => async (route) => {
  const o = Object.assign({ primary: 'v3', shadow: 'v4', ready: true, past: true }, opts || {});
  const resp = await route.fetch();
  const body = await resp.json();
  const now = Date.now();
  const pts = (from, n, step, base) => Array.from({ length: n }, (_, i) => ({ ts: new Date(now + (from + i * step) * 3600000).toISOString(), temp: base + i * 0.1 }));
  body.indoor_sensors = SENSORS;
  body.actual_indoor_sensors = {};
  SENSORS.forEach((s, k) => {
    body.actual_indoor_sensors[s.id] = body.actual_indoor.map(p => ({ time: p.time, temp: p.temp + k * 0.5 }));
  });
  body.prediction_engines = engines(o.primary, o.shadow);
  const past = {}, future = {}, baseline = {};
  SENSORS.forEach(s => {
    past[s.id] = o.past ? { '1h': pts(-12, 12, 1, 69), '4h': pts(-12, 12, 1, 69.5), '12h': [], '24h': [] } : {};
    future[s.id] = pts(0.5, 12, 1, 70);
    baseline[s.id] = pts(0.5, 12, 1, 71);
  });
  body.v4 = { ready: o.ready, past, future, baseline };
  await route.fulfill({ response: resp, body: JSON.stringify(body) });
};

async function open(page, opts) {
  await page.route('**/api/climate_advisor/chart_data**', withSensors(opts));
  await page.goto('/');
  await page.waitForFunction(() => document.getElementById('temp-chart') && typeof Chart !== 'undefined'
    && !!Chart.getChart(document.getElementById('temp-chart')), { timeout: 8000 });
  await expect(page.locator('#chart-key-body [data-series="ind:_ca"]')).toHaveCount(1);
  if (!(opts && opts.folded) && !(await page.evaluate(() => document.getElementById('chart-key').open))) {
    await page.locator('#chart-key > summary').click(); // the key starts folded: open it as a person would
    await expect(page.locator('#chart-key > summary')).toHaveAttribute('aria-expanded', 'true');
  }
}
const chip = (page, id) => page.locator('#chart-key-body [data-series="' + id + '"]');
const hidden = (page, id) => page.evaluate((sid) => {
  const ds = Chart.getChart(document.getElementById('temp-chart')).data.datasets.filter(d => d.caId === sid);
  return ds.length ? ds.every(d => d.hidden) : null;
}, id);

test.describe('Status chart: remembered choices and defaults', () => {
  test('with nothing stored, CA reading is the only indoor reading line on', async ({ page }) => {
    await open(page);
    await expect(chip(page, 'ind:_ca')).toHaveAttribute('aria-pressed', 'true');
    await expect(chip(page, 'ind:sensor.living')).toHaveAttribute('aria-pressed', 'false');
    await expect(chip(page, 'ind:sensor.bed')).toHaveAttribute('aria-pressed', 'false');
    expect(await hidden(page, 'ind:_ca')).toBe(false);
    expect(await hidden(page, 'ind:sensor.living')).toBe(true);
    // non-sensor lines keep their defaults: the primary (v3) and the shadow (v4) on, the "no plan" baseline off
    await expect(chip(page, 'v3')).toHaveAttribute('aria-pressed', 'true');
    await expect(chip(page, 'v4:sensor.living')).toHaveAttribute('aria-pressed', 'true');
    await expect(chip(page, 'base:sensor.living')).toHaveAttribute('aria-pressed', 'false');
  });

  test('sensor and line choices survive a reload', async ({ page }) => {
    await open(page);
    await chip(page, 'ind:sensor.bed').click();
    await chip(page, 'ind:_ca').click();
    await chip(page, 'base:sensor.living').click();
    await chip(page, 'out:service').click();
    await page.reload();
    await page.waitForFunction(() => !!Chart.getChart(document.getElementById('temp-chart')), { timeout: 8000 });
    await expect(chip(page, 'ind:sensor.bed')).toHaveAttribute('aria-pressed', 'true');
    await expect(chip(page, 'ind:_ca')).toHaveAttribute('aria-pressed', 'false');
    await expect(chip(page, 'ind:sensor.living')).toHaveAttribute('aria-pressed', 'false');
    await expect(chip(page, 'base:sensor.living')).toHaveAttribute('aria-pressed', 'true');
    await expect(chip(page, 'out:service')).toHaveAttribute('aria-pressed', 'false');
    expect(await hidden(page, 'ind:sensor.bed')).toBe(false);
    expect(await hidden(page, 'ind:_ca')).toBe(true);
  });

  test('the main line stays off when the primary engine changes (remembered by role)', async ({ page }) => {
    await open(page, { primary: 'v3', shadow: 'v4' });
    await chip(page, 'v3').click(); // the primary line off
    await expect(chip(page, 'v3')).toHaveAttribute('aria-pressed', 'false');
    const stored = await page.evaluate((k) => JSON.parse(localStorage.getItem(k)), KEY);
    expect(stored.on['role:primary:_ca']).toBe(false);
    expect(stored.on.v3).toBeUndefined();
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    await open(page, { primary: 'v4', shadow: 'v3' });
    // v4 is now the main engine: its lines are still off; v3 is now the comparison and keeps its default (on)
    await expect(chip(page, 'v4:sensor.living')).toHaveAttribute('aria-pressed', 'false');
    await expect(chip(page, 'v4:sensor.bed')).toHaveAttribute('aria-pressed', 'false');
    expect(await hidden(page, 'v4:sensor.living')).toBe(true);
    await expect(chip(page, 'v3')).toHaveAttribute('aria-pressed', 'true');
  });

  test('an older store (engine ids + pairing) is moved to roles', async ({ page }) => {
    await page.addInitScript((k) => {
      if (!sessionStorage.getItem('seeded')) {
        localStorage.setItem(k, JSON.stringify({ on: { v3: false, 'v4:sensor.bed': false }, range: '24h', ahead: '24h', lead: '4h', table: false, model: 'v3+v4' }));
        sessionStorage.setItem('seeded', '1');
      }
    }, KEY);
    await open(page, { primary: 'v4', shadow: 'v3' });
    // the old main line (v3) was off: the new main engine's lines start off; v3 is now the comparison (default on)
    await expect(chip(page, 'v4:sensor.living')).toHaveAttribute('aria-pressed', 'false');
    await expect(chip(page, 'v3')).toHaveAttribute('aria-pressed', 'true');
    await chip(page, 'out:service').click(); // any write stores the migrated shape
    const stored = await page.evaluate((k) => JSON.parse(localStorage.getItem(k)), KEY);
    expect(stored.on['role:primary:_ca']).toBe(false);
    expect(stored.on['role:shadow:sensor.bed']).toBe(false);
    expect(stored.on.v3).toBeUndefined();
    expect(stored.on['v4:sensor.bed']).toBeUndefined();
  });

  test('an older store without a pairing drops the engine choices (they cannot be placed)', async ({ page }) => {
    await page.addInitScript((k) => {
      localStorage.setItem(k, JSON.stringify({ on: { v3: false, 'ind:sensor.bed': true } }));
    }, KEY);
    await open(page, { primary: 'v3', shadow: 'v4' });
    await expect(chip(page, 'v3')).toHaveAttribute('aria-pressed', 'true');
    await expect(chip(page, 'ind:sensor.bed')).toHaveAttribute('aria-pressed', 'true'); // non-engine choices kept
  });

  test('storage that throws: the chart still renders with the defaults and toggles still work', async ({ page }) => {
    await page.addInitScript(() => {
      const boom = () => { throw new Error('storage blocked'); };
      Object.defineProperty(window, 'localStorage', { configurable: true, get: boom });
    });
    const errors = [];
    page.on('pageerror', e => errors.push(String(e)));
    await open(page);
    await expect(chip(page, 'ind:_ca')).toHaveAttribute('aria-pressed', 'true');
    await expect(chip(page, 'ind:sensor.living')).toHaveAttribute('aria-pressed', 'false');
    await chip(page, 'ind:sensor.living').click();
    await expect(chip(page, 'ind:sensor.living')).toHaveAttribute('aria-pressed', 'true');
    expect(errors.filter(e => /storage blocked/.test(e))).toEqual([]);
  });
});

test.describe('Status chart: the "Past v4 lines" row', () => {
  test('says what it does: v4 predicted N h earlier, only left of Now', async ({ page }) => {
    await open(page);
    const row = page.locator('#chart-lead-row');
    await expect(row.locator('#chart-lead-label')).toHaveText('Past v4 lines: predicted');
    await expect(row).toHaveAttribute('title', /predicted that many hours earlier.*only the past part of the v4 lines/);
    await expect(row.locator('[role="group"]')).toHaveAttribute('aria-labelledby', 'chart-lead-label');
    await expect(row.locator('[role="group"]')).toHaveAttribute('aria-describedby', 'chart-lead-help');
    await expect(row.locator('#chart-lead-help')).toContainText('left of Now');
    const b4 = row.locator('[data-lead="4h"]');
    await expect(b4).toHaveText('4 h earlier');
    await expect(b4).toHaveAttribute('title', /4 h earlier/);
    await expect(b4).toBeEnabled();
    // a look-back with no archived predictions in the view says so in words (0.9.6 Lane F: "in view", not "yet");
    // Issue #1459 (deliberate reversal of the 0.9.6 lock): it stays choosable, and choosing it says "none in this view"
    await expect(row.locator('[data-lead="12h"]')).toHaveText('12 h earlier (none in view)');
    await expect(row.locator('[data-lead="12h"]')).toBeEnabled();
    await expect(row.locator('#chart-lead-empty')).toHaveCount(0);
    await row.locator('[data-lead="12h"]').click();
    await expect(page.locator('#chart-lead-row [data-lead="12h"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('#chart-lead-row #chart-lead-empty')).toHaveText(/^12 h earlier: none in this view; choose /);
    await expect(page.locator('#chart-lead-row #chart-lead-help')).toContainText('left of Now');
    await page.locator('#chart-lead-row [data-lead="1h"]').click();
    await expect(row.locator('[data-lead="1h"]')).toHaveAttribute('aria-pressed', 'true');
  });

  // Issue #1459: the buttons stay choosable (never greyed out for "nothing yet"); the row says why there is nothing
  test('with no past v4 predictions the row says so, and the buttons stay choosable', async ({ page }) => {
    await open(page, { past: false });
    const row = page.locator('#chart-lead-row');
    await expect(row.locator('#chart-lead-empty')).toHaveText('v4 has no past predictions to show yet');
    await expect(row.locator('#chart-lead-help')).toContainText('left of Now');
    await expect(row.locator('[data-lead]')).toHaveCount(4);
    await expect(row.locator('[data-lead]:disabled')).toHaveCount(0);
  });

  test('v4 not ready: no v4 chips and no row; one line says v4 is still learning the sensors', async ({ page }) => {
    await open(page, { ready: false });
    await expect(page.locator('#chart-lead-row')).toHaveCount(0);
    await expect(page.locator('#chart-key-body [data-series^="v4:"]')).toHaveCount(0);
    await expect(page.locator('#chart-v4-learning')).toHaveText('v4 is still learning: Living room, Bedroom');
  });
});

test.describe('Status chart: the "How each engine has scored" link', () => {
  test('classic (index.html) passes ?from=classic so the scoring page comes back to classic', async ({ page }) => {
    await page.goto('/index.html');
    await page.waitForFunction(() => !!document.querySelector('#chart-main-model a[href^="prediction_accuracy.html"]'), { timeout: 8000 });
    await expect(page.locator('#chart-main-model a[href^="prediction_accuracy.html"]')).toHaveAttribute('href', 'prediction_accuracy.html?from=classic');
  });

  test('the new dashboard keeps the plain link', async ({ page }) => {
    await page.goto('/new_dashboard.html');
    await page.waitForFunction(() => !!document.querySelector('#chart-main-model a[href^="prediction_accuracy.html"]'), { timeout: 8000 });
    await expect(page.locator('#chart-main-model a[href^="prediction_accuracy.html"]')).toHaveAttribute('href', 'prediction_accuracy.html');
  });
});

// The key folds into one "Showing: ..." line at every width, folded on a first visit, remembered with the choices.
for (const vp of [{ name: 'desktop', width: 1280, height: 900 }, { name: 'phone', width: 375, height: 800 }]) {
  test.describe('Status chart key: fold and unfold (' + vp.name + ')', () => {
    test.use({ viewport: { width: vp.width, height: vp.height } });
    const sum = (page) => page.locator('#chart-key > summary');
    const isOpen = (page) => page.evaluate(() => document.getElementById('chart-key').open);
    const items = async (page) => {
      const t = await sum(page).locator('.key-sum-text').textContent();
      return t.replace(/^Showing: /, '').split(' · ');
    };

    test('folded by default; the summary names what is drawn', async ({ page }) => {
      await open(page, { folded: true });
      expect(await isOpen(page)).toBe(false);
      await expect(sum(page)).toBeVisible();
      await expect(sum(page)).toHaveAttribute('aria-expanded', 'false');
      await expect(sum(page).locator('.key-sum-text')).toHaveText(/^Showing: CA reading/);
      await expect(sum(page)).toContainText('Edit');
      await expect(page.locator('#chart-key-body')).toBeHidden();
      const it = await items(page);
      expect(it).toEqual(expect.arrayContaining(['CA reading', 'v3', 'v4 Living room', 'Plan']));
      expect(it).not.toContain('Living room');   // the sensor's own line is off by default
      expect(it).not.toContain('Humidity');      // an overlay that starts off
      await expect(sum(page).locator('#chart-key-count')).toHaveText('(' + it.length + ')');
      // the summary is one line
      const box = await sum(page).boundingBox();
      expect(box.height).toBeLessThan(48);
    });

    test('opens and closes from the keyboard, the chart keeps its size, and the choice is remembered', async ({ page }) => {
      await open(page, { folded: true });
      const h0 = (await page.locator('#chart-stack').boundingBox()).height;
      await sum(page).focus();
      await page.keyboard.press('Enter');
      await expect(sum(page)).toHaveAttribute('aria-expanded', 'true');
      await expect(page.locator('#chart-key-body')).toBeVisible();
      await expect(sum(page)).toContainText('Hide');
      expect((await page.locator('#chart-stack').boundingBox()).height).toBe(h0);
      await page.reload();
      await page.waitForFunction(() => !!Chart.getChart(document.getElementById('temp-chart')), { timeout: 8000 });
      await expect(sum(page)).toHaveAttribute('aria-expanded', 'true');
      expect(await isOpen(page)).toBe(true);
      await sum(page).focus();
      await page.keyboard.press('Space');
      await expect(sum(page)).toHaveAttribute('aria-expanded', 'false');
      await page.reload();
      await page.waitForFunction(() => !!Chart.getChart(document.getElementById('temp-chart')), { timeout: 8000 });
      await expect(sum(page)).toHaveAttribute('aria-expanded', 'false');
      expect(await isOpen(page)).toBe(false);
    });

    test('the summary follows the selection', async ({ page }) => {
      await open(page);
      await chip(page, 'ind:sensor.bed').click();
      await chip(page, 'v3').click();
      await sum(page).click();
      await expect(sum(page)).toHaveAttribute('aria-expanded', 'false');
      const it = await items(page);
      expect(it).toContain('Bedroom');
      expect(it).not.toContain('v3');
      const pressed = await page.locator('#chart-key-body [data-series]:not([data-series^="strip:"])[aria-pressed="true"]:not(.no-data)').count();
      expect(it.length).toBe(pressed);
    });
  });
}
