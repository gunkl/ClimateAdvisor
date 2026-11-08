const { test, expect } = require('@playwright/test');

// Issue #1436: the activity strips under the chart. Solid = what happened (the chart log), faded = what the plan
// expects (the operation timeline's `strips`). The owner's case: the plan said "close the windows by 7:14 PM" while the
// faded Win plan / Nat-vent / Fan strips ran all night, drawn from chart_data.predicted_activity. The mock's
// predicted_activity reproduces that shape (windows recommended and fan active for every hour ahead); the mock's
// timeline holds one window period (+45 min open to +240 min close) and one heat run (+660 to +780 min).

const PAGES = [['classic', '/'], ['new dashboard', '/new_dashboard.html']];

async function ready(page, path) {
  await page.goto(path);
  await page.waitForFunction(() => {
    const c = document.getElementById('temp-chart');
    return c && typeof Chart !== 'undefined' && !!Chart.getChart(c) && window.CAChartV2 && window.CAChartV2.timeline()
      && window.__test_strips && (window.__test_strips.segs.windowsplan || []).some(g => g.future);
  }, { timeout: 8000 });
}

const futureSegs = (page, row) => page.evaluate((r) => (window.__test_strips.segs[r] || [])
  .filter(g => g.future).map(g => ({ a: g.a - Date.now(), b: g.b - Date.now(), text: g.step && g.step.text })), row);

for (const [name, path] of PAGES) {
  test.describe(`Activity strips follow the plan (${name})`, () => {
    test('right of NOW every strip is a plan span; predicted_activity draws nothing', async ({ page }) => {
      await ready(page, path);
      const win = await futureSegs(page, 'windowsplan');
      expect(win.length).toBe(1);
      // the period: the open step (+45 min) to the close step (+240 min), never the predictor's all-night hours
      expect(Math.abs(win[0].a - 45 * 60000)).toBeLessThan(5 * 60000);
      expect(Math.abs(win[0].b - 240 * 60000)).toBeLessThan(5 * 60000);
      expect(win[0].text).toMatch(/open the windows/);
      // the plan holds no nat-vent session, no fan and no away / override: the faded rows are empty
      for (const row of ['natvent', 'fan', 'windows', 'away', 'override', 'cooling']) {
        expect(await futureSegs(page, row), row).toEqual([]);
      }
      const heat = await futureSegs(page, 'heating');
      expect(heat.length).toBe(1);
      expect(Math.abs(heat[0].a - 660 * 60000)).toBeLessThan(5 * 60000);
      expect(heat[0].text).toMatch(/^Heating expected for about 20 min/);
    });

    test('the past is solid and ends at NOW; a row holds until the next row', async ({ page }) => {
      await ready(page, path);
      const past = await page.evaluate(() => {
        const now = Date.now();
        return Object.entries(window.__test_strips.segs).flatMap(([row, segs]) =>
          segs.filter(g => !g.future).map(g => ({ row, a: g.a, b: g.b, endsAfterNow: g.b > now + 1000 })));
      });
      expect(past.length).toBeGreaterThan(0);
      expect(past.filter(p => p.endsAfterNow)).toEqual([]);
      // the mock's windows_open rows are 16..27 of 48 half-hourly rows: the bar starts AT row 16 (no half-poll lead)
      const win = await page.evaluate(() => {
        const log = Chart && window.CAChartV2.data().state_log;
        const first = log.find(e => e.windows_open);
        const seg = window.__test_strips.segs.windows.find(g => !g.future);
        return { row: Date.parse(first.ts), a: seg.a };
      });
      expect(Math.abs(win.a - win.row)).toBeLessThan(1000);
    });

    test('the legend says what solid and faded mean', async ({ page }) => {
      await ready(page, path);
      const legend = page.locator('#chart-strip-legend');
      await expect(legend).toBeVisible();
      await expect(legend).toHaveText('Solid: what happened · Faded: what the plan expects');
      // it sits with the strips, under the activity canvas
      const below = await page.evaluate(() => {
        const c = document.getElementById('activity-chart').getBoundingClientRect();
        const l = document.getElementById('chart-strip-legend').getBoundingClientRect();
        return l.top >= c.bottom - 1;
      });
      expect(below).toBe(true);
    });

    test('hovering a faded strip names the plan step', async ({ page }) => {
      await ready(page, path);
      await page.evaluate(() => {
        const ch = Chart.getChart(document.getElementById('temp-chart'));
        const x = ch.scales.x.getPixelForValue(Date.now() + 120 * 60000);
        window.__triggerHoverAt(x);
      });
      const panel = page.locator('#chart-hover-panel');
      await expect(panel).toContainText('Activity');
      await expect(panel).toContainText(/Win plan\s*plan · You \d{1,2}:\d{2}\s*[AP]M: Open the windows/);
      // the step's time is shown once, beside it, never repeated inside the sentence (Issue #1399)
      expect(await panel.innerHTML()).not.toMatch(/<strong>\s*\d{1,2}:\d{2}\s*[AP]M/);
      // nothing from the predictor: no Nat-vent named at that time
      await expect(panel).not.toContainText('Nat-vent');
    });
  });
}
