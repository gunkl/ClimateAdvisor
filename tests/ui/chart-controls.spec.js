const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

// 0.9.6 Lane F: the Status chart's controls, against the live 0.9.5 Zone 1 shape (tests/fixtures/chart_v2_zone1_live_095.json):
// v4 model "_ca"-only; v4 future/baseline for "_ca" only; v4 past for "_ca" and the thermostat's finer sensor (from the
// backtest); nothing from v4 for the thermostat or the bedroom sensor; no humidity / feels-like logged.
//  1. v4 chips only for the sensors v4 has data for; the rest on one "v4 is still learning" line; a history-only sensor has one chip (#1430).
//  2. Humidity and Feels-like (Issue #1414): ordinary indoor/outdoor lines; the old ov: choices move to the indoor lines.
//  4. The owner's "v4 has no past predictions to show yet" while v4 had days of them: scrolled into history the fetched
//     window has none, so the row said "yet". It now says "none in this view" (and when v4 has them, from v4.past_span).

const LIVE = JSON.parse(fs.readFileSync(path.join(__dirname, '../fixtures/chart_v2_zone1_live_095.json'), 'utf8'));
const FINER = 'sensor.santa_maria_hallway_current_temperature';
const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

function shifted(deltaMs) {
  const walk = (o) => {
    if (typeof o === 'string' && ISO.test(o)) { const t = Date.parse(o); return isNaN(t) ? o : new Date(t + deltaMs).toISOString(); }
    if (Array.isArray(o)) return o.map(walk);
    if (o && typeof o === 'object') { const r = {}; Object.keys(o).forEach(k => { r[k] = walk(o[k]); }); return r; }
    return o;
  };
  return walk(LIVE.chart_data);
}

const HOURS = { '6h': 6, '12h': 12, '24h': 24, '3d': 72, '7d': 168, '30d': 720, '1y': 8760 };

// Like the backend: v4.past only carries the fetched window [end - range, end) (end = before_ts or now).
// opts.span: add v4.past_span (the 0.9.6 backend) covering every past point of the capture.
// opts.pastAgeH: the past lines end that many hours earlier than in the capture (the backtest was published at a
// deploy that long ago and the live archive has not caught up; live on 2026-11-07 the archive held 8 slots).
function liveRoute(opts) {
  const o = Object.assign({ span: true, pastAgeH: 0 }, opts || {});
  return async (route) => {
    const u = new URL(route.request().url());
    const now = Date.now();
    const body = shifted(now - Date.parse(LIVE.now));
    const end = u.searchParams.get('before_ts') ? Number(u.searchParams.get('before_ts')) : now;
    const start = end - (HOURS[u.searchParams.get('range')] || 24) * 3600000;
    const all = [];
    const sensors = [];
    const past = {};
    Object.entries(body.v4.past || {}).forEach(([sid, byLead]) => {
      const kept = {};
      Object.entries(byLead).forEach(([lead, raw]) => {
        const pts = raw.map(p => Object.assign({}, p, { ts: new Date(Date.parse(p.ts) - o.pastAgeH * 3600000).toISOString() }));
        pts.forEach(p => all.push(Date.parse(p.ts)));
        if (pts.length && !sensors.includes(sid)) sensors.push(sid);
        const w = pts.filter(p => { const t = Date.parse(p.ts); return t >= start && t < end; });
        if (w.length) kept[lead] = w;
      });
      if (Object.keys(kept).length) past[sid] = kept;
    });
    body.v4.past = past;
    if (end < now - 3600000) { body.v4.future = {}; body.v4.baseline = {}; } // a historical view has no forecast
    if (o.span) body.v4.past_span = { first: new Date(Math.min(...all)).toISOString(), last: new Date(Math.max(...all)).toISOString(), sensors };
    if (o.mutate) o.mutate(body);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  };
}

