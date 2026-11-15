const { test, expect } = require('@playwright/test');
const FIX = require('../fixtures/new_dashboard_api.json');

// Issue #1341 S3: REST `unit` is temperature-only. A time setting's unit rides in `time_unit`, so the classic page,
// which appends `unit` to a value, shows exactly what it showed before the time units existed: the bare number (the
// label already says "(minutes)"), and a temperature with its symbol.
test('classic Settings: a time setting is the bare number, a temperature has its symbol', async ({ page }) => {
  await page.route('**/api/climate_advisor/config*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(FIX.config) }));
  await page.goto('/');
  await page.locator('[data-tab="settings"]').first().click();
  await page.waitForSelector('#settings-content .setting-row', { timeout: 8000 });
  const value = (label) => page.locator('#settings-content .setting-row', { has: page.locator('.setting-label', { hasText: label }) }).locator('.setting-value').first().innerText();
  expect((await value('Sensor Debounce (minutes)')).trim()).toBe('10');
  expect((await value('Manual Grace Period (minutes)')).trim()).toBe('180');
  expect((await value('Comfort Heat')).trim()).toBe('68°F');
  // the fixture rows carry time_unit, never unit, for the time settings
  const row = FIX.config.settings.find((s) => s.key === 'sensor_debounce_seconds');
  expect(row.time_unit).toBe('min');
  expect(row.unit).toBeUndefined();
});
