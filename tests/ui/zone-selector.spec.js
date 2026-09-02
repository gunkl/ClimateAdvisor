const { test, expect } = require('@playwright/test');

// Issue #796 PR9: dashboard zone selector. The selector row (#zone-selector-row)
// must stay hidden/empty for single-zone installs (zone_count <= 1, the
// overwhelming majority of installs today) and must render one pill button per
// zone, driven by /api/climate_advisor/status's zones/zone_count fields, only
// when zone_count > 1. Selecting a zone must re-fetch status scoped to that
// zone's entry_id (verified via the query string on the follow-up request).

test.describe('Zone selector (Issue #796 PR9)', () => {

  test('single-zone install: selector row stays hidden (pixel-identical to today)', async ({ page }) => {
    await page.route('**/api/climate_advisor/status**', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          day_type: 'mild',
          hvac_mode: 'off',
          indoor_temp: 71,
          outdoor_temp: 65,
          automation_enabled: true,
          occupancy_mode: 'home',
          automation_status: 'active',
          compliance_score: 1.0,
          zones: [{ entry_id: 'zone-a', title: 'Climate Advisor' }],
          zone_count: 1,
        }),
      });
    });

    await page.goto('/');
    await page.waitForSelector('#status-grid', { state: 'visible' });

    const row = page.locator('#zone-selector-row');
    await expect(row).toBeHidden();
    expect((await row.innerHTML()).trim()).toBe('');
  });

  test('zero zones (e.g. right after reload): selector row stays hidden', async ({ page }) => {
    await page.route('**/api/climate_advisor/status**', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          day_type: 'mild',
          hvac_mode: 'off',
          automation_enabled: true,
          occupancy_mode: 'home',
          automation_status: 'unknown',
          compliance_score: 1.0,
          zones: [],
          zone_count: 0,
        }),
      });
    });

    await page.goto('/');
    await page.waitForSelector('#status-grid', { state: 'visible' });

    await expect(page.locator('#zone-selector-row')).toBeHidden();
  });

  test('multi-zone install: renders one button per zone and selecting one re-scopes requests', async ({ page }) => {
    await page.route('**/api/climate_advisor/status**', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          day_type: 'mild',
          hvac_mode: 'off',
          indoor_temp: 71,
          outdoor_temp: 65,
          automation_enabled: true,
          occupancy_mode: 'home',
          automation_status: 'active',
          compliance_score: 1.0,
          zones: [
            { entry_id: 'zone-bedroom', title: 'Bedroom' },
            { entry_id: 'zone-living', title: 'Living Room' },
          ],
          zone_count: 2,
        }),
      });
    });

    await page.goto('/');
    await page.waitForSelector('#status-grid', { state: 'visible' });

    const row = page.locator('#zone-selector-row');
    await expect(row).toBeVisible();
    const buttons = row.locator('.zone-tab-btn');
    await expect(buttons).toHaveCount(2);
    await expect(buttons.nth(0)).toHaveText('Bedroom');
    await expect(buttons.nth(1)).toHaveText('Living Room');
    // First zone selected by default.
    await expect(buttons.nth(0)).toHaveClass(/active/);
    await expect(buttons.nth(1)).not.toHaveClass(/active/);

    // Selecting the second zone must trigger a status re-fetch carrying its entry_id.
    const nextStatusRequest = page.waitForRequest((req) =>
      req.url().includes('/api/climate_advisor/status') && req.url().includes('entry_id=zone-living')
    );
    await buttons.nth(1).click();
    await nextStatusRequest;

    await expect(buttons.nth(1)).toHaveClass(/active/);
    await expect(buttons.nth(0)).not.toHaveClass(/active/);
  });

  test('multi-zone install: clicking a zone alone (no range-button click) re-fetches the chart with the correct entry_id', async ({ page }) => {
    await page.route('**/api/climate_advisor/status**', (route) => {
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          day_type: 'mild',
          hvac_mode: 'off',
          automation_enabled: true,
          occupancy_mode: 'home',
          automation_status: 'active',
          compliance_score: 1.0,
          zones: [
            { entry_id: 'zone-bedroom', title: 'Bedroom' },
            { entry_id: 'zone-living', title: 'Living Room' },
          ],
          zone_count: 2,
        }),
      });
    });

    // Registered before navigation (not after waitForSelector below) so it
    // can't miss the initial chart_data request firing from loadAll() —
    // that request can resolve before a post-navigation listener would even
    // be attached. Awaiting it here lets the page's initial load settle
    // before the click below, so the later waitForRequest can only be
    // satisfied by a chart request the zone click itself triggers.
    const initialChartResponse = page.waitForResponse((res) => res.url().includes('/api/climate_advisor/chart_data'));
    await page.goto('/');
    await initialChartResponse;
    await page.waitForSelector('#status-grid', { state: 'visible' });

    const buttons = page.locator('#zone-selector-row .zone-tab-btn');
    // No range-button click here — this is the point of the test. Before the
    // Issue #796 Verification fix, _refreshAll() (fired by the zone click)
    // never called loadChart(), so this request would only ever arrive from
    // the unrelated 5-min _chartCycle poll and this waitForRequest would time
    // out — the fix wires loadChart() directly into the zone click handler.
    const nextChartRequest = page.waitForRequest((req) =>
      req.url().includes('/api/climate_advisor/chart_data') && req.url().includes('entry_id=zone-living')
    );
    await buttons.nth(1).click();
    await nextChartRequest;
  });

});
