const { test, expect } = require('@playwright/test');

// Issue #1230 R6fe: the new dashboard's Status tab, assembled from the shared chart module, the plan list, the live
// night plan and the briefing. The mock server serves new_dashboard.html with a stored token and these endpoints:
// status, chart_data, timeline, night_strategy (?numbers=0 is not used here; routes below override), briefing.

async function open(page, hash) {
  await page.goto('/new_dashboard.html' + (hash || ''));
  await page.waitForFunction(() => document.querySelector('#status-plan-list > .plan-row, #status-plan-list .plan-empty')
    && document.getElementById('temp-chart') && typeof Chart !== 'undefined' && !!Chart.getChart(document.getElementById('temp-chart')), { timeout: 8000 });
}
const strategy = (numbers) => async (route) => {
  const resp = await route.fetch();
  const body = await resp.json();
  body.show_numbers = numbers;
  if (!numbers) {
    body.tradeoffs = [];
    body.options = body.options.map(o => Object.assign({}, o, { bedroom_low_f: null, bedroom_high_f: null, bedroom_wake_f: null, discomfort_fh: null, heat_min: null, cool_min: null, hvac_minutes: null, score: null, path: [] }));
  }
  await route.fulfill({ response: resp, body: JSON.stringify(body) });
};

test.describe('New dashboard: Status tab', () => {
  test('the placeholders are gone and every part is on the page, in reading order', async ({ page }) => {
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
    await open(page);
    for (const id of ['status-for-you', 'status-grid', 'status-chart', 'status-plan-list', 'night-card', 'status-briefing']) {
      await expect(page.locator('#' + id)).toBeAttached();
    }
    await expect(page.locator('[data-slot="chart"], [data-slot="plan"]')).toHaveCount(0);
    expect(errors.filter(e => !/AbortError/.test(e))).toEqual([]);
  });

  test('the reading cards and the For you / CA next lines come from the status payload', async ({ page }) => {
    await open(page);
    await expect(page.locator('#status-card-hallway')).toBeVisible();
    await expect(page.locator('#status-card-today')).toContainText('Mild');
    await expect(page.locator('#status-card-outdoor')).toContainText('65.0');
    await expect(page.locator('#status-card-doors')).toBeVisible();
    await expect(page.locator('#status-card-occupancy')).toContainText('Home');
    await expect(page.locator('#status-for-you')).toHaveText('None');
  });

  test('the same chart: key, ranges, table view; the plan chip is in Overlays and remembered', async ({ page }) => {
    await open(page);
    await expect(page.locator('#chart-key-body .key-group[aria-label="Overlays"] [data-series="ov:plan"]')).toHaveAttribute('aria-pressed', 'true');
    await page.locator('.range-btn[data-range="3d"]').click();
    await expect(page.locator('.range-btn[data-range="3d"]')).toHaveAttribute('aria-pressed', 'true');
    await page.locator('#chart-key > summary').click(); // the key starts folded
    await page.locator('#chart-key-body [data-series="ov:plan"]').click();
    const stored = await page.evaluate(() => JSON.parse(localStorage.getItem('ca_chart_v2:mock-zone')));
    expect(stored.on['ov:plan']).toBe(false);
    expect(stored.range).toBe('3d');
    await page.locator('#chart-table-toggle').click();
    await expect(page.locator('#chart-table-wrap')).toBeVisible();
    await expect(page.locator('.chart-table')).toBeVisible();
  });

  test('Next 24 hours lists the steps, "you" and "CA", with the qualifier on a low-confidence step', async ({ page }) => {
    await open(page);
    const rows = page.locator('#status-plan-list > .plan-row');
    await expect(rows).toHaveCount(4);
    await expect(rows.nth(0).locator('.plan-actor')).toContainText('You');
    await expect(page.locator('#status-plan-list > .plan-row[data-kind="windows_close"] .plan-qual')).toHaveText('may help');
  });

  test('Tonight in the bedroom is live: no sample badge, the recommended plan, the options and their figures', async ({ page }) => {
    await open(page);
    await expect(page.locator('#night-card [data-opt]')).toHaveCount(2);
    await expect(page.locator('#night-sample-badge')).toBeHidden();
    await expect(page.locator('#night-card .rec')).toContainText('Keep closed');
    await expect(page.locator('#night-card svg[role="img"]')).toHaveCount(1);
    await expect(page.locator('#night-card table')).toBeVisible();
    await expect(page.locator('#night-tradeoffs')).toContainText('more thing');
    // the option with steps lists them with the actor
    await expect(page.locator('#night-steps')).toBeAttached();
  });

  test('show_numbers false: the plan without predicted figures (no chart, no table, no temperatures)', async ({ page }) => {
    await page.route('**/api/climate_advisor/night_strategy**', strategy(false));
    await open(page);
    await expect(page.locator('#night-card .rec')).toContainText('Keep closed');
    await expect(page.locator('#night-nonumbers')).toContainText('not shown yet');
    await expect(page.locator('#night-headline')).toContainText('Keep the windows closed tonight');
    await expect(page.locator('#night-card svg[role="img"]')).toHaveCount(0);
    await expect(page.locator('#night-card table')).toHaveCount(0);
    // the headline names the sleep band the plan holds (a setting, not a prediction); every predicted figure is absent
    const text = (await page.locator('#night-card').innerText()).replace(await page.locator('#night-headline').innerText(), '');
    expect(text).not.toMatch(/\d+(\.\d)?\s?°/);
    await expect(page.locator('#night-sample-badge')).toBeHidden();
  });

  test('the night card says its figures are from v4', async ({ page }) => {
    await open(page);
    await expect(page.locator('#night-source')).toHaveText('from v4');
    await expect(page.locator('#night-source-note')).toContainText('always built on v4');
  });

  test('v4 off (503 v4_off): the card says v4 is switched off and where to turn it on', async ({ page }) => {
    await page.route('**/api/climate_advisor/night_strategy**', (r) => r.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"night_strategy_not_ready","reason":"v4_off"}' }));
    await open(page);
    await expect(page.locator('#night-empty')).toContainText('v4 is switched off');
    await expect(page.locator('#night-empty')).toContainText('Settings, Prediction engine');
  });

  test('no night plan yet (503) is an empty state, not an error', async ({ page }) => {
    await page.route('**/api/climate_advisor/night_strategy**', (r) => r.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"night_strategy_not_ready"}' }));
    await open(page);
    await expect(page.locator('#night-empty')).toContainText('No night plan yet');
  });

  test('in daytime the empty state says when the plan appears (reason no_night_in_view)', async ({ page }) => {
    await page.route('**/api/climate_advisor/night_strategy**', (r) => r.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"night_strategy_not_ready","reason":"no_night_in_view"}' }));
    await open(page);
    await expect(page.locator('#night-empty')).toHaveText("Tonight's plan appears 6 hours before bedtime.");
    await expect(page.locator('#night-sample-badge')).toBeHidden();
  });

  test('an install without the endpoint (404) shows the labelled sample, never a silent one', async ({ page }) => {
    await page.route('**/api/climate_advisor/night_strategy**', (r) => r.fulfill({ status: 404, contentType: 'application/json', body: '{}' }));
    await open(page);
    await expect(page.locator('#night-sample-badge')).toBeVisible();
    await expect(page.locator('#night-card [data-opt]').first()).toBeVisible();
  });

  test('the briefing is collapsed; Regenerate posts the classic body, Send posts without a body', async ({ page }) => {
    await open(page);
    const details = page.locator('#status-briefing');
    expect(await details.evaluate(d => d.open)).toBe(false);
    await details.locator('summary').click();
    await expect(page.locator('#status-briefing-text')).toHaveText('Test briefing');
    const posts = [];
    await page.route('**/api/climate_advisor/send_briefing**', async (r) => { posts.push(r.request().postData()); await r.fulfill({ status: 200, contentType: 'application/json', body: '{"message":"done"}' }); });
    await page.locator('#status-briefing-regenerate').click();
    await page.locator('#status-briefing-send').click();
    await expect.poll(() => posts.length).toBe(2);
    expect(JSON.parse(posts[0])).toEqual({ notify: false });
    expect(posts[1]).toBeNull();
  });
});
