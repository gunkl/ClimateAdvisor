const { test, expect } = require('@playwright/test');
const FIX = require('../fixtures/new_dashboard_api.json');

// Issue #1475: the Analysis tab says what each number covers. The payloads follow Zone 1's live shapes of 2026-11-08:
// a mild day whose record latched "windows recommended" while the live outdoor check said no, yesterday's record from
// the day the heat/cool split reached the house mid-day, the 14-day compliance window over 91 records, all-time
// promise misses, and v4 as the primary engine (its own 4 h miss, not v3's logged forecast).

const ymd = (d) => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
const iso = (h, m, dayOffset) => { const d = new Date(); d.setDate(d.getDate() + (dayOffset || 0)); d.setHours(h, m, 0, 0); return d.toISOString(); };

function learning(over) {
  const today = new Date(), y = new Date(); y.setDate(y.getDate() - 1);
  return Object.assign({}, FIX.learning, {
    unit: 'fahrenheit',
    yesterday_record: {
      date: ymd(y), day_type: 'warm', trend_direction: 'cooling', windows_recommended: true, window_open_time: '06:00:00', window_close_time: '10:00:00',
      hvac_mode_recommended: 'off', windows_physically_opened: true, window_physical_open_time: iso(10, 48, -1), window_physical_close_time: iso(18, 15, -1),
      windows_opened: false, hvac_runtime_minutes: 42.77, hvac_heat_runtime_minutes: 21.68, hvac_cool_runtime_minutes: 0.0, thermostat_fan_only_runtime_minutes: 0,
      occupancy_away_minutes: 64.9, door_window_pause_events: 1, manual_overrides: 0, avg_indoor_temp: 67.5, comfort_violations_minutes: 233.3, estimated_cost: null,
      forecast_high_f: 77.0, forecast_low_f: 54.0, observed_high_f: 75.6, observed_low_f: 47.5, setback_depth_f: null, setback_skipped_reason: null,
    },
    today_record: {
      date: ymd(today), day_type: 'mild', trend_direction: 'warming', windows_recommended: true, window_open_time: '10:00:00', window_close_time: '17:00:00',
      hvac_mode_recommended: 'off', windows_physically_opened: true, window_physical_open_time: iso(8, 2), window_physical_close_time: iso(8, 9),
      windows_opened: false, hvac_runtime_minutes: 0, hvac_heat_runtime_minutes: 0, hvac_cool_runtime_minutes: 0, thermostat_fan_only_runtime_minutes: 0,
      occupancy_away_minutes: 0, door_window_pause_events: 1, manual_overrides: 0, avg_indoor_temp: null, comfort_violations_minutes: 22.4, estimated_cost: null,
      forecast_high_f: 68.0, forecast_low_f: 49.0, observed_high_f: null, observed_low_f: null, setback_depth_f: null, setback_skipped_reason: null,
    },
    tomorrow_plan: { date: ymd(new Date(Date.now() + 864e5)), day_type: 'mild', trend_direction: 'stable', hvac_mode: 'off', windows_recommended: true,
      window_open_time: '11:29:02.888217', window_close_time: '16:47:26.579327', pre_condition: false, expected_high: 73.4, expected_low: 51.2 },
    compliance: { status: 'active', days_recorded: 91, window_days: 14, window_compliance: 0.6923, window_compliance_denominator: 13,
      avg_daily_hvac_runtime_minutes: 90.3, avg_daily_thermostat_fan_only_runtime_minutes: 0, comfort_score: 0.698, total_manual_overrides: 33, pending_suggestions: 0 },
    today_live: { windows_recommended_now: false, forecast_windows_recommended: true, day_type: 'mild', plan_windows: [],
      observed_high_f: 67.4, observed_low_f: 52.0, avg_indoor_f: 67.1 },
    cost_configured: false,
  }, over || {});
}
const accuracy = { primary_engine: 'v4', shadow_engine: 'v3', unit: '°F', stats: { n: 39, v4_mae: 0.5, v3_mae: 1.16, flat_mae: 1.19, v4_bias: 0.17, v3_bias: 0.84 } };
const promises = { promises: [
  { promise: 'comfort_kept', label: '7: comfort kept', hits: 4, last_hit: '2026-11-03T05:05:57-08:00' },
  { promise: 'whf_sealed', label: '9: whf sealed', hits: 1, last_hit: '2026-11-01T15:04:04-08:00' }], total_hits: 5 };

