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

// Issue #407: the "Natural Vent" info previously rendered as its own separate
// status-item card, duplicating (and drifting from) the main Status card's own
// nat-vent target text — a UI the user never asked for (a byproduct of the #402
// follow-up fix). Merged the cycling band (off/on threshold + target) and
// AC-assist/savings-mode label back into the Status card as a supplemental line,
// and removed the standalone card. This still guards against "target 71°F but
// indoor is 69°F, why is the fan still on" by showing the band makes clear 69°F
// is within the fan's normal cycling range, not a contradiction.
test.describe('Natural Vent info merged into Status card (Issue #407)', () => {

  test('Status card shows the cycling band when nat-vent is active', async ({ page }) => {
    await page.route('**/api/climate_advisor/status', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          day_type: 'warm',
          hvac_mode: 'off',
          indoor_temp: 69,
          outdoor_temp: 65,
          automation_enabled: true,
          occupancy_mode: 'home',
          automation_status: 'active',
          compliance_score: 1.0,
          nat_vent_active: true,
          nat_vent_ac_assist: false,
          nat_vent_target: 71,
          nat_vent_on_threshold: 72,
          nat_vent_off_threshold: 70,
        }),
      });
    });

    await page.goto('/');
    await page.waitForSelector('#status-grid', { state: 'visible' });

    // No separate "Natural Vent" card should exist anymore.
    await expect(page.locator('.status-item .label', { hasText: 'Natural Vent' })).toHaveCount(0);

    const statusItem = page.locator('.status-item', { hasText: 'Status' }).first();
    await expect(statusItem).toBeVisible();
    const html = await statusItem.innerHTML();
    expect(html).toContain('70');
    expect(html).toContain('72');
    expect(html).toContain('71');
    expect(html).toContain('savings mode');
  });

  test('Status card shows no nat-vent line when nat-vent is not active', async ({ page }) => {
    await page.route('**/api/climate_advisor/status', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          day_type: 'warm',
          hvac_mode: 'off',
          indoor_temp: 69,
          outdoor_temp: 65,
          automation_enabled: true,
          occupancy_mode: 'home',
          automation_status: 'active',
          compliance_score: 1.0,
          nat_vent_active: false,
        }),
      });
    });

    await page.goto('/');
    await page.waitForSelector('#status-grid', { state: 'visible' });

    const statusItem = page.locator('.status-item', { hasText: 'Status' }).first();
    const html = await statusItem.innerHTML();
    expect(html).not.toContain('Natural ventilation');
  });

});
