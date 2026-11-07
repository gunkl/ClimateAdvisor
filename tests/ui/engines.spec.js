const { test, expect } = require('@playwright/test');

// Issue #1230: the Status chart's key names the PRIMARY engine (the main line) and the optional SHADOW engine, and while
// the primary has no usable fit the page says so and shows the schedule estimate instead of another engine.

const engines = (over) => Object.assign({
  primary: 'v4', shadow: 'none', fallback: false, fitting: false, primary_state: 'ready', text: '',
  engines: [
    { id: 'v3', label: 'v3', running: true, primary: false, shadow: false, usable: true, confidence: 'high' },
    { id: 'v4', label: 'v4', running: true, primary: true, shadow: false, usable: true, confidence: 'medium' },
  ],
}, over);

const withEngines = (pe, extra) => async (route) => {
  const resp = await route.fetch();
  const body = await resp.json();
  body.prediction_engines = pe;
  Object.assign(body, extra || {});
  await route.fulfill({ response: resp, body: JSON.stringify(body) });
};

async function open(page) {
  await page.goto('/');
  await page.waitForFunction(() => document.getElementById('temp-chart') && typeof Chart !== 'undefined'
    && !!Chart.getChart(document.getElementById('temp-chart')), { timeout: 8000 });
}

test.describe('Prediction engines on the Status chart', () => {
  test('the key names the primary and the comparison engine and links to Settings', async ({ page }) => {
    await page.route('**/api/climate_advisor/chart_data**', withEngines(engines({ shadow: 'v3' })));
    await open(page);
    const row = page.locator('#chart-main-model');
    await expect(row).toBeAttached();
    await expect(row).toContainText('Main prediction: v4');
    await expect(row).toContainText('Comparison: v3');
    await expect(row.locator('a[href="new_dashboard.html#settings"]')).toHaveText(/Prediction engine/);
    await expect(page.locator('#chart-v4-note')).toBeHidden();
  });

  test('v3 as the primary with no comparison engine', async ({ page }) => {
    await page.route('**/api/climate_advisor/chart_data**', withEngines(engines({ primary: 'v3' })));
    await open(page);
    await expect(page.locator('#chart-main-model')).toContainText('Main prediction: v3');
    await expect(page.locator('#chart-main-model')).toContainText('No comparison engine');
  });

  test('a primary that has not finished learning says so and shows the schedule estimate', async ({ page }) => {
    const text = 'Prediction: learning — showing the schedule estimate';
    const when = (m) => new Date(Date.now() + m * 60000).toISOString();
    const sched = [1, 2, 3, 4].map((i) => ({ ts: when(i * 60), temp: 69 + i * 0.2 }));
    await page.route('**/api/climate_advisor/chart_data**', withEngines(engines({ fallback: true, primary_state: 'learning', text }), { schedule_estimate: sched }));
    await open(page);
    await expect(page.locator('#chart-v4-note')).toBeVisible();
    await expect(page.locator('#chart-v4-note')).toHaveText(text);
    await expect(page.locator('#chart-main-model')).toContainText(text);
    await expect(page.locator('#chart-key-body')).toContainText('Schedule estimate (learning)');
  });

  test('while v4 is fitting the page says it is fitting from your history', async ({ page }) => {
    await page.route('**/api/climate_advisor/chart_data**', withEngines(engines({ fallback: true, fitting: true, primary_state: 'learning', text: 'Learning: v4 is fitting from your history' })));
    await open(page);
    await expect(page.locator('#chart-v4-note')).toHaveText('Learning: v4 is fitting from your history');
    await expect(page.locator('#chart-v4-note')).toHaveAttribute('data-engine-state', 'fitting');
  });
});
