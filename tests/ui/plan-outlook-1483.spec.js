const { test, expect } = require('@playwright/test');

// Issue #1483 part 2: the plan looks ahead as far as the forecast goes (at most a week). The "Next 24 hours" list is
// unchanged for its first 24 h; each later day follows as one folded row ("Wed, Nov 11 · 3 steps · 1 for you") that
// opens to its steps and stays open across the list's refreshes.

const H = 3600000;
const PAGES = [['classic', '/', '#plan-list'], ['new dashboard', '/new_dashboard.html', '#status-plan-list']];

function step(ms, kind, actor, text) {
  return { ts: new Date(ms).toISOString(), actor, kind, text, text_short: text, reason: 'x', confidence: 'ok', lead_minutes: actor === 'you' ? 30 : 0, meta: { status: 'planned', id: kind + ms } };
}

function weekTimeline(now) {
  const steps = [step(now + 2 * H, 'windows_open', 'you', 'Open the windows.'), step(now + 10 * H, 'sleep_band', 'ca', 'Bedtime band.')];
  for (let d = 2; d <= 5; d++) {
    const day = new Date(now); day.setDate(day.getDate() + d); day.setHours(6, 30, 0, 0);
    steps.push(step(day.getTime(), 'wake_band', 'ca', 'Wake band, day ' + d + '.'));
    if (d === 4) {
      steps.push(step(day.getTime() + 8 * H, 'windows_open', 'you', 'Open the windows, day 4.'));
      steps.push(step(day.getTime() + 10 * H, 'windows_close', 'you', 'Close the windows, day 4.'));
    }
    steps.push(step(day.getTime() + 15 * H, 'sleep_band', 'ca', 'Bedtime band, day ' + d + '.'));
  }
  return { schema_version: 1, steps, path: [], horizon_end: new Date(now + 6 * 24 * H).toISOString() };
}

for (const [name, url, list] of PAGES) {
  test.describe(`Plan outlook (${name})`, () => {
    test.beforeEach(async ({ page }) => {
      const now = Date.now();
      await page.route('**/api/climate_advisor/timeline**', (route) => route.fulfill({
        status: 200, contentType: 'application/json', body: JSON.stringify(weekTimeline(now)) }));
      await page.goto(url);
      await page.waitForFunction((sel) => document.querySelectorAll(sel + ' .plan-row').length > 0, list, { timeout: 8000 });
    });

    test('the first 24 h are listed as before; each later day is one folded row', async ({ page }) => {
      await expect(page.locator(list + ' > .plan-row')).toHaveCount(2);
      const days = page.locator(list + ' > .plan-later');
      await expect(days).toHaveCount(4);
      await expect(page.locator(list + ' > .plan-later-head')).toContainText('outlook');
      // folded: the day's steps are there but hidden until opened
      const day4 = days.nth(2);
      await expect(day4.locator('summary')).toContainText('4 steps');
      await expect(day4.locator('summary')).toContainText('2 for you');
      await expect(day4.locator('.plan-row').first()).toBeHidden();
      await day4.locator('summary').click();
      await expect(day4.locator('.plan-row')).toHaveCount(4);
      await expect(day4.locator('.plan-row').nth(1)).toBeVisible();
      await expect(day4.locator('.plan-row[data-kind="windows_open"]')).toContainText('Open the windows, day 4.');
    });

    test('an opened day stays open when the list refreshes', async ({ page }) => {
      const day = page.locator(list + ' > .plan-later').nth(1);
      await day.locator('summary').click();
      await expect(day.locator('details')).toHaveAttribute('open', '');
      await page.evaluate(() => window.CAChartV2.loadTimeline());
      await expect(page.locator(list + ' > .plan-later').nth(1).locator('details')).toHaveAttribute('open', '');
      await expect(page.locator(list + ' > .plan-later').nth(0).locator('details')).not.toHaveAttribute('open', '');
    });
  });
}
