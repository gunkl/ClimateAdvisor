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
    // the live grade words (v4_shadow.describe_grade, built by the coordinator)
    grade: { why: 'too few usable cooling starts (ok needs 5 in each mode it has seen)', by_mode: 'heating 45 usable of 61 starts (enough) · cooling 0 usable of 2 starts (needs 5) in the last 30 days' },
    // Issue #1432: which model the decisions read, already in words and the user's unit (coordinator.v4_model_summary)
    forecast_model: celsius
      ? { model: 'per_room', reason: 'per_room_no_worse', text: 'v4 forecasts each room separately (thermostat by day, bedroom at night): both rooms have 3+ days of readings (Hallway 6.0 days, Bedroom 6.0 days) and the per-room forecast scored 0.25°C vs 0.33°C at 4 h on this home\'s history' }
      : { model: 'per_room', reason: 'per_room_no_worse', text: 'v4 forecasts each room separately (thermostat by day, bedroom at night): both rooms have 3+ days of readings (Hallway 6.0 days, Bedroom 6.0 days) and the per-room forecast scored 0.45°F vs 0.60°F at 4 h on this home\'s history' },
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
  const v4 = Object.assign(v4Summary(celsius, opts.engines), opts.v4 || {});
  const auto = Object.assign({}, FIX.automation_state, {
    unit: celsius ? 'celsius' : 'fahrenheit', v4_model: v4,
    control_feed: opts.controlFeed || { mode: 'v4', path_points: 47, own_path: true },
    operation_timeline: timeline(),
    comfort_family_last_switch: { ts: at(5, 10), from: 'heating', to: 'cooling', reason: 'warm afternoon' },
  });
  // coordinator.decision_rates_summary(): v4 primary with a usable fit -> its rate scalars, v3's vent/solar observations
  const decisions = opts.decisions || [
    { key: 'confidence', kind: 'grade', value: 'low', source: 'v4' },
    { key: 'k_passive', kind: 'per_hour', value: -0.034, source: 'v4' },
    { key: 'k_active_heat', kind: 'rate', value: celsius ? 2.511 : 4.52, source: 'v4' },
    { key: 'k_active_cool', kind: 'rate', value: celsius ? -2.778 : -5.0, source: 'v4' },
    { key: 'k_solar', kind: 'rate', value: celsius ? 0.398 : 0.716, source: 'v3' },
    { key: 'solar_phase_offset_h', kind: 'hours', value: 3.231, source: 'v3' },
    { key: 'k_vent_window', kind: 'per_hour', value: -0.138, source: 'v3' },
    { key: 'k_vent_fan', kind: 'per_hour', value: null, source: 'none' },
  ];
  const engines = Object.assign({}, FIX.engines, { unit: auto.unit, v4, decisions });
  if (opts.classification) auto.classification = opts.classification;
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
const value = (page, key) => page.locator('#dbg-groups tr').filter({ has: page.locator('td.k .kn', { hasText: new RegExp('^' + key + '$') }) }).locator('td.mono');

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
    await expect(value(page, 'Training-fit miss')).toHaveText('1 h 0.65°F · 4 h 0.81°F · 12 h 0.80°F');
    await expect(value(page, 'Sensors modelled')).toHaveText('Climate Advisor reading only');
    await expect(value(page, 'Forecast model')).toHaveText(/^v4 forecasts each room separately \(thermostat by day, bedroom at night\): .*0\.45°F vs 0\.60°F at 4 h/);
    await expect(value(page, 'Last prediction')).toHaveText(/^\d{1,2}:26\s?AM, 96 forecast inputs, 7 ms, 0 new slots archived$/);
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
    await expect(value(page, 'Training-fit miss')).toHaveText('1 h 0.36°C · 4 h 0.45°C · 12 h 0.44°C');
    await expect(value(page, 'Decisions: heating rate')).toHaveText('2.511 °C/h · from v4');
    await expect(value(page, 'Mass node')).toHaveText('19.7°C');
    await expect(value(page, 'Forecast model')).toContainText('0.25°C vs 0.33°C at 4 h');
    // (the fixture's last_action_reason is a captured °F sentence, so the check is scoped to the engine block)
    const engines = page.locator('#dbg-groups section').filter({ has: page.locator('h2', { hasText: /^Prediction engines/ }) });
    const cells = await engines.locator('td.mono').allInnerTexts();
    expect(cells.length).toBeGreaterThan(8);
    for (const c of cells) expect(c, c).not.toMatch(/°F/);
  });

  test('the decisions rows say which engine each value comes from; v3\'s learned values are labelled v3', async ({ page }) => {
    await openDebug(page);
    await expect(value(page, 'Grade reason')).toHaveText('low: too few usable cooling starts (ok needs 5 in each mode it has seen)');
    await expect(value(page, 'By mode')).toHaveText(/^heating 45 usable of 61 starts \(enough\) · cooling 0 usable of 2 starts \(needs 5\)/);
    await expect(value(page, 'Decision grade')).toHaveText('low · from v4');
    await expect(value(page, 'Decisions: heat-loss rate')).toHaveText('-0.034 /h · from v4');
    await expect(value(page, 'Decisions: heating rate')).toHaveText('4.52 °F/h · from v4');
    await expect(value(page, 'Decisions: sun timing')).toHaveText('3.231 h · from v3’s learned observations');
    await expect(value(page, 'Decisions: window venting')).toHaveText('-0.138 /h · from v3’s learned observations');
    await expect(value(page, 'Decisions: fan venting')).toHaveText('not learned yet (decisions use their defaults)');
    // v3's own rows keep their values but say they are v3's (none is bare "k_passive" or "physics" any more)
    await expect(value(page, 'v3 k_passive')).toHaveCount(1);
    await expect(value(page, 'v3 model')).toHaveText(/ physics · /);
    await expect(value(page, 'k_passive')).toHaveCount(0);
    await expect(value(page, 'physics')).toHaveCount(0);
  });

  test('a v4 still learning: its blanked rates read as defaults, never as numbers', async ({ page }) => {
    const pe = engineState({ fallback: true, primary_state: 'learning' });
    pe.engines[1].usable = false;
    await openDebug(page, { engines: pe, decisions: [
      { key: 'confidence', kind: 'grade', value: 'none', source: 'default' },
      { key: 'k_passive', kind: 'per_hour', value: null, source: 'default' },
      { key: 'k_vent_window', kind: 'per_hour', value: -0.138, source: 'v3' },
    ] });
    await expect(value(page, 'Decision grade')).toHaveText('default (v4 still learning)');
    await expect(value(page, 'Decisions: heat-loss rate')).toHaveText('default (v4 still learning)');
    await expect(value(page, 'Decisions: window venting')).toHaveText('-0.138 /h · from v3’s learned observations');
  });

  // the live 2026-11-08 mild day: the high (65) misses comfort heat (68), the low passes comfort cool - 3 (71)
  const mildDay = (over) => Object.assign({
    day_type: 'mild', trend_direction: 'warming', trend_magnitude: 3.8, hvac_mode: 'off', windows_recommended: false,
    window_open_time: '10:00:00', window_close_time: '17:00:00', window_opportunity_morning: false, window_opportunity_evening: false,
    window_opportunity_morning_start: null, window_opportunity_morning_end: null, window_opportunity_evening_start: null, window_opportunity_evening_end: null,
    pre_condition: false, pre_condition_target: null, setback_modifier: 0.0, today_low: 49.0, tomorrow_low: 51.2,
    today_high: 65.0, low_is_estimated: false, forecast_windows_recommended: false,
    window_rule: { kind: 'range', high_at_least: 68.0, low_at_most: 71.0, opportunity_low_at_most: 80.0, hysteresis: false },
    trend_thresholds: { moderate: 5, significant: 10 },
  }, over || {});
  const hint = (page, key) => page.locator('#dbg-groups tr').filter({ has: page.locator('td.k .kn', { hasText: new RegExp('^' + key + '$') }) }).locator('td.k .hint');

  test('today\'s classification: why windows are off, which times are fixed, and the thresholds each low is judged on', async ({ page }) => {
    await openDebug(page, { classification: mildDay() });
    await expect(value(page, 'windows_recommended')).toHaveText('no (today’s high 65.0°F is below 68.0°F)');
    await expect(value(page, 'window times')).toHaveText(/^10:00\s?AM – 5:00\s?PM \(fixed for a mild day; windows not recommended today\)$/);
    await expect(value(page, 'morning opportunity')).toHaveText('not checked (hot days only)');
    await expect(value(page, 'today_high')).toHaveText('65.0°F');
    await expect(hint(page, 'today_low')).toHaveText('Windows need today’s low at or below 71.0°F');
    await expect(hint(page, 'tomorrow_low')).toHaveText('Used only on hot days (the evening opportunity)');
    await expect(hint(page, 'trend_magnitude')).toHaveText('The setback adjusts at 5° (by 2.0°) and 10° (by 3.0°)');
    await expect(value(page, 'window_open_time')).toHaveCount(0);
  });

  test('a hot day: the opportunities name the 80° low they need', async ({ page }) => {
    await openDebug(page, { classification: mildDay({ day_type: 'hot', hvac_mode: 'cool', window_open_time: null, window_close_time: null,
      window_rule: { kind: 'opportunity', high_at_least: 68.0, low_at_most: 71.0, opportunity_low_at_most: 80.0, hysteresis: false },
      window_opportunity_morning: true, window_opportunity_morning_start: '06:00:00', window_opportunity_morning_end: '10:00:00', tomorrow_low: 82.0 }) });
    await expect(value(page, 'morning opportunity')).toHaveText(/^yes, 6:00\s?AM – 10:00\s?AM$/);
    await expect(value(page, 'evening opportunity')).toHaveText('no (tomorrow’s low above 80.0°F)');
    await expect(value(page, 'window times')).toHaveText('none on a hot day');
  });

  test('per-mode grades (#1469): the primary row names both, for information only', async ({ page }) => {
    await openDebug(page, { v4: { modes: {
      heat: { seen: true, starts: 61, accepted: 45, grade: 'ok', reason: '' },
      cool: { seen: true, starts: 2, accepted: 0, grade: 'low', reason: 'too few accepted thermostat starts' } } } });
    await expect(value(page, 'Primary engine')).toHaveText('v4: usable · heating ok · cooling low');
    // the decisions keep reading the whole-model grade
    await expect(value(page, 'Decision grade')).toHaveText('low · from v4');
    await expect(value(page, 'Decisions: season')).toHaveCount(0);
  });

  test('a home without AC: cooling reads "not used"', async ({ page }) => {
    await openDebug(page, { v4: { modes: { heat: { seen: true, starts: 61, accepted: 45, grade: 'ok' }, cool: { seen: false, starts: 0, accepted: 0, grade: 'unseen' } } } });
    await expect(value(page, 'Primary engine')).toHaveText('v4: usable · heating ok · cooling not used');
  });

  test('capped skip counts, durations and the remote speed read in words', async ({ page }) => {
    await openDebug(page);
    await expect(value(page, 'skipped solar_gain')).toHaveText('100 or more (the list keeps the newest 100)');
    await expect(value(page, 'manual_grace_duration')).toHaveText('3 h');
    await expect(value(page, 'fan_remote_speed')).toHaveText('low (fan is off)');
  });

  test('while the primary has no usable fit the decisions say so in words', async ({ page }) => {
    const pe = engineState({ fallback: true, primary_state: 'learning' });
    pe.engines[1].usable = false;
    await openDebug(page, { controlFeed: { mode: 'defaults', path_points: 49, own_path: true }, engines: pe });
    await expect(value(page, 'Decisions use')).toHaveText('default behaviour (v4 still learning)');
    await expect(value(page, 'Primary engine')).toHaveText('v4: still learning');
  });
});
