const { test, expect } = require('@playwright/test');

// Issue #1230 R6fe: the classic Status tab's "Next 24 hours" list and the chart's plan overlay.
// The mock server's /timeline answers with steps relative to now (see makeTimelineFixture in mock-server.js):
//   -120 min  CA   done step (never listed)         +45 min  You  open windows
//   +150 min  CA   pre-cool                         +240 min You  close windows, LOW confidence ("may help")
//   +300 min  CA   briefing send (never listed)     +600 min CA   bedtime band
//   +1500 min CA   wake band (beyond 24 h, not listed)

async function waitForChart(page) {
  await page.waitForFunction(() => {
    const c = document.getElementById('temp-chart');
    return c && typeof Chart !== 'undefined' && !!Chart.getChart(c);
  }, { timeout: 8000 });
}
async function waitForPlan(page) {
  await page.waitForFunction(() => document.querySelectorAll('#plan-list .plan-row, #plan-list .plan-empty').length > 0
    && !document.querySelector('#plan-list').textContent.includes('Loading the plan'), { timeout: 8000 });
}
const planDatasets = (page) => page.evaluate(() => Chart.getChart(document.getElementById('temp-chart')).data.datasets
  .filter(d => d.caId === 'ov:plan').map(d => ({ n: d.data.length, hidden: d.hidden, first: d.data[0].x })));

