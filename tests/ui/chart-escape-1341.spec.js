const { test, expect } = require('@playwright/test');

// Issue #1341 S1: chart_v2.js builds attribute values (title, aria-label, data-*) from API text. The escaper must
// escape quotes, or a sensor name / role containing one breaks out of the attribute and injects an attribute.
const NAME = 'Hall" data-pwn-name="1';
const ROLE = "Role' data-pwn-role='1\" onmouseover=\"window.__pwned=1";
const SENSORS = [
  { id: 'sensor.hall', name: NAME, role: ROLE, role_code: 'other', primary: true },
  { id: 'sensor.bed', name: 'Bed', role: 'bedroom', role_code: 'bedroom_sensor', primary: false },
];

async function open(page, url) {
  await page.route('**/api/climate_advisor/chart_data**', async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    body.indoor_sensors = SENSORS;
    body.actual_indoor_sensors = {};
    SENSORS.forEach((s, k) => { body.actual_indoor_sensors[s.id] = body.actual_indoor.map((p) => ({ time: p.time, temp: p.temp + k * 0.5 })); });
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
  await page.goto(url);
  await page.waitForFunction(() => document.querySelector('#chart-key-body [data-series]') || document.querySelector('#chart-key [data-series]'), null, { timeout: 10000 });
  // the key can be collapsed: open it so the chips exist in the DOM either way (they are rendered regardless)
}

for (const [name, url] of [['classic (no i18n.js)', '/'], ['new dashboard, en', '/new_dashboard.html?lang=en'], ['new dashboard, de', '/new_dashboard.html?lang=de'], ['new dashboard, ar', '/new_dashboard.html?lang=ar'], ['new dashboard, ko', '/new_dashboard.html?lang=ko']]) {
  test(`a quote in a sensor name or role cannot break out of an attribute: ${name}`, async ({ page }) => {
    await open(page, url);
    const r = await page.evaluate(() => ({
      injected: document.querySelectorAll('[data-pwn-name],[data-pwn-role]').length,
      handler: document.querySelectorAll('[onmouseover]').length,
      chipTexts: [...document.querySelectorAll('[data-series]')].map((c) => (c.getAttribute('aria-label') || '') + '|' + (c.getAttribute('title') || '')).join('\n'),
    }));
    expect(r.injected).toBe(0);
    expect(r.handler).toBe(0);
    expect(r.chipTexts).toContain('Hall" data-pwn-name="1');   // the text arrives intact, as text
  });
}
