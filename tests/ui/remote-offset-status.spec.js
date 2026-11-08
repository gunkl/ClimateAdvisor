const { test, expect } = require('@playwright/test');

// Issue #1194 (d): while the remote-sensor outer loop holds an offset the thermostat is DELIBERATELY at
// target + offset, so the HVAC card must not show the #402 "(CA: X)" disagreement for it, must explain the offset
// on its own line, and must still flag a thermostat that reports something other than the commanded value.

const BASE = {
  day_type: 'cold',
  hvac_mode: 'heat',
  ca_target_heat: 66,
  ca_target_cool: 72,
  indoor_temp: 64,
  outdoor_temp: 40,
  automation_enabled: true,
  occupancy_mode: 'home',
  automation_status: 'active',
  compliance_score: 1.0,
  unit: 'fahrenheit',
};
const OFFSET = {
  state: 'clamped',
  mode: 'heat',
  text: 'Bedroom 66°F target - thermostat set 70°F (4° above the target, clamped at the 4° cap)',
  commanded: 70,
  expected_setpoint: 70,
  offset_active: true,
};

async function hvacHtml(page, body) {
  await page.route('**/api/climate_advisor/status**', (route) => {
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
  await page.goto('/');
  await page.waitForSelector('#status-grid', { state: 'visible' });
  return page.locator('.status-item', { hasText: 'HVAC' }).innerHTML();
}

test.describe('Status card offset line (Issue #1194 d)', () => {
  test('an offset night shows the target and the command, not a (CA: 66) disagreement', async ({ page }) => {
    const html = await hvacHtml(page, { ...BASE, current_setpoint: 70, remote_offset: OFFSET });
    expect(html).toContain('Bedroom 66°F target - thermostat set 70°F (4° above the target, clamped at the 4° cap)');
    expect(html).not.toContain('Climate Advisor wants');
  });

  test('a thermostat reporting something other than the commanded value still shows (CA: X)', async ({ page }) => {
    const html = await hvacHtml(page, { ...BASE, current_setpoint: 74, remote_offset: OFFSET });
    expect(html).toContain('(Climate Advisor wants 70');
  });

  test('a thermostat no longer in the offset mode is not compared with the commanded value', async ({ page }) => {
    const html = await hvacHtml(page, { ...BASE, hvac_mode: 'off', current_setpoint: 74, remote_offset: OFFSET });
    expect(html).not.toContain('Climate Advisor wants');
  });

  test('no offset keeps the plain band-edge comparison', async ({ page }) => {
    const html = await hvacHtml(page, {
      ...BASE,
      current_setpoint: 70,
      remote_offset: { state: 'holding', text: null, expected_setpoint: null, offset_active: false },
    });
    expect(html).toContain('(Climate Advisor wants 66');
    expect(html).not.toContain('remote-offset-line');
  });
});

// Issue #1194 (d) P2: the dashed "Thermostat set" chart series has a legend item only while it exists.
test.describe('Chart legend for the commanded setpoint (Issue #1194 d)', () => {
  test('the Thermostat set legend item is hidden on a page with no offset night', async ({ page }) => {
    await page.route('**/api/climate_advisor/status**', (route) => {
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(BASE) });
    });
    await page.goto('/');
    await page.waitForSelector('#status-grid', { state: 'visible' });
    await expect(page.locator('#legend-thermostat-set')).toBeHidden();
  });
});
