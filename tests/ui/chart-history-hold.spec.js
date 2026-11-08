const { test, expect } = require('@playwright/test');

// Issue #1406: the chart's past band draws a time-of-use bank as what the thermostat held.
// Occupant view: scrolling back over an Away cool bank (cool to 77°F inside the 60–80°F Away band) drew 77–80°F, the
// band schedule's shape, while the live chart (since #1404) drew the hold, 60–77°F. chart_log rows now carry
// hold_lower/hold_upper when the drawn band differs; the past band uses them, and rows without them (from before 0.9.6)
// keep lower/upper.

const H = 3600000;

async function routeChart(page) {
  await page.route('**/api/climate_advisor/chart_data**', async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    const now = Date.now();
    // 12 h ago .. 4 h ago: pre-0.9.6 bank rows (no hold, the old shape); 4 h ago .. now: bank rows with the hold.
    body.state_log = Array.from({ length: 25 }, (_, i) => {
      const ts = now - (12 - i * 0.5) * H;
      const row = { ts: new Date(ts).toISOString(), hvac: 'cool', fan: false, indoor: 77.5, outdoor: 90,
        windows_open: false, windows_recommended: false, lower: 77, upper: 80, occupancy: 'away' };
      if (ts > now - 4 * H) { row.hold_lower = 60; row.hold_upper = 77; }
      return row;
    });
    body.target_band = [];
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
}

async function bandAt(page, agoHours) {
  return page.evaluate((ago) => {
    const ch = Chart.getChart(document.getElementById('temp-chart'));
    const up = ch.data.datasets.find(d => d.label === 'Target Band');
    const lo = ch.data.datasets.find(d => d.label === '_target_band_lower');
    const t = Date.now() - ago * 3600000;
    const near = (ds) => ds.data.reduce((b, p) => (Math.abs(p.x - t) < Math.abs(b.x - t) ? p : b)).y;
    return [near(lo), near(up)];
  }, agoHours);
}

test.describe('Chart history draws a past time-of-use bank as the hold (Issue #1406)', () => {
  for (const path of ['/', '/new_dashboard.html']) {
    test(`${path}: a past bank row with a hold draws the hold; an older row keeps lower/upper`, async ({ page }) => {
      const errors = [];
      page.on('pageerror', (e) => errors.push(String(e)));
      await routeChart(page);
      await page.goto(path);
      await page.waitForFunction(() => {
        const c = document.getElementById('temp-chart');
        if (!c || typeof Chart === 'undefined' || !Chart.getChart(c)) return false;
        return Chart.getChart(c).data.datasets.some(d => d.label === 'Target Band' && d.data.length > 0);
      }, null, { timeout: 8000 });
      expect(await bandAt(page, 2)).toEqual([60, 77]); // the hold: cool to 77 inside the Away band
      expect(await bandAt(page, 8)).toEqual([77, 80]); // a pre-0.9.6 row: the old schedule shape
      expect(errors).toEqual([]);
    });
  }
});
