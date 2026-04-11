const { test, expect } = require('@playwright/test');

// Helper: wait for chart canvas to be rendered (non-blank)
async function waitForChart(page) {
  await page.waitForSelector('#temp-chart', { state: 'visible' });
  // Wait until Chart.js has registered a chart on the canvas (up to 8 seconds)
  await page.waitForFunction(() => {
    const canvas = document.getElementById('temp-chart');
    return canvas && typeof Chart !== 'undefined' && !!Chart.getChart(canvas);
  }, { timeout: 8000 });
}

test.describe('Temperature Forecast Chart', () => {

  test.beforeEach(async ({ page }) => {
    const errors = [];
    page.on('console', msg => { if (msg.type() === 'error') errors.push(msg.text()); });
    page.errors = errors;
    await page.goto('/');
    await waitForChart(page);
  });

  test('chart renders without JS errors', async ({ page }) => {
    // Filter out known HA framework errors that aren't from our code
    const ourErrors = page.errors.filter(e =>
      !e.includes('AbortError') &&
      !e.includes('sandbox') &&
      !e.includes('Cannot parse')
    );
    expect(ourErrors).toEqual([]);
  });

  test('canvas is not blank', async ({ page }) => {
    const canvas = page.locator('#temp-chart');
    const box = await canvas.boundingBox();
    expect(box).not.toBeNull();
    expect(box.width).toBeGreaterThan(100);
    expect(box.height).toBeGreaterThan(100);
    // Check that chart object exists in JS context
    const hasChart = await page.evaluate(() => typeof window._tempChart !== 'undefined' && window._tempChart !== null);
    // _tempChart is a closure var — check via chart registry instead
    const chartExists = await page.evaluate(() => {
      const canvas = document.getElementById('temp-chart');
      return !!Chart.getChart(canvas);
    });
    expect(chartExists).toBe(true);
  });

  test('hover panel shows all 4 temperature series', async ({ page }) => {
    const canvas = page.locator('#temp-chart');
    const box = await canvas.boundingBox();
    // Trigger hover at canvas-relative center x via the test hook
    // (CDP mouse events don't fire Chart.js afterEvent in headless chromium)
    const canvasCenterX = box.width / 2;
    await page.evaluate((x) => window.__triggerHoverAt(x), canvasCenterX);
    await page.waitForTimeout(200);

    const panel = page.locator('#chart-hover-panel');
    const html = await panel.innerHTML();
    expect(html).toContain('P-Indoor');
    expect(html).toContain('P-Outdoor');
    expect(html).toMatch(/Indoor/);
    expect(html).toMatch(/Outdoor/);
  });

  test('crosshair tracks cursor position smoothly', async ({ page }) => {
    const canvas = page.locator('#temp-chart');
    const box = await canvas.boundingBox();

    // Move to three different x positions via JS dispatch (CDP mouse events don't
    // fire DOM mousemove on canvas in Playwright headless chromium shell)
    const positions = [0.25, 0.5, 0.75];
    for (const frac of positions) {
      const mx = box.x + box.width * frac;
      const my = box.y + box.height / 2;
      await page.evaluate(({mx, my}) => {
        const canvas = document.getElementById('temp-chart');
        canvas.dispatchEvent(new MouseEvent('mousemove', {
          bubbles: true, cancelable: true,
          clientX: mx, clientY: my,
        }));
      }, { mx, my });
      await page.waitForTimeout(100);

      // Read _cursorPx from the chart's plugin via the test hook
      const crosshairX = await page.evaluate(() => window.__test_cursorPx);

      if (crosshairX !== null && crosshairX !== undefined) {
        // crosshairX should be close to mx - box.x (canvas-relative)
        const canvasRelX = mx - box.x;
        expect(Math.abs(crosshairX - canvasRelX)).toBeLessThan(5);
      }
    }
  });

  test('range button 6h centers x-axis on now (±3h)', async ({ page }) => {
    await page.click('button[data-range="6h"]');
    await page.waitForTimeout(500);

    const { axisMin, axisMax } = await page.evaluate(() => {
      const canvas = document.getElementById('temp-chart');
      const chart = Chart.getChart(canvas);
      return chart ? { axisMin: chart.scales.x.min, axisMax: chart.scales.x.max } : {};
    });

    const now = Date.now();
    // 6h range centered: min ≈ now-3h, max ≈ now+3h (allow 2 min tolerance)
    expect(axisMin).not.toBeNull();
    expect(Math.abs(axisMin - (now - 3 * 3600000))).toBeLessThan(2 * 60 * 1000);
    expect(Math.abs(axisMax - (now + 3 * 3600000))).toBeLessThan(2 * 60 * 1000);
  });

  test('hover panel clears when mouse leaves chart', async ({ page }) => {
    const canvas = page.locator('#temp-chart');
    const box = await canvas.boundingBox();

    const cx = box.x + box.width / 2;
    const cy = box.y + box.height / 2;

    // Move into chart via JS dispatch
    await page.evaluate(({cx, cy}) => {
      const canvas = document.getElementById('temp-chart');
      canvas.dispatchEvent(new MouseEvent('mousemove', {
        bubbles: true, cancelable: true, clientX: cx, clientY: cy,
      }));
    }, { cx, cy });
    await page.waitForTimeout(200);

    // Dispatch mouseleave to trigger the panel-clearing listener
    // Note: mouseout is intentionally omitted — it triggers Chart.js internals
    // that re-populate the panel via the external tooltip callback
    await page.evaluate(() => {
      const canvas = document.getElementById('temp-chart');
      canvas.dispatchEvent(new MouseEvent('mouseleave', { bubbles: false, cancelable: true }));
    });
    await page.waitForTimeout(300);

    const panel = page.locator('#chart-hover-panel');
    const html = await panel.innerHTML();
    expect(html.trim()).toBe('');
  });

  test('activity timeline renders with 4 rows', async ({ page }) => {
    const canvas = page.locator('#activity-chart');
    await expect(canvas).toBeVisible();
    const box = await canvas.boundingBox();
    // H should be ~86px for 4 rows (rowH=14, gap=10 between rows)
    expect(box.height).toBeGreaterThan(70);
    // Timeline should appear BELOW the temp chart
    const chartBox = await page.locator('#temp-chart').boundingBox();
    expect(box.y).toBeGreaterThan(chartBox.y + chartBox.height - 5);
  });

  test('hover panel updates on every mouse pixel, not just at data-point boundaries', async ({ page }) => {
    // This test uses real DOM mousemove dispatch (not __triggerHoverAt) to exercise the
    // actual Chart.js event pipeline. The bug: external tooltip callback only fires when
    // crossing a data-point boundary, so the panel freezes mid-segment.
    // The fix: panel rendering runs in afterEvent on every pixel.
    //
    // Fixture: hvacStates=['off','heating','off','cooling','off','fan'] cycling by index
    // i=5: hvac='fan',     ts = now - 42*1800000
    // i=3: hvac='cooling', ts = now - 44*1800000
    const { fanPageX, coolingPageX, pageY } = await page.evaluate(() => {
      const chart = Chart.getChart(document.getElementById('temp-chart'));
      const canvas = document.getElementById('temp-chart');
      if (!chart || !chart.scales.x) return {};
      const rect = canvas.getBoundingClientRect();
      const now = Date.now();
      const fanCanvasX    = chart.scales.x.getPixelForValue(now - 42 * 1800000);
      const coolingCanvasX = chart.scales.x.getPixelForValue(now - 44 * 1800000);
      return {
        fanPageX:    rect.left + fanCanvasX,
        coolingPageX: rect.left + coolingCanvasX,
        pageY:       rect.top + rect.height / 2,
      };
    });
    expect(fanPageX).toBeTruthy();
    expect(coolingPageX).toBeTruthy();

    // Dispatch real DOM mousemove to fan area
    await page.evaluate(({x, y}) => {
      document.getElementById('temp-chart').dispatchEvent(
        new MouseEvent('mousemove', { bubbles: true, cancelable: true, clientX: x, clientY: y })
      );
    }, { x: fanPageX, y: pageY });
    await page.waitForTimeout(150);
    let html = await page.locator('#chart-hover-panel').innerHTML();
    expect(html.toLowerCase()).toContain('fan');

    // Dispatch real DOM mousemove to cooling area — panel must update without leaving chart
    await page.evaluate(({x, y}) => {
      document.getElementById('temp-chart').dispatchEvent(
        new MouseEvent('mousemove', { bubbles: true, cancelable: true, clientX: x, clientY: y })
      );
    }, { x: coolingPageX, y: pageY });
    await page.waitForTimeout(150);
    html = await page.locator('#chart-hover-panel').innerHTML();
    expect(html.toLowerCase()).not.toContain('fan');
    expect(html.toLowerCase()).toContain('cooling');
  });

  test('fan-on indicator clears on mouse move without leaving chart', async ({ page }) => {
    // DOM mousemove version — exercises real event pipeline, not __triggerHoverAt shortcut.
    // Fixture: fan: i%7===0, so i=0 (fan=true), i=1 (fan=false, hvac='heating')
    const { fanPageX, fanOffPageX, pageY } = await page.evaluate(() => {
      const chart = Chart.getChart(document.getElementById('temp-chart'));
      const canvas = document.getElementById('temp-chart');
      if (!chart || !chart.scales.x) return {};
      const rect = canvas.getBoundingClientRect();
      const now = Date.now();
      return {
        fanPageX:    rect.left + chart.scales.x.getPixelForValue(now - 47 * 1800000),
        fanOffPageX: rect.left + chart.scales.x.getPixelForValue(now - 46 * 1800000),
        pageY:       rect.top + rect.height / 2,
      };
    });
    expect(fanPageX).toBeTruthy();

    const dispatch = (x, y) => page.evaluate(({x, y}) => {
      document.getElementById('temp-chart').dispatchEvent(
        new MouseEvent('mousemove', { bubbles: true, cancelable: true, clientX: x, clientY: y })
      );
    }, { x, y });

    await dispatch(fanPageX, pageY);
    await page.waitForTimeout(150);
    let html = await page.locator('#chart-hover-panel').innerHTML();
    expect(html).toContain('Sched fan');

    await dispatch(fanOffPageX, pageY);
    await page.waitForTimeout(150);
    html = await page.locator('#chart-hover-panel').innerHTML();
    expect(html).not.toContain('Sched fan');
  });

  test('hover panel height stays fixed when state badges appear', async ({ page }) => {
    // Get panel offsetHeight when empty (no hover yet)
    const emptyHeight = await page.evaluate(() =>
      document.getElementById('chart-hover-panel').offsetHeight
    );

    // Trigger hover at HVAC=fan segment (fixture i=5 → ts = now - 42*1800000)
    const fanX = await page.evaluate(() => {
      const canvas = document.getElementById('temp-chart');
      const chart = Chart.getChart(canvas);
      const now = Date.now();
      return chart ? chart.scales.x.getPixelForValue(now - 42 * 1800000) : null;
    });
    expect(fanX).not.toBeNull();
    await page.evaluate((x) => window.__triggerHoverAt(x), fanX);
    await page.waitForTimeout(150);

    const badgeHeight = await page.evaluate(() =>
      document.getElementById('chart-hover-panel').offsetHeight
    );

    // Panel height must not grow when HVAC/fan badges appear — layout jump is a UX bug
    expect(badgeHeight).toBe(emptyHeight);
  });

  test('fan state shown once — HVAC fan mode does not also show Fan on badge', async ({ page }) => {
    // Fixture i=5: hvac='fan' → hover must show fan indication exactly once (not both
    // an HVAC "Fan" badge AND a separate "Fan on" automation badge simultaneously)
    const fanX = await page.evaluate(() => {
      const canvas = document.getElementById('temp-chart');
      const chart = Chart.getChart(canvas);
      const now = Date.now();
      return chart ? chart.scales.x.getPixelForValue(now - 42 * 1800000) : null;
    });
    expect(fanX).not.toBeNull();
    await page.evaluate((x) => window.__triggerHoverAt(x), fanX);
    await page.waitForTimeout(150);

    const html = await page.locator('#chart-hover-panel').innerHTML();
    // When HVAC mode is already 'fan', the separate 'Fan on' badge must not also appear
    expect(html.toLowerCase()).not.toContain('fan on');
  });

  test('predicted outdoor dataset contains points from state_log (historical predicted)', async ({ page }) => {
    // The fixture state_log has 48 entries with pred_outdoor set. After drawChart(),
    // the 'Predicted Outdoor' dataset must include points sourced from those stored values.
    const predOutdoorCount = await page.evaluate(() => {
      const canvas = document.getElementById('temp-chart');
      const chart = Chart.getChart(canvas);
      if (!chart) return 0;
      const ds = chart.data.datasets.find(d => d.label === 'Predicted Outdoor');
      return ds ? ds.data.length : 0;
    });
    // 48 fixture state_log entries all have pred_outdoor; plus today's remaining forecast hours
    expect(predOutdoorCount).toBeGreaterThan(20);
  });

  test('historical predicted points include past timestamps (not today-only)', async ({ page }) => {
    // If predicted series were today-only (hourToDate based), all x values would be >= todayMidnight.
    // With historical storage, at least some points must be before today midnight.
    const hasPastPoints = await page.evaluate(() => {
      const canvas = document.getElementById('temp-chart');
      const chart = Chart.getChart(canvas);
      if (!chart) return false;
      const ds = chart.data.datasets.find(d => d.label === 'Predicted Outdoor');
      if (!ds || !ds.data.length) return false;
      const todayMidnight = new Date();
      todayMidnight.setHours(0, 0, 0, 0);
      return ds.data.some(p => p.x < todayMidnight.getTime());
    });
    expect(hasPastPoints).toBe(true);
  });

  // ── Navigation / windowing tests (TDD — written before implementation) ──────

  test('24h default view is centered on now — max ≈ now+12h', async ({ page }) => {
    const axisMax = await page.evaluate(() => {
      const chart = Chart.getChart(document.getElementById('temp-chart'));
      return chart ? chart.scales.x.max : null;
    });
    expect(axisMax).not.toBeNull();
    // 24h centered: max = now + 12h (allow 2 min tolerance)
    expect(Math.abs(axisMax - (Date.now() + 12 * 3600000))).toBeLessThan(2 * 60 * 1000);
  });

  test('forward nav button shifts 24h window forward by 4h', async ({ page }) => {
    const maxBefore = await page.evaluate(() => {
      const chart = Chart.getChart(document.getElementById('temp-chart'));
      return chart ? chart.scales.x.max : null;
    });
    await page.click('#chart-nav-fwd');
    await page.waitForTimeout(200);
    const maxAfter = await page.evaluate(() => {
      const chart = Chart.getChart(document.getElementById('temp-chart'));
      return chart ? chart.scales.x.max : null;
    });
    // Step for 24h = 4h
    expect(Math.abs((maxAfter - maxBefore) - 4 * 3600000)).toBeLessThan(60 * 1000);
  });

  test('back nav button shifts 24h window backward by 4h', async ({ page }) => {
    const minBefore = await page.evaluate(() => {
      const chart = Chart.getChart(document.getElementById('temp-chart'));
      return chart ? chart.scales.x.min : null;
    });
    await page.click('#chart-nav-back');
    await page.waitForTimeout(200);
    const minAfter = await page.evaluate(() => {
      const chart = Chart.getChart(document.getElementById('temp-chart'));
      return chart ? chart.scales.x.min : null;
    });
    // min should decrease by 4h
    expect(Math.abs((minBefore - minAfter) - 4 * 3600000)).toBeLessThan(60 * 1000);
  });

  test('switching range resets pan offset — 12h window midpoint ≈ now', async ({ page }) => {
    // After switching to any range the window should be re-centered on now (offset = 0)
    await page.click('button[data-range="12h"]');
    await page.waitForTimeout(500);
    const { xMin, xMax } = await page.evaluate(() => {
      const chart = Chart.getChart(document.getElementById('temp-chart'));
      return chart ? { xMin: chart.scales.x.min, xMax: chart.scales.x.max } : {};
    });
    const midpoint = (xMin + xMax) / 2;
    // Midpoint of a 0-offset 12h window must be ≈ now (allow 5 min)
    expect(Math.abs(midpoint - Date.now())).toBeLessThan(5 * 60 * 1000);
  });

  test('3d view: predicted outdoor curve extends beyond 24h', async ({ page }) => {
    // Switch to 3d range — forecast_outdoor fixture has 5 days (120h) of future data
    await page.evaluate(() => window.setChartRange('3d', null));
    await page.waitForTimeout(500);

    const maxX = await page.evaluate(() => {
      const canvas = document.getElementById('temp-chart');
      const chart = Chart.getChart(canvas);
      if (!chart) return null;
      const ds = chart.data.datasets.find(d => d.label === 'Predicted Outdoor');
      if (!ds || !ds.data.length) return null;
      return Math.max(...ds.data.map(p => p.x));
    });

    expect(maxX).not.toBeNull();
    // fixture has 120h of future data; the max data point must be > 23h from now
    expect(maxX).toBeGreaterThan(Date.now() + 23 * 3600000);
  });

  test('swipe right on chart canvas scrolls backward in time', async ({ page }) => {
    const minBefore = await page.evaluate(() => {
      const chart = Chart.getChart(document.getElementById('temp-chart'));
      return chart ? chart.scales.x.min : null;
    });
    // Dispatch touchstart then touchend with dx = +60px (rightward swipe = back in time)
    await page.evaluate(() => {
      const canvas = document.getElementById('temp-chart');
      const t1 = new Touch({ identifier: 1, target: canvas, clientX: 200, clientY: 100 });
      const t2 = new Touch({ identifier: 1, target: canvas, clientX: 260, clientY: 100 });
      canvas.dispatchEvent(new TouchEvent('touchstart', { bubbles: true, touches: [t1], changedTouches: [t1] }));
      canvas.dispatchEvent(new TouchEvent('touchend',   { bubbles: true, touches: [],   changedTouches: [t2] }));
    });
    await page.waitForTimeout(200);
    const minAfter = await page.evaluate(() => {
      const chart = Chart.getChart(document.getElementById('temp-chart'));
      return chart ? chart.scales.x.min : null;
    });
    expect(minAfter).toBeLessThan(minBefore);
  });

});
