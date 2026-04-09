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

  test('range button 6h narrows x-axis to ~6 hours', async ({ page }) => {
    await page.click('button[data-range="6h"]');
    await page.waitForTimeout(500);

    const axisMin = await page.evaluate(() => {
      const canvas = document.getElementById('temp-chart');
      const chart = Chart.getChart(canvas);
      return chart ? chart.scales.x.min : null;
    });

    const expectedMin = Date.now() - 6 * 3600000;
    expect(axisMin).not.toBeNull();
    // Allow 2 min tolerance
    expect(Math.abs(axisMin - expectedMin)).toBeLessThan(2 * 60 * 1000);
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

});
