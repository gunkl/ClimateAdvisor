const { test, expect } = require('@playwright/test');

// Issue #1590: the page-wide `overflow-wrap: anywhere` (#1341 D8) let an auto-sized table column shrink to one character,
// so the Activity record broke its Time column ("16:3" / "7") and headers ("Tim" / "e", "Indo" / "or") while Event and
// Settings took the width. Table cells now use `break-word`: short values stay on one line, long text still wraps.

const LONG = 'indoor 68°F / outdoor 53°F, fan: whole_house_fan, windows open, grace period running for 156 more minutes while the '
  + 'occupant override stays confirmed and the setpoint is held at the manual value until the period ends';

async function openActivity(page, width) {
  await page.setViewportSize({ width, height: 900 });
  await page.route('**/api/climate_advisor/activity_record*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
    table: '| Time | Event | Settings | Source | Indoor | Outdoor |\n|---|---|---|---|---|---|\n| 16:37 | x | y | manual | 68°F | 53°F |',
    rows: [
      { time: '16:37', event: 'Override cleared (was heat) after the occupant changed the setpoint on the thermostat itself', settings: LONG, source: 'manual', indoor: '68°F', outdoor: '53°F', event_msg: null, settings_msg: null },
      { time: '14:52', event: 'Startup coalescing complete -- HVAC commanded', settings: LONG, source: 'sensor', indoor: '68°F', outdoor: '53°F', event_msg: null, settings_msg: null },
    ],
    limited_msg: null, no_events_msg: null, whf_warning_msg: null, hours: 12, is_truncated: false,
  }) }));
  await page.goto('/new_dashboard.html?lang=en#analysis');
  await page.selectOption('#report-type', 'activity_record');
  await page.click('#run-report-btn');
  await expect(page.locator('#report-content table td', { hasText: '16:37' })).toBeVisible();
}

// The number of distinct line tops the text of each cell in the given columns occupies (1 = one line).
function lineCounts(page, cols) {
  return page.evaluate((cols) => {
    const table = document.querySelector('#report-content table');
    const out = [];
    table.querySelectorAll('tr').forEach((tr) => {
      cols.forEach((c) => {
        const cell = tr.children[c]; if (!cell || !cell.textContent.trim()) return;
        const r = document.createRange(); r.selectNodeContents(cell);
        const tops = new Set(Array.from(r.getClientRects()).filter((x) => x.width > 0).map((x) => Math.round(x.top)));
        out.push({ text: cell.textContent.trim(), lines: tops.size });
      });
    });
    return out;
  }, cols);
}

for (const width of [1280, 760]) {
  test(`Activity record at ${width}px: Time, Source, Indoor and Outdoor cells and headers stay on one line`, async ({ page }) => {
    await openActivity(page, width);
    const cells = await lineCounts(page, [0, 3, 4, 5]);
    expect(cells.length).toBeGreaterThanOrEqual(12);
    expect(cells.filter((c) => c.lines !== 1)).toEqual([]);
    // The long Settings text still wraps instead of being forced onto one line.
    const settings = await lineCounts(page, [2]);
    expect(Math.max(...settings.map((c) => c.lines))).toBeGreaterThan(1);
  });
}

test('Activity record on a phone (390px): the table scrolls sideways inside its box, the page itself does not widen', async ({ page }) => {
  await openActivity(page, 390);
  const cells = await lineCounts(page, [0, 4, 5]);
  expect(cells.filter((c) => c.lines !== 1)).toEqual([]);
  const pageOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(pageOverflow).toBeLessThanOrEqual(0);
});