test.describe('Next 24 hours and the plan overlay (classic Status)', () => {
  test('lists the steps ahead, in order, with who does each one', async ({ page }) => {
    await page.goto('/');
    await waitForChart(page);
    await waitForPlan(page);
    const rows = page.locator('#plan-list .plan-row');
    await expect(rows).toHaveCount(4);
    const texts = await rows.evaluateAll(els => els.map(e => ({ actor: e.dataset.actor, kind: e.dataset.kind, text: e.querySelector('.plan-text').innerText })));
    expect(texts.map(t => t.kind)).toEqual(['windows_open', 'precool_start', 'windows_close', 'sleep_band']);
    expect(texts.map(t => t.actor)).toEqual(['you', 'ca', 'you', 'ca']);
    expect(texts.some(t => /Past step|daily briefing/.test(t.text))).toBe(false);
    // who does it is a word as well as a shape
    await expect(rows.nth(0).locator('.plan-actor')).toContainText('You');
    await expect(rows.nth(1).locator('.plan-actor')).toContainText('CA');
  });

  test('a "you" step shows a reminder 30 minutes ahead; a low-confidence one says "may help"', async ({ page }) => {
    await page.goto('/');
    await waitForPlan(page);
    const open = page.locator('#plan-list .plan-row[data-kind="windows_open"]');
    await expect(open.locator('.plan-note')).toContainText('Reminder at');
    await expect(open.locator('.plan-qual')).toHaveCount(0);
    const close = page.locator('#plan-list .plan-row[data-kind="windows_close"]');
    await expect(close.locator('.plan-qual')).toHaveText('may help');
    await expect(close).toHaveClass(/plan-low/);
    // a CA step has no reminder
    await expect(page.locator('#plan-list .plan-row[data-kind="precool_start"] .plan-note')).toHaveCount(0);
  });

  test('the overlay: a chip in the Overlays group, markers and a future-only path, on by default', async ({ page }) => {
    await page.goto('/');
    await waitForChart(page);
    await waitForPlan(page);
    const chip = page.locator('#chart-key-body .key-group[aria-label="Overlays"] [data-series="ov:plan"]');
    await expect(chip).toHaveCount(1);
    await expect(chip).toHaveAttribute('aria-pressed', 'true');
    const ds = await planDatasets(page);
    expect(ds.length).toBe(1);
    expect(ds[0].hidden).toBe(false);
    expect(ds[0].n).toBeGreaterThan(10);
    // the path starts at (about) now, never months back
    expect(ds[0].first).toBeGreaterThan(Date.now() - 2 * 3600000);
    // markers are drawn on the indoor canvas to the right of NOW: sample a pixel at the first "you" step's x
    const hit = await page.evaluate(() => {
      const ch = Chart.getChart(document.getElementById('temp-chart'));
      const x = ch.scales.x.getPixelForValue(Date.now() + 45 * 60000);
      const y = ch.scales.y.top + 8;
      const d = ch.ctx.getImageData(Math.round(x * devicePixelRatio), Math.round(y * devicePixelRatio), 1, 1).data;
      return [d[0], d[1], d[2], d[3]];
    });
    expect(hit[3]).toBeGreaterThan(0);
  });

  test('the toggle hides the plan overlay and is remembered across a reload (per zone)', async ({ page }) => {
    await page.goto('/');
    await waitForChart(page);
    await waitForPlan(page);
    await page.locator('#chart-key-body [data-series="ov:plan"]').click();
    await expect(page.locator('#chart-key-body [data-series="ov:plan"]')).toHaveAttribute('aria-pressed', 'false');
    expect((await planDatasets(page))[0].hidden).toBe(true);
    const stored = await page.evaluate(() => JSON.parse(localStorage.getItem('ca_chart_v2:default')));
    expect(stored.on['ov:plan']).toBe(false);
    await page.reload();
    await waitForChart(page);
    await waitForPlan(page);
    await expect(page.locator('#chart-key-body [data-series="ov:plan"]')).toHaveAttribute('aria-pressed', 'false');
    expect((await planDatasets(page))[0].hidden).toBe(true);
    // the list itself is not an overlay: it stays
    await expect(page.locator('#plan-list .plan-row')).toHaveCount(4);
    // and back on
    await page.locator('#chart-key-body [data-series="ov:plan"]').click();
    expect((await planDatasets(page))[0].hidden).toBe(false);
  });

  test('hovering near a step names it in the readout', async ({ page }) => {
    await page.goto('/');
    await waitForChart(page);
    await waitForPlan(page);
    const box = await page.locator('#temp-chart').boundingBox();
    const x = await page.evaluate(() => {
      const ch = Chart.getChart(document.getElementById('temp-chart'));
      return ch.scales.x.getPixelForValue(Date.now() + 150 * 60000);
    });
    await page.evaluate((px) => window.__triggerHoverAt(px), x);
    await page.waitForTimeout(150);
    const html = await page.locator('#chart-hover-panel').innerHTML();
    expect(html).toContain('CA ');
    expect(html).toContain('Start cooling a little early');
    expect(box.width).toBeGreaterThan(100);
  });

  test('no plan yet (503) is a plain empty state and the chart is unaffected', async ({ page }) => {
    await page.route('**/api/climate_advisor/timeline**', (r) => r.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"timeline_not_ready"}' }));
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    await page.goto('/');
    await waitForChart(page);
    await waitForPlan(page);
    await expect(page.locator('#plan-list .plan-empty')).toContainText('after Climate Advisor has classified the day');
    expect(await planDatasets(page)).toEqual([]);
    await expect(page.locator('#chart-key-body [data-series="ov:plan"]')).toHaveClass(/no-data/);
    expect(errors).toEqual([]);
  });

  test('a plan with no steps in the next 24 hours says so', async ({ page }) => {
    await page.route('**/api/climate_advisor/timeline**', (r) => r.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ schema_version: 1, unit: 'fahrenheit', steps: [], path: [] }) }));
    await page.goto('/');
    await waitForPlan(page);
    await expect(page.locator('#plan-list .plan-empty')).toHaveText('Nothing is planned for the next 24 hours.');
  });

  test('a v4 plan path is not drawn for a zone where v4 has no data', async ({ page }) => {
    await page.route('**/api/climate_advisor/timeline**', async (r) => {
      const resp = await r.fetch();
      const body = await resp.json();
      body.path = body.path.map(p => Object.assign({}, p, { model: 'v4' }));
      await r.fulfill({ response: resp, body: JSON.stringify(body) });
    });
    await page.goto('/');
    await waitForChart(page);
    await waitForPlan(page);
    // the mock chart payload has no v4 block, so v4 is not ready: the steps still show, the path does not
    expect(await planDatasets(page)).toEqual([]);
    await expect(page.locator('#plan-list .plan-row')).toHaveCount(4);
  });

  test('a Celsius plan keeps the unit of the payload', async ({ page }) => {
    await page.route('**/api/climate_advisor/status', (r) => r.fulfill({ status: 200, contentType: 'application/json',
      body: JSON.stringify({ day_type: 'mild', hvac_mode: 'off', unit: 'celsius', indoor_temp: 21.5, outdoor_temp: 18, automation_enabled: true, occupancy_mode: 'home' }) }));
    await page.route('**/api/climate_advisor/timeline**', async (r) => {
      const resp = await r.fetch();
      const body = await resp.json();
      body.unit = 'celsius';
      body.path = body.path.map(p => Object.assign({}, p, { temp_f: 21 }));
      await r.fulfill({ response: resp, body: JSON.stringify(body) });
    });
    await page.goto('/');
    await waitForChart(page);
    await waitForPlan(page);
    const ys = await page.evaluate(() => Chart.getChart(document.getElementById('temp-chart')).data.datasets.filter(d => d.caId === 'ov:plan')[0].data.map(p => p.y));
    expect(new Set(ys)).toEqual(new Set([21]));
  });
});
