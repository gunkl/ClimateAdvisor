const { test, expect } = require('@playwright/test');

// Issue #1341 S2: every time the dashboard shows follows Home Assistant's own setting (CAI18N.timeZone(): the server
// zone by default), never the browser's. Here the browser is in Los Angeles and Home Assistant in Tokyo; the Status
// text, the chart readout, the chart's axis ticks and the night card's axis must all agree on Tokyo time.

const HA_ZONE = 'Asia/Tokyo';
test.use({ timezoneId: 'America/Los_Angeles', viewport: { width: 1280, height: 1000 } });

const STATUS = {
  language: 'en', unit: 'fahrenheit', version: '0.9.9', coordinator_healthy: true, automation_enabled: true,
  day_type: 'mild', hvac_mode: 'heat', hvac_action: 'idle', current_setpoint: 68, ca_target_heat: 70, occupancy_mode: 'home',
  indoor_temp: 71, outdoor_temp: 65, compliance_score: 0.84, hvac_runtime_today: 95, trend_direction: 'warming', trend_magnitude: 3,
  automation_status: 'active', status_code: 'active', automation_status_msg: { key: 'status.auto.active', params: {} },
  fan_remote_timer_hours: 2, fan_remote_timer_ends: '2026-11-14T22:30:00-05:00', fan_remote_speed: 3,
  zone_count: 1, zones: [{ entry_id: 'mock-zone', title: 'Home' }],
};
// 2026-11-14T22:30-05:00 = 03:30Z = 12:30 PM in Tokyo, 7:30 PM in Los Angeles
const ENDS_TOKYO = /12:30\s?PM/;

const fmt = (ms, opts) => new Intl.DateTimeFormat('en', Object.assign({ timeZone: HA_ZONE }, opts)).format(new Date(ms));

async function open(page, { server = true } = {}) {
  await page.addInitScript((cfg) => {
    const fake = { hass: { language: 'en', locale: { language: 'en', time_zone: cfg.server ? 'server' : 'local' }, config: { time_zone: cfg.zone } } };
    const orig = Document.prototype.querySelector;
    Document.prototype.querySelector = function (sel) { return sel === 'home-assistant' ? fake : orig.call(this, sel); };
  }, { zone: HA_ZONE, server });
  let night = null;
  await page.route('**/api/climate_advisor/status*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(STATUS) }));
  await page.route('**/api/climate_advisor/night_strategy*', async (route) => {
    const resp = await route.fetch();
    night = await resp.json();
    await route.fulfill({ response: resp, body: JSON.stringify(night) });
  });
  await page.goto('/new_dashboard.html?lang=en');
  await page.waitForFunction(() => {
    const p = document.getElementById('chart-hover-panel');
    return document.querySelector('#status-grid .tile') && document.querySelector('#night-card .rec') && p && /Now/.test((p.querySelector('.chp-time') || {}).textContent || '');
  }, null, { timeout: 10000 });
  return () => night;
}

test('status text, chart readout, chart ticks and the night axis all show Home Assistant time, not the browser time', async ({ page }) => {
  const getNight = await open(page);
  const t0 = Date.now();
  // Status text: the fan timer end, a fixed instant
  const grid = await page.locator('#status-grid').innerText();
  expect(grid).toMatch(ENDS_TOKYO);
  expect(grid).not.toMatch(/7:30\s?PM/);
  // chart readout "Now · <time>"
  const nowText = await page.evaluate(() => document.querySelector('#chart-hover-panel .chp-time').textContent);
  const t1 = Date.now();
  const allowed = new Set([t0, t1, (t0 + t1) / 2].map((ms) => fmt(ms, { hour: 'numeric', minute: '2-digit' }).replace(/ /g, ' ')));
  expect([...allowed].some((a) => nowText.replace(/ /g, ' ').includes(a)), `${nowText} vs ${[...allowed]}`).toBe(true);
  // the chart's own axis ticks (Chart.js callback): the first visible tick is a Tokyo hour
  const ticks = await page.evaluate(() => {
    const chart = Chart.getChart(document.getElementById('temp-chart'));
    const sc = chart.scales.x;
    return sc.ticks.map((tk) => ({ value: tk.value, label: sc.options.ticks.callback.call(sc, tk.value, 0, sc.ticks) }));
  });
  expect(ticks.length).toBeGreaterThan(2);
  for (const tk of ticks) {
    const span = await page.evaluate(() => { const s = Chart.getChart(document.getElementById('temp-chart')).scales.x; return s.max - s.min; });
    if (span <= 36 * 3600000) expect(tk.label.replace(/ /g, ' ')).toBe(fmt(tk.value, { hour: 'numeric', minute: '2-digit' }).replace(/ /g, ' '));
  }
  // the night card's axis: hour labels every 8th point of the plan's path
  const night = getNight();
  const first = night.options[0].path;
  const expected = [0, 8, 16, 24, 32].filter((i) => i < first.length).map((i) => fmt(Date.parse(first[i][0]), { hour: 'numeric' }));
  const labels = await page.evaluate(() => [...document.querySelectorAll('#night-card bdi')].map((b) => b.textContent.trim()));
  for (const e of expected) expect(labels, `axis ${e} in ${labels}`).toContain(e);
});

test('with Home Assistant set to the browser zone ("local") the browser time shows', async ({ page }) => {
  await open(page, { server: false });
  const grid = await page.locator('#status-grid').innerText();
  expect(grid).toMatch(/7:30\s?PM/);
  expect(grid).not.toMatch(ENDS_TOKYO);
});
