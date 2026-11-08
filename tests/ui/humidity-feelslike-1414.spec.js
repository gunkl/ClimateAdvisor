const { test, expect } = require('@playwright/test');

// Issue #1414: indoor and outdoor humidity and feels-like on the Status charts.
// Occupant view: the chart shows how humid it is inside and out and what it feels like (a heat index from 80°F, an
// outdoor wind chill from 50°F with wind), as ordinary lines in the key, the hover readout and the table. Humidity is
// read on a right % axis (on both charts, so the two plot areas stay lined up); a stretch with no reading is a gap.
// The data follows the backend contract: chart_log rows carry humidity / feelslike / outdoor_humidity /
// outdoor_feelslike only when the value is real, and outdoor_humidity_forecast / outdoor_feelslike_forecast are the
// future ({ts, value}).

const H = 3600000;

// 12 h of rows every 30 min. Indoor humidity has a gap 6 h..4 h ago; indoor feels-like (a heat index) only in the last
// 3 h; outdoor humidity on every row; outdoor feels-like (a wind chill) only 12 h..8 h ago. opts.celsius: °C payload.
function routeChart(page, opts) {
  const o = opts || {};
  return page.route('**/api/climate_advisor/chart_data**', async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    const now = Date.now();
    const t = v => o.celsius ? Math.round((v - 32) * 5 / 9 * 10) / 10 : v;
    body.state_log = Array.from({ length: 25 }, (_, i) => {
      const ago = 12 - i * 0.5;
      const row = { ts: new Date(now - ago * H).toISOString(), hvac: 'cool', fan: false, indoor: t(81), outdoor: t(70),
        windows_open: false, windows_recommended: false, lower: t(68), upper: t(76), occupancy: 'home', outdoor_humidity: 60 };
      if (ago > 6 || ago < 4) row.humidity = 45.5;
      if (ago <= 3) row.feelslike = t(83);
      if (ago >= 8) row.outdoor_feelslike = t(40);
      return row;
    });
    body.outdoor_humidity_forecast = Array.from({ length: 12 }, (_, i) => ({ ts: new Date(now + (i + 1) * H).toISOString(), value: 70 }));
    if (!o.celsius) body.outdoor_feelslike_forecast = Array.from({ length: 6 }, (_, i) => ({ ts: new Date(now + (i + 1) * H).toISOString(), value: 84 }));
    if (o.celsius) body.unit = 'celsius';
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
}

async function open(page, path) {
  await page.goto(path);
  await page.waitForFunction(() => {
    const c = document.getElementById('temp-chart'), o = document.getElementById('out-chart');
    return c && o && typeof Chart !== 'undefined' && Chart.getChart(c) && Chart.getChart(o)
      && Chart.getChart(c).data.datasets.some(d => d.caId === 'ind:humidity');
  }, null, { timeout: 8000 });
  if (!(await page.evaluate(() => document.getElementById('chart-key').open))) await page.locator('#chart-key > summary').click();
}
const chip = (page, id) => page.locator('#chart-key-body [data-series="' + id + '"]');

const dsInfo = (page) => page.evaluate(() => {
  const out = {};
  [['in', 'temp-chart'], ['out', 'out-chart']].forEach(([k, id]) => {
    Chart.getChart(document.getElementById(id)).data.datasets.forEach(d => {
      if (/humidity|feelslike/.test(d.caId || '')) out[d.caId] = { chart: k, axis: d.yAxisID, hidden: !!d.hidden, data: d.data };
    });
  });
  return out;
});

const axes = (page) => page.evaluate(() => {
  const a = id => {
    const c = Chart.getChart(document.getElementById(id));
    return { left: c.chartArea.left, right: c.chartArea.right, rightLabels: (c.scales.yRight ? c.scales.yRight.ticks : []).map(t => t.label) };
  };
  return { in: a('temp-chart'), out: a('out-chart') };
});

for (const path of ['/', '/new_dashboard.html']) {
  test.describe(`humidity and feels-like on the Status charts (${path})`, () => {
    test('four series on the right charts and axes, off by default, with honest gaps', async ({ page }) => {
      const errors = [];
      page.on('pageerror', e => errors.push(String(e)));
      await routeChart(page);
      await open(page, path);
      const ds = await dsInfo(page);
      expect(Object.keys(ds).sort()).toEqual(['ind:feelslike', 'ind:humidity', 'out:feelslike', 'out:humidity']);
      expect([ds['ind:humidity'].chart, ds['ind:humidity'].axis]).toEqual(['in', 'yRight']);
      expect([ds['ind:feelslike'].chart, ds['ind:feelslike'].axis]).toEqual(['in', 'y']);
      expect([ds['out:humidity'].chart, ds['out:humidity'].axis]).toEqual(['out', 'yRight']);
      expect([ds['out:feelslike'].chart, ds['out:feelslike'].axis]).toEqual(['out', 'y']);
      Object.values(ds).forEach(d => expect(d.hidden).toBe(true));   // all four start off
      for (const id of ['ind:humidity', 'ind:feelslike', 'out:humidity', 'out:feelslike']) {
        await expect(chip(page, id)).toBeEnabled();
        await expect(chip(page, id)).toHaveAttribute('aria-pressed', 'false');
      }
      // indoor humidity: a gap (null point) inside 6 h..4 h ago, and no point there
      const now = Date.now();
      const hum = ds['ind:humidity'].data;
      expect(hum.some(p => p.y === null)).toBe(true);
      expect(hum.filter(p => p.y != null && p.x > now - 5.9 * H && p.x < now - 4.1 * H)).toEqual([]);
      // feels-like only where it was real: indoor in the last 3 h; outdoor 12..8 h ago, then the forecast
      expect(ds['ind:feelslike'].data.filter(p => p.y != null).every(p => p.x >= now - 3.1 * H)).toBe(true);
      const of = ds['out:feelslike'].data;
      expect(of.some(p => p.y === null)).toBe(true);     // the 8 h without a wind chill is a gap
      expect(of.filter(p => p.y != null && p.x > now - 7.9 * H && p.x < now)).toEqual([]);
      expect(of.filter(p => p.y === 84 && p.x > now).length).toBe(6);
      expect(ds['out:humidity'].data.filter(p => p.y === 70 && p.x > now).length).toBe(12);   // the forecast
      expect(errors).toEqual([]);
    });

    test('the outdoor right % axis shows only with outdoor humidity on; the plot areas stay aligned', async ({ page }) => {
      await routeChart(page);
      await open(page, path);
      let a = await axes(page);
      expect(a.out.rightLabels.length).toBeGreaterThan(0);
      expect(a.out.rightLabels.every(l => l === '')).toBe(true);
      expect(a.in.rightLabels.every(l => l === '')).toBe(true);
      expect([a.out.left, a.out.right]).toEqual([a.in.left, a.in.right]);
      await chip(page, 'out:humidity').click();
      await expect(chip(page, 'out:humidity')).toHaveAttribute('aria-pressed', 'true');
      a = await axes(page);
      expect(a.out.rightLabels.some(l => /%$/.test(l))).toBe(true);
      expect(a.in.rightLabels.every(l => l === '')).toBe(true);   // the indoor axis follows only the indoor line
      expect([a.out.left, a.out.right]).toEqual([a.in.left, a.in.right]);
      await chip(page, 'ind:humidity').click();
      a = await axes(page);
      expect(a.in.rightLabels.some(l => /%$/.test(l))).toBe(true);
      expect([a.out.left, a.out.right]).toEqual([a.in.left, a.in.right]);
      await chip(page, 'out:humidity').click();
      a = await axes(page);
      expect(a.out.rightLabels.every(l => l === '')).toBe(true);
      expect([a.out.left, a.out.right]).toEqual([a.in.left, a.in.right]);
    });

    test('readout: % for humidity, the temperature degree for feels-like; table columns with units', async ({ page }) => {
      await routeChart(page);
      await open(page, path);
      for (const id of ['ind:humidity', 'ind:feelslike', 'out:humidity']) await chip(page, id).click();
      const panel = page.locator('#chart-hover-panel');
      await expect(panel).toContainText('Indoor humidity: 45.5%');
      await expect(panel).toContainText('Indoor feels-like: 83.0°');
      await expect(panel).toContainText('Outdoor humidity: 60.0%');
      await expect(panel).not.toContainText('83.0%');
      await page.locator('#chart-table-toggle').click();
      const heads = await page.locator('#chart-table-wrap thead th').allTextContents();
      expect(heads).toContain('Indoor humidity (%)');
      expect(heads).toContain('Indoor feels-like (°F)');
      expect(heads).toContain('Outdoor humidity (%)');
      await expect(page.locator('#chart-table-wrap caption')).toContainText('humidity in %');
    });

    test('an old saved ov:humidity: true turns the indoor humidity line on (re-saved once)', async ({ page }) => {
      await page.addInitScript(() => {
        if (sessionStorage.getItem('seeded')) return;
        sessionStorage.setItem('seeded', '1');
        localStorage.setItem('ca_chart_v2:default', JSON.stringify({ on: { 'ov:humidity': true } }));
      });
      await routeChart(page);
      await open(page, path);
      await expect(chip(page, 'ind:humidity')).toHaveAttribute('aria-pressed', 'true');
      await expect(chip(page, 'ind:feelslike')).toHaveAttribute('aria-pressed', 'false');
      const ds = await dsInfo(page);
      expect(ds['ind:humidity'].hidden).toBe(false);
      expect((await axes(page)).in.rightLabels.some(l => /%$/.test(l))).toBe(true);
      const stored = await page.evaluate(() => JSON.parse(localStorage.getItem('ca_chart_v2:default')).on);
      expect(stored['ind:humidity']).toBe(true);
      expect('ov:humidity' in stored).toBe(false);
    });

    test('a °C payload reads in °C, and the feels-like reason is in °C', async ({ page }) => {
      await routeChart(page, { celsius: true });
      await open(page, path);
      await expect(page.locator('#chart-title')).toHaveText('Temperature (°C)');
      await chip(page, 'ind:feelslike').click();
      await expect(page.locator('#chart-hover-panel')).toContainText('Indoor feels-like: 28.3°');
      await page.locator('#chart-table-toggle').click();
      const heads = await page.locator('#chart-table-wrap thead th').allTextContents();
      expect(heads).toContain('Indoor feels-like (°C)');
      await page.locator('#chart-table-toggle').click();
      // the outdoor chip's description names the thresholds in °C
      await expect(chip(page, 'out:feelslike')).toHaveAttribute('title', /27°C and up.*10°C and below/);
    });
  });
}

test('°C payload with no feels-like at all: the disabled reason is in °C', async ({ page }) => {
  await page.route('**/api/climate_advisor/chart_data**', async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    body.unit = 'celsius';
    body.state_log = (body.state_log || []).map(e => { const r = Object.assign({}, e); delete r.feelslike; delete r.outdoor_feelslike; return r; });
    delete body.outdoor_feelslike_forecast;
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
  await open(page, '/');
  await expect(chip(page, 'out:feelslike')).toBeDisabled();
  await expect(chip(page, 'out:feelslike')).toHaveAttribute('title',
    'Feels-like only shows as a heat index (27°C and up) or wind chill (10°C and below with wind)');
  await expect(chip(page, 'ind:feelslike')).toHaveAttribute('title', 'Indoor feels-like only shows as a heat index (27°C and up)');
});
