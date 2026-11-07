const { test, expect } = require('@playwright/test');
const FIX = require('../fixtures/new_dashboard_api.json');

// Issue #1399: the new dashboard's Debug tab. No catch-all "Other" group, and no raw JSON in any value: the prediction
// engine block, the decisions feed (control_feed) and the operation timeline ("Plan") read in words, in the user's
// unit. The payloads below follow the live shapes (coordinator.v4_model_summary(), prediction_engines.engine_state(),
// get_debug_state()'s control_feed and timeline_debug()), with the live model {"primary":"_ca","sensors":["_ca"]} and an
// empty archive. The raw objects stay in Download diagnostics.

const at = (h, m, dayOffset) => { const d = new Date(); d.setDate(d.getDate() + (dayOffset || 0)); d.setHours(h, m, 0, 0); return d.toISOString(); };
const ymd = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');

function v4Summary(celsius, pe) {
  return {
    prediction_engines: pe || engineState(),
    has_model: true, confidence: 'low', fitted_at: at(6, 42), span_days: 30.0, n_anchors: 160,
    mae_by_lead: celsius ? { '12h': 0.44, '1h': 0.36, '4h': 0.45 } : { '12h': 0.8, '1h': 0.65, '4h': 0.81 },
    primary: '_ca', sensors: ['_ca'], schema_version: 1, unit: celsius ? '°C' : '°F', engine_available: true,
    mass: celsius ? 19.7 : 67.4, fit_running: false,
    last_fit: { at: at(6, 42), reason: 'first', seconds: 1.0, ok: true },
    last_cycle: { at: at(7, 26), ms: 7.2, predicted: true, archived: 0, inputs: 96 },
    archive_slots: {}, archive_since: null, sensor_names: {},
  };
}
const engineState = (over) => Object.assign({
  primary: 'v4', shadow: 'v3', primary_state: 'ready', fallback: false, fitting: false, text: '',
  engines: [
    { id: 'v3', label: 'v3', running: true, primary: false, shadow: true, usable: true, confidence: 'high' },
    { id: 'v4', label: 'v4', running: true, primary: true, shadow: false, usable: true, confidence: 'low' },
  ],
  control: { engine: 'v4', source: 'v4', ready: true },
}, over || {});
function timeline() {
  const steps = [];
  for (let i = 0; i < 10; i++) steps.push({ ts: at(8 + i, 0), actor: 'ca', kind: 'setpoint', text: 'Step ' + i, reason: 'r', target_f: 70, mode: 'heat', confidence: 'medium', lead_minutes: 0, source: 'plan', meta: { a: [1, 2] } });
  return {
    shadow: false, unit: 'fahrenheit',
    last_run: { at: at(7, 30), trigger: 'cycle', ms: 12.5, regenerated: false, reasons: [], steps: 10, generation: 1 },
    schema_version: 1, zone: 'entry', generated_at: at(6, 50), day: ymd(new Date()), horizon_end: at(7, 30, 2),
    steps, path: [{ ts: at(8, 0), temp_f: 70.1, sensor: '_ca', model: 'v4' }], latched: true, regen_reason: null,
    detail: { path_source: 'v4', grade: 'medium', generation: 1, regenerated_for: [], band: [], runs: [], candidates: [], summary: [], hvac_minutes: 40, dropped: [], days: [] },
  };
}

