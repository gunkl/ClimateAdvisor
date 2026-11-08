const { test, expect } = require('@playwright/test');

// Issue #1483: v4's line runs up to a week ahead, as far as the forecast goes. Past the last real hourly forecast hour
// it runs on the daily highs and lows, so the chart draws it fainter (a solid line also dashed) and the readout tags
// those values "rough". The chart offers a 7-day look-ahead, and the past v4 lines can be the 2, 3 or 5-day ones.

const H = 3600000;
const engines = {
  primary: 'v4', shadow: 'none', fallback: false, fitting: false, primary_state: 'ready', text: '',
  engines: [
    { id: 'v3', label: 'v3', running: true, primary: false, shadow: false, usable: true, confidence: 'high' },
    { id: 'v4', label: 'v4', running: true, primary: true, shadow: false, usable: true, confidence: 'ok' },
  ],
};
const LEADS = ['1h', '4h', '12h', '24h', '48h', '72h', '120h'];

function weekV4(now) {
  const iso = (ms) => new Date(ms).toISOString();
  const half = 1800000;
  const past = Array.from({ length: 48 }, (_, i) => ({ ts: iso(now - (48 - i) * half), temp: 68 + (i % 5) * 0.2 }));
  const future = Array.from({ length: 6 * 48 }, (_, i) => ({ ts: iso(now + (i + 1) * half), temp: 69 + (i % 7) * 0.2 }));
  return {
    ready: true, reason: 'ok', scores: null, model: { confidence: 'ok', fitted_at: iso(now - 9 * H) },
    leads: LEADS, sensors: ['_ca'],
    past: { _ca: Object.fromEntries(LEADS.map((l) => [l, past])) },
    future: { _ca: future }, future_at: iso(now), baseline: { _ca: future },
    hourly_until: iso(now + 40 * H),   // the real hourly forecast ends 40 h ahead; the daily curve runs on to 6 days
    breaks: { _ca: [] },
    corrected_outdoor: { past: [], future: future.map((p) => ({ ts: p.ts, temp: 50 })) },
  };
}

async function openWith(page, now, url = '/') {
  await page.route('**/api/climate_advisor/chart_data**', async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    body.prediction_engines = engines;
    body.v4 = weekV4(now);
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
  await page.goto(url);
  await page.waitForFunction(() => document.getElementById('temp-chart') && typeof Chart !== 'undefined'
    && !!Chart.getChart(document.getElementById('temp-chart')), { timeout: 8000 });
}

// The style Chart.js gives the segment that starts at a point `ms` from now on dataset `id`
function segStyleAt(page, id, offsetMs) {
  return page.evaluate(([id, off]) => {
    const c = Chart.getChart(document.getElementById(id.startsWith('out:') ? 'out-chart' : 'temp-chart'));
    const i = c.data.datasets.findIndex((d) => d.caId === id);
    const ds = c.data.datasets[i];
    const t = Date.now() + off;
    const k = ds.data.findIndex((p) => p.x >= t && p.y != null);
    const seg = ds.segment;
    if (!seg || k < 0) return { has: !!seg, color: ds.borderColor, dash: ds.borderDash };
    const ctx = { p0: { parsed: { x: ds.data[k].x } }, p1: { parsed: { x: ds.data[k + 1] ? ds.data[k + 1].x : ds.data[k].x } } };
    return { has: true, color: seg.borderColor(ctx) || ds.borderColor, dash: seg.borderDash(ctx) || ds.borderDash };
  }, [id, offsetMs]);
}

test.describe('v4 up to a week ahead (Issue #1483)', () => {
  test('the line runs to the end of its forecast and the 7-day view shows all of it', async ({ page }) => {
    const now = Date.now();
    await openWith(page, now);
    await expect(page.locator('.ahead-btn[data-ahead="7d"]')).toHaveCount(1);
    await page.locator('.ahead-btn[data-ahead="7d"]').click();
    await expect(page.locator('.ahead-btn[data-ahead="7d"]')).toHaveAttribute('aria-pressed', 'true');
    const r = await page.evaluate(() => {
      const c = Chart.getChart(document.getElementById('temp-chart'));
      const d = c.data.datasets.find((x) => x.caId === 'v4:_ca');
      return { last: Math.max(...d.data.filter((p) => p.y != null).map((p) => p.x)) - Date.now(), max: c.scales.x.max - Date.now() };
    });
    expect(r.last).toBeGreaterThan(5.9 * 24 * H);
    expect(r.max).toBeGreaterThan(6.9 * 24 * H);
  });

  test('past the real hourly forecast the line is fainter and dashed; before it, as always', async ({ page }) => {
    await openWith(page, Date.now());
    const near = await segStyleAt(page, 'v4:_ca', 10 * H);
    const far = await segStyleAt(page, 'v4:_ca', 60 * H);
    expect(near.has).toBe(true);
    expect(near.dash).toEqual([]);
    expect(String(near.color)).not.toContain('0.45');
    expect(far.dash).toEqual([6, 4]);
    expect(String(far.color)).toContain('0.45');
    const outFar = await segStyleAt(page, 'out:corrected', 60 * H);
    expect(String(outFar.color)).toContain('0.45');
  });

  test('the readout tags a value past the hourly forecast as rough', async ({ page }) => {
    await openWith(page, Date.now());
    await page.locator('.ahead-btn[data-ahead="5d"]').click();
    await page.waitForFunction(() => Chart.getChart(document.getElementById('temp-chart')).scales.x.max - Date.now() > 4.5 * 24 * 3600000);
    await page.waitForTimeout(150);
    const box = await page.locator('#temp-chart').boundingBox();
    const xAt = (off) => page.evaluate((off) => {
      const c = Chart.getChart(document.getElementById('temp-chart'));
      return c.scales.x.getPixelForValue(Date.now() + off);   // canvas pixels, from the canvas's own left edge
    }, off);
    await page.mouse.move(box.x + await xAt(10 * H), box.y + box.height / 2);
    await expect(page.locator('#chart-hover-panel')).toContainText('v4 Climate Advisor reading');
    await expect(page.locator('#chart-hover-panel .plan-qual', { hasText: 'rough' })).toHaveCount(0);
    await page.mouse.move(box.x + await xAt(70 * H), box.y + box.height / 2);
    await expect(page.locator('#chart-hover-panel .plan-qual', { hasText: 'rough' }).first()).toBeVisible();
  });

  test('the past-line buttons offer 2, 3 and 5 days', async ({ page }) => {
    await openWith(page, Date.now());
    for (const [lead, text] of [['48h', '2 days'], ['72h', '3 days'], ['120h', '5 days']]) {
      await expect(page.locator(`#chart-key-body [data-lead="${lead}"]`)).toContainText(text);
    }
    await expect(page.locator('#chart-key-body [data-lead="4h"]')).toContainText('4 h');
  });
});
