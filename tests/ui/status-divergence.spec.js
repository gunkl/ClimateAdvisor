const { test, expect } = require('@playwright/test');

// Issue #402: the single-setpoint HVAC status card previously had no CA-vs-actual
// divergence indicator, unlike the heat_cool card — so a thermostat setpoint that
// stopped tracking CA's intended target (e.g. suppressed while a whole-house-fan
// session owns the thermostat) showed no indication of staleness. These tests
// intercept /api/climate_advisor/status per-test to exercise both the diverged and
// converged cases, since the shared mock-server.js returns fixed data.

test.describe('Status card CA-target divergence indicator (Issue #402)', () => {

  test('shows (CA: X) annotation when real setpoint diverges from CA target by >1 degree', async ({ page }) => {
    await page.route('**/api/climate_advisor/status', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          day_type: 'warm',
          hvac_mode: 'cool',
          current_setpoint: 74,
          ca_target_heat: 68,
          ca_target_cool: 72,
          indoor_temp: 70,
          outdoor_temp: 60,
          automation_enabled: true,
          occupancy_mode: 'home',
          automation_status: 'active',
          compliance_score: 1.0,
        }),
      });
    });

    await page.goto('/');
    await page.waitForSelector('#status-grid', { state: 'visible' });

    const hvacItem = page.locator('.status-item', { hasText: 'HVAC' });
    const html = await hvacItem.innerHTML();
    expect(html).toContain('74');
    expect(html).toContain('(CA: 72');
  });

  test('shows no annotation when real setpoint matches CA target', async ({ page }) => {
    await page.route('**/api/climate_advisor/status', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          day_type: 'warm',
          hvac_mode: 'cool',
          current_setpoint: 72,
          ca_target_heat: 68,
          ca_target_cool: 72,
          indoor_temp: 70,
          outdoor_temp: 60,
          automation_enabled: true,
          occupancy_mode: 'home',
          automation_status: 'active',
          compliance_score: 1.0,
        }),
      });
    });

    await page.goto('/');
    await page.waitForSelector('#status-grid', { state: 'visible' });

    const hvacItem = page.locator('.status-item', { hasText: 'HVAC' });
    const html = await hvacItem.innerHTML();
    expect(html).toContain('72');
    expect(html).not.toContain('(CA:');
  });

});
