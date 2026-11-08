const { test, expect } = require('@playwright/test');

// Issue #1477 owner decision (PR #1482): the rounded-up swing (comfort detectors, thermostat model) is shown with the
// measured one (the bedroom loop's trigger) beside it, on both dashboards. Zone 1 on 2026-11-08: heat measured
// 1.44 F, stored 1.5 F; cool not learned yet (the 1.5 F default, no "measured" note). Celsius: x 5/9, no offset.
function thermal(unit) {
  const c = unit === 'celsius', d = (f) => Math.round((c ? f * 5 / 9 : f) * 100) / 100;
  return {
    unit, confidence: 'high', heating_rate: d(5.3), cooling_rate: d(6.1),
    swing_heat: d(1.5), swing_cool: d(1.5), swing_heat_raw: d(1.44), swing_cool_raw: d(1.5),
    swing_heat_measured: true, swing_cool_measured: false,
    observation_count_swing_heat: 7, observation_count_swing_cool: 0,
    observation_count_heat: 12, observation_count_cool: 0, confidence_swing_heat: 'medium', confidence_swing_cool: 'none',
  };
}

async function routeChart(page, unit) {
  await page.route('**/api/climate_advisor/chart_data*', async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    body.thermal_model = Object.assign({}, body.thermal_model || {}, thermal(unit));
    body.unit = unit;
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
}

for (const [unit, heat, raw, cool] of [['fahrenheit', '1.5 °F', '1.44 °F', '1.5 °F'], ['celsius', '0.83 °C', '0.80 °C', '0.83 °C']]) {
  test(`new dashboard Analysis: thermostat swing shows stored and measured (${unit})`, async ({ page }) => {
    await routeChart(page, unit);
    await page.goto('/new_dashboard.html#analysis');
    const model = page.locator('#an-model');
    await expect(model).toContainText(`Thermostat swing: heat starts ${heat} (measured ${raw}) below the setpoint, cooling ${cool} above`, { timeout: 8000 });
    await expect(model).not.toContainText(`cooling ${cool} (measured`);
  });

  test(`classic dashboard: heat swing row shows stored and measured (${unit})`, async ({ page }) => {
    await routeChart(page, unit);
    await page.goto('/');
    const panel = page.locator('#thermal-model-content');
    await expect(panel).toContainText(`Heat swing${heat} (measured ${raw}) below setpoint`, { timeout: 8000 });
    await expect(panel).toContainText(`Cool swing${cool} above setpoint`);  // nothing measured: no note
  });
}
