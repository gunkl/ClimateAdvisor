const { test, expect } = require('@playwright/test');

// Issue #1425: a short Away is one flat Away block on the chart, not a band spike.
// Occupant view (production, 2026-11-07): a 30-minute Away was recorded on one chart_log row (63-79 F) between home
// rows (68-74 F). The past band was a straight line from row to row, so each such row drew a tall triangle. Now each
// past row's band holds flat until the next row, and the Away strip runs from the row you left on to the next row.

const M = 60000;

async function routeChart(page) {
  await page.route('**/api/climate_advisor/chart_data**', async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    const now = Date.now();
    const row = (ago, occupancy, lower, upper) => ({ ts: new Date(now - ago * M).toISOString(), hvac: 'idle', fan: false,
      indoor: 67.5, outdoor: 62, windows_open: true, windows_recommended: false, lower, upper, occupancy });
    // home 68-74 until 3 h ago; Away (63-79) from 150 min ago; home again from 120 min ago (one 30-minute Away row)
    body.state_log = [row(240, 'home', 68, 74), row(180, 'home', 68, 74), row(150, 'away', 63, 79),
      row(120, 'home', 68, 74), row(60, 'home', 68, 74), row(5, 'home', 68, 74)];
    body.target_band = [];
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
}

test.describe('A short Away is drawn as one flat Away block (Issue #1425)', () => {
  for (const path of ['/', '/new_dashboard.html']) {
    test(`${path}: the band holds flat over the Away row and never slopes`, async ({ page }) => {
      const errors = [];
      page.on('pageerror', (e) => errors.push(String(e)));
      await routeChart(page);
      await page.goto(path);
      await page.waitForFunction(() => {
        const c = document.getElementById('temp-chart');
        if (!c || typeof Chart === 'undefined' || !Chart.getChart(c)) return false;
        return Chart.getChart(c).data.datasets.some(d => d.label === 'Target Band' && d.data.length > 0);
      }, null, { timeout: 8000 });
      const band = await page.evaluate(() => {
        const ch = Chart.getChart(document.getElementById('temp-chart'));
        const pts = (label) => ch.data.datasets.find(d => d.label === label).data.map(p => [p.x, p.y]);
        return { up: pts('Target Band'), lo: pts('_target_band_lower') };
      });
      const now = Date.now();
      // Every point is on one of the two bands' values: no sloped segment ever reaches a value in between.
      expect(band.up.every(([, y]) => y === 74 || y === 79)).toBe(true);
      expect(band.lo.every(([, y]) => y === 68 || y === 63)).toBe(true);
      // Between consecutive points the value changes only at the same instant (a vertical step), never across time.
      for (const pts of [band.up, band.lo]) {
        for (let i = 1; i < pts.length; i++) {
          if (pts[i][1] !== pts[i - 1][1]) expect(pts[i][0]).toBe(pts[i - 1][0]);
        }
      }
      // The Away block spans its own row to the next row: 79 at 150 and at 120 minutes ago.
      const at = (pts, ago) => pts.filter(([x]) => Math.abs(x - (now - ago * 60000)) < 90000).map(([, y]) => y);
      expect(at(band.up, 150)).toContain(79);
      expect(at(band.up, 120)).toEqual(expect.arrayContaining([79, 74]));
      expect(errors).toEqual([]);
    });
  }
});