async function open(page, url, opts) {
  await page.route('**/api/climate_advisor/chart_data**', liveRoute(opts));
  await page.goto(url);
  await page.waitForFunction(() => document.getElementById('temp-chart') && typeof Chart !== 'undefined'
    && !!Chart.getChart(document.getElementById('temp-chart')), { timeout: 8000 });
  await expect(page.locator('#chart-key-body [data-series="ind:_ca"]')).toHaveCount(1);
  if (!(await page.evaluate(() => document.getElementById('chart-key').open))) await page.locator('#chart-key > summary').click();
}
const chip = (page, id) => page.locator('#chart-key-body [data-series="' + id + '"]');

for (const url of ['/', '/new_dashboard.html']) {
  test.describe(`chart controls on the live 0.9.5 shape (${url})`, () => {
    test('v4 chips only for sensors v4 has data for; the rest on one muted line; history only has one chip', async ({ page }) => {
      await open(page, url);
      const v4 = await page.locator('#chart-key-body [data-series^="v4:"], #chart-key-body [data-series^="base:"]')
        .evaluateAll(bs => bs.map(b => b.dataset.series + '=' + b.textContent.trim()).sort());
      // Issue #1430: the finer sensor (past predictions only) has one chip, on the "Past predictions only" row
      expect(v4).toEqual([
        'base:_ca=Climate Advisor reading',
        'v4:_ca=Climate Advisor reading',
        `v4:${FINER}=Santa Maria thermostat My ecobee Current Temperature`,
      ].sort());
      await expect(page.locator('#chart-v4-learning')).toHaveText('v4 is still learning: thermostat, bedroom sensor');
      await expect(page.locator('#chart-v4-learning')).toHaveAttribute('title', /Santa Maria thermostat My ecobee \(thermostat\), Primary Bedroom Sensor Temperature \(bedroom sensor\)/);
      // the finer sensor: its past (backtest) line can be drawn; it has no no-plan chip (that line is a forecast)
      await expect(chip(page, 'v4:' + FINER)).toBeEnabled();
      await expect(chip(page, 'base:' + FINER)).toHaveCount(0);
      if ((await chip(page, 'v4:' + FINER).getAttribute('aria-pressed')) !== 'true') await chip(page, 'v4:' + FINER).click();
      await expect(chip(page, 'v4:' + FINER)).toHaveAttribute('aria-pressed', 'true');
      const n = await page.evaluate((id) => {
        const c = Chart.getChart(document.getElementById('temp-chart'));
        const i = c.data.datasets.findIndex(d => d.caId === id);
        return c.isDatasetVisible(i) ? c.data.datasets[i].data.filter(p => p.y != null && p.x >= c.scales.x.min).length : 0;
      }, 'v4:' + FINER);
      expect(n).toBeGreaterThan(10);
    });

    test('Humidity and Feels-like (Issue #1414): the old overlay choices move to the indoor lines; no overlay chip', async ({ page }) => {
      await page.addInitScript(() => {
        if (sessionStorage.getItem('seeded')) return;
        sessionStorage.setItem('seeded', '1');
        localStorage.setItem('ca_chart_v2:default', JSON.stringify({ on: { 'ov:humidity': true, 'ov:feelslike': true, 'ov:band': false } }));
      });
      await open(page, url, { mutate: b => { b.state_log = b.state_log.map(e => Object.assign({}, e, { humidity: 45, feelslike: 70 })); } });
      await expect(chip(page, 'ov:humidity')).toHaveCount(0);
      await expect(chip(page, 'ov:feelslike')).toHaveCount(0);
      await expect(chip(page, 'ind:humidity')).toHaveAttribute('aria-pressed', 'true');
      await expect(chip(page, 'ind:feelslike')).toHaveAttribute('aria-pressed', 'true');
      const drawn = await page.evaluate(() => Chart.getChart(document.getElementById('temp-chart')).data.datasets
        .filter(d => d.caId === 'ind:humidity' || d.caId === 'ind:feelslike').map(d => d.caId + ':' + d.yAxisID + ':' + !d.hidden).sort());
      expect(drawn).toEqual(['ind:feelslike:y:true', 'ind:humidity:yRight:true']);
      const stored = await page.evaluate(() => JSON.parse(localStorage.getItem('ca_chart_v2:default')).on);
      expect(stored).toEqual({ 'ov:band': false, 'ind:humidity': true, 'ind:feelslike': true });
    });

    // Issue #1459 (deliberate reversal of 0.9.6 #1421): an empty view keeps the reason but no longer disables the chip
    test('Humidity and Feels-like with nothing logged (the live 0.9.5 shape): chips switchable, with the reason', async ({ page }) => {
      await open(page, url);
      await expect(chip(page, 'ind:humidity')).toBeEnabled();
      await expect(chip(page, 'ind:humidity')).toHaveClass(/no-data/);
      await expect(chip(page, 'ind:humidity')).toHaveAttribute('title', 'No humidity reading recorded in this view');
      await expect(chip(page, 'out:feelslike')).toHaveAttribute('title', /heat index \(80°F and up\) or wind chill \(50°F and below with wind\)/);
    });

    test('the owner\'s case: scrolled into history the row says "none in this view", never "none yet"', async ({ page }) => {
      await open(page, url);
      await expect(page.locator('#chart-lead-row [data-lead="4h"]')).toBeEnabled();   // the live view has past points
      for (let i = 0; i < 3; i++) await page.evaluate(() => shiftChartWindow(-1));     // 72 h back: before the capture
      const help = page.locator('#chart-lead-row #chart-lead-empty');
      await expect(help).toHaveAttribute('data-lead-empty', 'view');
      await expect(help).toHaveText(/^No past v4 predictions in this view; v4 has them from .+ to .+$/);
      await expect(help).not.toContainText('yet');
      await page.evaluate(() => chartGoNow());
      await expect(page.locator('#chart-lead-row [data-lead="4h"]')).toBeEnabled();
    });

    test('the owner\'s case, live view: a 6 h view after the backtest ended says "none in this view", not "yet"', async ({ page }) => {
      // past lines end 8 h ago (backtest published at a deploy 8 h ago, archive not caught up): the 24 h view has them,
      // the 6 h view's fetch has none. 0.9.5 said "v4 has no past predictions to show yet" here.
      await open(page, url, { pastAgeH: 8 });
      await expect(page.locator('#chart-lead-row [data-lead="4h"]')).toBeEnabled();
      await page.evaluate(() => setChartRange('6h'));
      const help = page.locator('#chart-lead-row #chart-lead-empty');
      await expect(help).toHaveAttribute('data-lead-empty', 'view');
      await expect(help).toHaveText(/^No past v4 predictions in this view; v4 has them from .+ to .+$/);
      // and the v4 chips say why they draw nothing left of Now only where that is so: the CA reading still has its forecast
      await expect(chip(page, 'v4:_ca')).toBeEnabled();
      // Issue #1459: nothing in view says why but stays switchable (and the look-ahead buttons stay choosable)
      await expect(chip(page, 'v4:' + FINER)).toBeEnabled();
      await expect(chip(page, 'v4:' + FINER)).toHaveClass(/no-data/);
      await expect(chip(page, 'v4:' + FINER)).toHaveAttribute('title', /Nothing to draw in this view/);
      await expect(page.locator('#chart-lead-row [data-lead="4h"]')).toBeEnabled();
    });

    test('an older backend (no past_span): scrolled history still says "in this view"', async ({ page }) => {
      await open(page, url, { span: false });
      for (let i = 0; i < 3; i++) await page.evaluate(() => shiftChartWindow(-1));
      await expect(page.locator('#chart-lead-row #chart-lead-empty')).toHaveText('No past v4 predictions in this view');
    });

    test('every range and the daily resolution: past lead buttons whenever the view has past points', async ({ page }) => {
      await open(page, url);
      for (const r of ['6h', '12h', '24h', '3d', '7d', '30d', '1y']) {
        await page.evaluate((rr) => setChartRange(rr), r);
        await expect(page.locator('#chart-lead-row [data-lead="4h"]')).toBeEnabled();
      }
    });
  });
}