async function openAnalysis(page, opts) {
  opts = opts || {};
  const json = (body) => (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  await page.route('**/api/climate_advisor/learning**', json(learning(opts.learning)));
  await page.route('**/api/climate_advisor/prediction_accuracy**', json(opts.accuracy || accuracy));
  await page.route('**/api/climate_advisor/status**', async (route) => {
    const resp = await route.fetch(); const body = await resp.json(); body.promise_summary = opts.promises === undefined ? promises : opts.promises;
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
  await page.goto('/new_dashboard.html#analysis');
  await page.waitForSelector('#an-days table', { timeout: 8000 });
}
const tile = (page, label) => page.locator('#an-kpis .tile').filter({ has: page.locator('.l', { hasText: new RegExp('^' + label + '$') }) });
const cells = (page, label) => page.locator('#an-days tr').filter({ has: page.locator('th', { hasText: new RegExp('^' + label + '$') }) }).locator('td');

test.describe('Analysis tab in words (#1475)', () => {
  test('headline tiles: the 14-day window, 30-day promises (#1490), and the main engine\'s own 4 h miss', async ({ page }) => {
    await openAnalysis(page);
    await expect(tile(page, 'Comfort score').locator('.n')).toHaveText('last 14 days (91 recorded)');
    await expect(tile(page, 'Window advice followed').locator('.n')).toHaveText('13 days with advice, last 14 days');
    await expect(tile(page, 'Manual changes').locator('.n')).toHaveText('last 14 days · 0 today');
    await expect(tile(page, 'Promises missed').locator('.n')).toHaveText(/^last 30 days · last /);
    await expect(tile(page, 'Promises missed').locator('.n')).not.toContainText('24 h');
    await expect(tile(page, 'Prediction error, 4 h').locator('.v')).toHaveText('0.5°');
    await expect(tile(page, 'Prediction error, 4 h').locator('.n')).toHaveText('v4, last 24 h · runs +0.2° warm');
    await expect(page.locator('#an-compliance')).toContainText('Over the last 14 of 91 recorded days');
    await expect(page.locator('#an-compliance')).not.toContainText('in total');
  });

  test('today\'s window advice says what holds now; the opened row says whether it counted', async ({ page }) => {
    await openAnalysis(page);
    const adv = cells(page, 'Windows advised');
    await expect(adv.nth(0)).toHaveText('yes · 6:00–10:00 (fixed for a warm day)');
    await expect(adv.nth(1)).toHaveText('earlier today, not now (the live outdoor check says no)');
    await expect(adv.nth(2)).toHaveText('yes · plan 11:29–16:47');
    const open = cells(page, 'Windows actually open');
    await expect(open.nth(0)).toHaveText(/^yes · 10:48\s?AM–6:15\s?PM · not counted \(advice was off when opened\)$/);
    await expect(open.nth(1)).toHaveText(/^yes · 8:02\s?AM–8:09\s?AM · not counted/);
  });

  test('today with plan periods names them, not the fixed day-type times', async ({ page }) => {
    await openAnalysis(page, { learning: { today_live: { windows_recommended_now: true, forecast_windows_recommended: true, plan_windows: [['11:30:00', '16:45:00']], observed_high_f: 67.4, observed_low_f: 52, avg_indoor_f: 67.1 } } });
    await expect(cells(page, 'Windows advised').nth(1)).toHaveText('yes · plan 11:30–16:45');
  });

  test('days table: recommended mode, the runtime split, today so far, and why a cell is empty', async ({ page }) => {
    await openAnalysis(page);
    await expect(cells(page, 'Recommended HVAC mode').nth(0)).toHaveText('off');
    await expect(cells(page, 'HVAC runtime').nth(0)).toHaveText('43 min (heat/cool split not recorded for the whole day)');
    await expect(cells(page, 'HVAC runtime').nth(1)).toHaveText('0 min (heat 0 · cool 0)');
    await expect(cells(page, 'High / low \\(forecast → observed\\)').nth(1)).toHaveText('68 / 49 · so far 67 / 52');
    await expect(cells(page, 'High / low \\(forecast → observed\\)').nth(0)).toHaveText('77→76 / 54→48');
    await expect(cells(page, 'Avg indoor · minutes outside comfort').nth(1)).toHaveText('67.1°F so far · 22 min (2% of day)');
    await expect(cells(page, 'Night setback').nth(0)).toHaveText('not recorded');
    await expect(cells(page, 'Night setback').nth(1)).toHaveText('after bedtime');
    await expect(cells(page, 'Pre-conditioning').nth(0)).toHaveText('not recorded');
    await expect(cells(page, 'Pre-conditioning').nth(2)).toHaveText('no');
    await expect(cells(page, 'Estimated cost').nth(0)).toHaveText('set an energy rate in Settings');
  });

  test('a recorded setback and a skipped one read in words', async ({ page }) => {
    const l = learning();
    await openAnalysis(page, { learning: {
      yesterday_record: Object.assign({}, l.yesterday_record, { setback_depth_f: 2.0 }),
      today_record: Object.assign({}, l.today_record, { setback_skipped_reason: 'paused_by_door' }),
      cost_configured: true } });
    await expect(cells(page, 'Night setback').nth(0)).toHaveText('2.0°F');
    await expect(cells(page, 'Night setback').nth(1)).toHaveText('skipped (paused by door)');
    await expect(cells(page, 'Estimated cost').nth(1)).toHaveText('after the day ends');
  });

  test('the look-ahead bars say they are v3\'s log when v4 is the main engine; promises are the last 30 days (#1490)', async ({ page }) => {
    await openAnalysis(page, { promises: null });
    await expect(page.locator('#an-promises')).toHaveText('None missed in the last 30 days.');
    await expect(page.locator('h3', { hasText: 'Household promises missed' })).toContainText('last 30 days');
    await expect(tile(page, 'Promises missed').locator('.n')).toHaveText('none in the last 30 days');
  });
});