async function openDebug(page, opts) {
  opts = opts || {};
  const celsius = !!opts.celsius;
  const v4 = v4Summary(celsius, opts.engines);
  const auto = Object.assign({}, FIX.automation_state, {
    unit: celsius ? 'celsius' : 'fahrenheit', v4_model: v4,
    control_feed: opts.controlFeed || { mode: 'v4', path_points: 47, own_path: true },
    operation_timeline: timeline(),
    comfort_family_last_switch: { ts: at(5, 10), from: 'heating', to: 'cooling', reason: 'warm afternoon' },
  });
  const engines = Object.assign({}, FIX.engines, { unit: auto.unit, v4 });
  const events = { events: [{ time: at(7, 0), type: 'hvac_action', details: { mode: 'heat', list: [1, 2] } }], diagnostics: { last_command: { service: 'set_temperature', data: { temperature: 70 } } } };
  const json = (body) => (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/api/climate_advisor/automation_state**', json(auto));
  await page.route('**/api/climate_advisor/engines**', json(engines));
  await page.route('**/api/climate_advisor/event_log**', json(events));
  if (celsius) {
    await page.route('**/api/climate_advisor/status**', async (route) => {
      const resp = await route.fetch(); const body = await resp.json(); body.unit = 'celsius';
      await route.fulfill({ response: resp, body: JSON.stringify(body) });
    });
  }
  await page.goto('/new_dashboard.html#debug');
  await page.waitForSelector('#dbg-groups section', { timeout: 8000 });
}
const value = (page, key) => page.locator('#dbg-groups tr').filter({ has: page.locator('td.k', { hasText: new RegExp('^' + key + '$') }) }).locator('td.mono');

test.describe('New dashboard: Debug tab', () => {
  test('no "Other" group, and no visible value or event detail contains JSON', async ({ page }) => {
    await openDebug(page);
    const titles = await page.locator('#dbg-groups section h2').allInnerTexts();
    expect(titles.some((t) => /^Other\b/.test(t))).toBe(false);
    expect(titles.some((t) => /^Plan\b/.test(t))).toBe(true);
    const cells = await page.locator('#dbg-groups td.mono, #dbg-ev-rows td').allInnerTexts();
    expect(cells.length).toBeGreaterThan(50);
    for (const c of cells) expect(c, c).not.toMatch(/[{[]/);
    await expect(page.locator('#panel-debug')).not.toContainText('appear under Other');
    await expect(page.locator('#dbg-dl-diag')).toHaveText('Download diagnostics');
  });

  test('prediction engines, decisions feed and plan read in words (°F)', async ({ page }) => {
    await openDebug(page);
    await expect(value(page, 'Primary engine')).toHaveText('v4: usable, low confidence');
    await expect(value(page, 'Shadow engine')).toHaveText('v3: high confidence');
    await expect(value(page, 'Decisions use')).toHaveText('v4’s own path (47 points)');
    await expect(value(page, 'Last learned')).toHaveText(/^\d{1,2}:42\s?AM from 30 days of history, 160 points \(first fit, 1 s\)$/);
    await expect(value(page, 'Typical miss')).toHaveText('1 h 0.65°F · 4 h 0.81°F · 12 h 0.80°F');
    await expect(value(page, 'Sensors modelled')).toHaveText('Climate Advisor reading only');
    await expect(value(page, 'Last prediction')).toHaveText(/^\d{1,2}:26\s?AM, 96 forecast inputs, 7 ms, 0 slots archived$/);
    await expect(value(page, 'Archive')).toHaveText('empty');
    await expect(value(page, 'Mass node')).toHaveText('67.4°F');
    await expect(value(page, 'Fitting now')).toHaveText('no');
    await expect(value(page, 'Engine available')).toHaveText('yes');
    await expect(value(page, 'Generated')).toHaveText(/^\d{1,2}:50\s?AM for \w{3} \d{1,2}\/\d{1,2}$/);
    await expect(value(page, 'Covers through')).toHaveText(/^\w{3} \d{1,2}:30\s?AM$/);
    await expect(value(page, 'Steps')).toHaveText('10');
    await expect(value(page, 'Last checked')).toHaveText(/^\d{1,2}:30\s?AM, not regenerated$/);
    await expect(value(page, 'comfort_family_last_switch')).toHaveText(/^heating → cooling at \d{1,2}:10\s?AM \(warm afternoon\)$/);
  });

  test('°C: the errors and the mass node are in the user unit', async ({ page }) => {
    await openDebug(page, { celsius: true });
    await expect(value(page, 'Typical miss')).toHaveText('1 h 0.36°C · 4 h 0.45°C · 12 h 0.44°C');
    await expect(value(page, 'Mass node')).toHaveText('19.7°C');
    // (the fixture's last_action_reason is a captured °F sentence, so the check is scoped to the engine block)
    const engines = page.locator('#dbg-groups section').filter({ has: page.locator('h2', { hasText: /^Prediction engines/ }) });
    const cells = await engines.locator('td.mono').allInnerTexts();
    expect(cells.length).toBeGreaterThan(8);
    for (const c of cells) expect(c, c).not.toMatch(/°F/);
  });

  test('while the primary has no usable fit the decisions say so in words', async ({ page }) => {
    const pe = engineState({ fallback: true, primary_state: 'learning' });
    pe.engines[1].usable = false;
    await openDebug(page, { controlFeed: { mode: 'defaults', path_points: 49, own_path: true }, engines: pe });
    await expect(value(page, 'Decisions use')).toHaveText('default behaviour (v4 still learning)');
    await expect(value(page, 'Primary engine')).toHaveText('v4: still learning');
  });
});
