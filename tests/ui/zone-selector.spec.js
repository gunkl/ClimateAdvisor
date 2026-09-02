const { test, expect } = require('@playwright/test');

// Issue #796 PR9: dashboard zone selector. The selector row (#zone-selector-row)
// must stay hidden/empty for single-zone installs (zone_count <= 1, the
// overwhelming majority of installs today) and must render one pill button per
// zone, driven by /api/climate_advisor/status's zones/zone_count fields, only
// when zone_count > 1. Selecting a zone must re-fetch status scoped to that
// zone's entry_id (verified via the query string on the follow-up request).
//
// Issue #813: most tests below mock /status to return the same full-status
// body regardless of whether entry_id is present. That's a simplification —
// the REAL backend (api.py::ClimateAdvisorStatusView.get(), Issue #813) now
// returns a zone-list-only { zone_selection_required: true, ... } body when
// entry_id is absent AND 2+ zones exist, and only returns full status once
// entry_id is present. The mocks below are fine for what THEY test (click
// behavior, persistence, the stale-503 self-heal), since real usage always
// ends up sending entry_id after the very first exchange either way — but
// the bootstrap contract itself is covered explicitly by the dedicated
// describe block at the bottom of this file, which mirrors the real
// two-step shape instead of collapsing it into one mocked response.

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

  // Issue #812: _selectedEntryId used to start out null on every single page
  // load (nothing persisted it), so all non-status loadAll() calls went out
  // zone-blind until the first /status response resolved a zone. These two
  // cases cover localStorage persistence across a reload and graceful
  // fallback when the persisted zone no longer exists.

  test('Issue #812: zone selection persists across a page reload', async ({ page }) => {
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

    await page.goto('/');
    await page.waitForSelector('#status-grid', { state: 'visible' });

    const buttons = page.locator('#zone-selector-row .zone-tab-btn');
    const statusAfterClick = page.waitForRequest((req) =>
      req.url().includes('/api/climate_advisor/status') && req.url().includes('entry_id=zone-living')
    );
    await buttons.nth(1).click();
    await statusAfterClick;

    // Reload: this is a genuine first-ever-visit's opposite case — a stored
    // zone already exists, so _selectedEntryId must be seeded from
    // localStorage synchronously (before loadAll() fires) and the very first
    // status request after reload must already carry the persisted zone,
    // rather than only re-selecting it after the response comes back.
    const statusAfterReload = page.waitForRequest((req) =>
      req.url().includes('/api/climate_advisor/status') && req.url().includes('entry_id=zone-living')
    );
    await page.reload();
    await statusAfterReload;
    await page.waitForSelector('#status-grid', { state: 'visible' });

    await expect(buttons.nth(1)).toHaveClass(/active/);
    await expect(buttons.nth(0)).not.toHaveClass(/active/);
  });

  test('Issue #812: a stored zone that no longer exists falls back gracefully', async ({ page }) => {
    // Issue #812 Verification fix: the original mock here returned HTTP 200
    // with the full zone list regardless of the `entry_id` query param, so it
    // never actually reproduced the real backend contract and the test passed
    // even though production deadlocked (Finding 1). The real backend
    // (api.py::_get_coordinator -> zone_registry.get_coordinator()) returns
    // no coordinator for an unknown entry_id, and the view responds with
    // HTTP 503 {"error": "Climate Advisor not loaded"} — mirror that exactly:
    // only the two real zone ids (or no entry_id at all, the self-heal retry)
    // get a 200; the stale 'zone-removed' id gets the same 503 the live
    // backend would send.
    await page.route('**/api/climate_advisor/status**', (route) => {
      const url = new URL(route.request().url());
      const entryId = url.searchParams.get('entry_id');
      if (entryId && entryId !== 'zone-bedroom' && entryId !== 'zone-living') {
        route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ error: 'Climate Advisor not loaded' }),
        });
        return;
      }
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

    // Seed localStorage with an entry_id for a zone that has since been
    // removed, before any page script runs — simulates a returning user
    // whose stored selection is now stale.
    await page.addInitScript(() => {
      localStorage.setItem('climate_advisor_selected_zone', 'zone-removed');
    });

    await page.goto('/');
    // The very first /status request carries the stale entry_id and 503s;
    // loadStatus()'s self-heal (Issue #812 fix) must clear it and retry
    // unscoped before the page ever settles into a visible, non-error state.
    await page.waitForSelector('#status-grid', { state: 'visible' });

    const row = page.locator('#zone-selector-row');
    await expect(row).toBeVisible();
    const buttons = row.locator('.zone-tab-btn');
    await expect(buttons).toHaveCount(2);
    // renderZoneSelector()'s existing validation (zones.some(...)) must
    // detect the stale entry_id and fall back to the first known zone,
    // exactly as it already does for the never-selected (null) case.
    await expect(buttons.nth(0)).toHaveClass(/active/);
    await expect(buttons.nth(1)).not.toHaveClass(/active/);

    // The fallback must also be re-persisted so the next load doesn't repeat
    // the same stale lookup.
    const stored = await page.evaluate(() => localStorage.getItem('climate_advisor_selected_zone'));
    expect(stored).toBe('zone-bedroom');
  });

  test.describe('Issue #813: real two-step bootstrap contract (no guessing, ever)', () => {

    test('first-ever visit, multi-zone: bootstrap response never carries status data, second request supplies it', async ({ page }) => {
      const zones = [
        { entry_id: 'zone-bedroom', title: 'Bedroom' },
        { entry_id: 'zone-living', title: 'Living Room' },
      ];
      let firstCallSeenWithNoEntryId = false;

      await page.route('**/api/climate_advisor/status**', (route) => {
        const url = new URL(route.request().url());
        const entryId = url.searchParams.get('entry_id');
        if (!entryId) {
          firstCallSeenWithNoEntryId = true;
          // Mirrors api.py's real bootstrap-only response exactly: no
          // coordinator-derived field is present at all — proving the
          // backend never resolved (guessed) a coordinator for this call.
          route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({ zone_selection_required: true, zones, zone_count: 2 }),
          });
          return;
        }
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
            zones,
            zone_count: 2,
          }),
        });
      });

      const scopedStatusRequest = page.waitForRequest((req) =>
        req.url().includes('/api/climate_advisor/status') && req.url().includes('entry_id=zone-bedroom')
      );
      await page.goto('/');
      await scopedStatusRequest;
      await page.waitForSelector('#status-grid', { state: 'visible' });

      expect(firstCallSeenWithNoEntryId).toBe(true);
      const buttons = page.locator('#zone-selector-row .zone-tab-btn');
      await expect(buttons).toHaveCount(2);
      await expect(buttons.nth(0)).toHaveClass(/active/);
      // The status grid must reflect the real (second-request) data, not be
      // stuck on the bootstrap response's absence of status fields.
      await expect(page.locator('#status-grid')).toContainText('active');
      const stored = await page.evaluate(() => localStorage.getItem('climate_advisor_selected_zone'));
      expect(stored).toBe('zone-bedroom');
    });

    test('stale stored entry_id chains through BOTH the 503 self-heal and the bootstrap requirement to a real result', async ({ page }) => {
      // Worst realistic case for the shared retry-depth counter: a stale
      // stored entry_id 503s first (self-heal #1: clear + retry unscoped),
      // and since the install is still multi-zone, the unscoped retry comes
      // back zone_selection_required (self-heal #2: pick a zone + retry
      // scoped) before the third call finally succeeds. Proves the counter
      // guard doesn't let the first correction swallow the second.
      const zones = [
        { entry_id: 'zone-bedroom', title: 'Bedroom' },
        { entry_id: 'zone-living', title: 'Living Room' },
      ];
      await page.route('**/api/climate_advisor/status**', (route) => {
        const url = new URL(route.request().url());
        const entryId = url.searchParams.get('entry_id');
        if (entryId === 'zone-removed') {
          route.fulfill({
            status: 503,
            contentType: 'application/json',
            body: JSON.stringify({ error: 'Climate Advisor not loaded' }),
          });
          return;
        }
        if (!entryId) {
          route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({ zone_selection_required: true, zones, zone_count: 2 }),
          });
          return;
        }
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
            zones,
            zone_count: 2,
          }),
        });
      });

      await page.addInitScript(() => {
        localStorage.setItem('climate_advisor_selected_zone', 'zone-removed');
      });

      await page.goto('/');
      await page.waitForSelector('#status-grid', { state: 'visible' });

      const buttons = page.locator('#zone-selector-row .zone-tab-btn');
      await expect(buttons).toHaveCount(2);
      await expect(buttons.nth(0)).toHaveClass(/active/);
      await expect(page.locator('#status-grid')).toContainText('active');
      const stored = await page.evaluate(() => localStorage.getItem('climate_advisor_selected_zone'));
      expect(stored).toBe('zone-bedroom');
    });

  });

});
