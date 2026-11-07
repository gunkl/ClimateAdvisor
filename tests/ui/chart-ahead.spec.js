const { test, expect } = require('@playwright/test');

// The default "now" view shows at least 24 h of future on every range, on both dashboards (shared chart_v2.js).
// Rule: the range is the look-back, "Ahead" (24h / 48h / 5d, default 24h) is the future; there is no shorter Ahead.
// A remembered Ahead under a day (the old '12h' button) reads back as 24h and is re-saved.

const H = 3600000;
const RANGES = ['6h', '12h', '24h', '3d', '7d', '30d', '1y'];
const PAGES = [['classic', '/'], ['new dashboard', '/new_dashboard.html']];
const KEY = 'ca_chart_v2:default';

async function open(page, url) {
  await page.goto(url);
  await page.waitForFunction(() => {
    const c = document.getElementById('temp-chart'), o = document.getElementById('out-chart');
    return typeof Chart !== 'undefined' && c && o && !!Chart.getChart(c) && !!Chart.getChart(o);
  }, { timeout: 8000 });
}

// x-axis edges of both charts, as offsets from now (ms)
function axes(page) {
  return page.evaluate(() => {
    const now = Date.now();
    const g = id => { const ch = Chart.getChart(document.getElementById(id)); return { min: ch.scales.x.min - now, max: ch.scales.x.max - now }; };
    return { temp: g('temp-chart'), out: g('out-chart'), state: window.__chartState() };
  });
}

// wait until the range is applied and the (re-fetched, redrawn) charts show it
async function waitRange(page, r) {
  await page.waitForFunction(r => {
    if (!window.__chartState || window.__chartState().range !== r) return false;
    const w = window.__chartState().win;
    return ['temp-chart', 'out-chart'].every(id => { const ch = Chart.getChart(document.getElementById(id)); return ch && Math.abs(ch.scales.x.min - w.min) < 60000; });
  }, r);
  await page.waitForTimeout(150);
}

for (const [name, url] of PAGES) {
  test.describe(`Chart ahead >= 24 h (${name})`, () => {
    test('every range: the default view reaches at least 24 h past now; the range is the look-back', async ({ page }) => {
      await open(page, url);
      await expect(page.locator('.ahead-btn[data-ahead="12h"]')).toHaveCount(0);
      await expect(page.locator('.ahead-btn[data-ahead="24h"]')).toHaveAttribute('aria-pressed', 'true');
      for (const r of RANGES) {
        await page.locator(`.range-btn[data-range="${r}"]`).click();
        await waitRange(page, r);
        const a = await axes(page);
        const back = { '6h': 6, '12h': 12, '24h': 24, '3d': 72, '7d': 168, '30d': 720, '1y': 8760 }[r] * H;
        for (const ch of [a.temp, a.out]) {
          expect(ch.max, `${r} max`).toBeGreaterThanOrEqual(24 * H - 60000);
          expect(Math.abs(ch.max - 24 * H), `${r} max`).toBeLessThan(2 * 60000);
          expect(Math.abs(ch.min + back), `${r} min`).toBeLessThan(2 * 60000);
        }
        expect(a.state.offset).toBe(0);
        expect(a.state.zoomed).toBe(false);
        // the forecast really reaches that far (the payload, not a stretched axis)
        const lastForecast = await page.evaluate(() => {
          const ds = Chart.getChart(document.getElementById('out-chart')).data.datasets.find(d => d.label === 'Service forecast');
          return ds && ds.data.length ? Math.max(...ds.data.map(p => p.x)) - Date.now() : null;
        });
        expect(lastForecast, `${r} forecast end`).toBeGreaterThanOrEqual(23 * H);
      }
    });

    test('a longer Ahead still works and is remembered; an unknown one is ignored', async ({ page }) => {
      await open(page, url);
      await page.locator('.ahead-btn[data-ahead="48h"]').click();
      await expect(page.locator('.ahead-btn[data-ahead="48h"]')).toHaveAttribute('aria-pressed', 'true');
      expect(Math.abs((await axes(page)).temp.max - 48 * H)).toBeLessThan(2 * 60000);
      expect((await page.evaluate(k => JSON.parse(localStorage.getItem(k)), KEY)).ahead).toBe('48h');
      await page.evaluate(() => window.setChartAhead('12h'));
      expect((await axes(page)).state.ahead).toBe('48h');
    });

    test('drag-zoom then Reset zoom returns to the 24 h-ahead view', async ({ page }) => {
      await open(page, url);
      await page.locator('#temp-chart').scrollIntoViewIfNeeded();
      const box = await page.locator('#temp-chart').boundingBox();
      const y = box.y + box.height / 2;
      await page.mouse.move(box.x + box.width * 0.35, y);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width * 0.5, y, { steps: 5 });
      await page.mouse.move(box.x + box.width * 0.6, y, { steps: 5 });
      await page.mouse.up();
      await page.waitForFunction(() => window.__chartState().zoomed === true);
      const z = await axes(page);
      expect(z.temp.max).toBeLessThan(24 * H - 60000);
      await page.locator('#chart-zoom-reset').click();
      await page.waitForFunction(() => window.__chartState().zoomed === false);
      const a = await axes(page);
      for (const ch of [a.temp, a.out]) expect(Math.abs(ch.max - 24 * H)).toBeLessThan(2 * 60000);
    });

    test('scroll back then Now returns to the 24 h-ahead view', async ({ page }) => {
      await open(page, url);
      await page.locator('#chart-nav-back').click();
      await page.waitForTimeout(300);
      expect((await axes(page)).temp.max).toBeLessThan(24 * H - 60000);
      await page.evaluate(() => window.chartGoNow());
      await page.waitForFunction(() => window.__chartState().offset === 0);
      await page.waitForTimeout(300);
      expect(Math.abs((await axes(page)).temp.max - 24 * H)).toBeLessThan(2 * 60000);
    });

    test('a remembered 12h Ahead migrates to 24h and is re-saved; other choices are kept', async ({ page }) => {
      await page.addInitScript(k => {
        if (!sessionStorage.getItem('seeded')) {
          localStorage.setItem(k, JSON.stringify({ on: { 'ov:band': false }, range: '6h', ahead: '12h', lead: '4h', table: false }));
          sessionStorage.setItem('seeded', '1');
        }
      }, KEY);
      await open(page, url);
      await waitRange(page, '6h');
      const a = await axes(page);
      expect(a.state.ahead).toBe('24h');
      expect(a.state.range).toBe('6h');
      for (const ch of [a.temp, a.out]) expect(Math.abs(ch.max - 24 * H)).toBeLessThan(2 * 60000);
      await expect(page.locator('.ahead-btn[data-ahead="24h"]')).toHaveAttribute('aria-pressed', 'true');
      const stored = await page.evaluate(k => JSON.parse(localStorage.getItem(k)), KEY);
      expect(stored.ahead).toBe('24h');
      expect(stored.range).toBe('6h');
      expect(stored.on['ov:band']).toBe(false);
      expect('migrated' in stored).toBe(false);
    });
  });
}
