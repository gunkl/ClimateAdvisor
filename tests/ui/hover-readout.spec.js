const { test, expect } = require('@playwright/test');

// The chart's hover readout (#chart-hover-panel: Indoor / Predicted / Outdoor / Overlays at the hovered time) sits
// BELOW the chart stack on both dashboards. Above the chart it reserved a 104px block that was an empty gap whenever
// nothing was hovered. Issue #1408: off the chart it now shows the values at NOW ("Now · <time>", a small hover/pin
// hint); hovered or pinned it follows the pointer, and because it is below the chart its size never moves the chart.

const PAGES = [
  { name: 'classic', url: '/' },
  { name: 'new dashboard', url: '/new_dashboard.html' },
];
const VIEWPORTS = [
  { name: 'desktop', size: { width: 1280, height: 900 } },
  { name: 'phone', size: { width: 375, height: 812 } },
];

async function openChart(page, url) {
  await page.goto(url);
  await page.waitForFunction(() => {
    const c = document.getElementById('temp-chart');
    return c && typeof Chart !== 'undefined' && !!Chart.getChart(c) && !!Chart.getChart(c).scales.x;
  }, { timeout: 8000 });
}

const geom = (page) => page.evaluate(() => {
  const r = (id) => document.getElementById(id).getBoundingClientRect();
  const panel = document.getElementById('chart-hover-panel');
  return {
    stackBottom: r('chart-stack').bottom,
    canvasBottom: Math.max(r('temp-chart').bottom, r('out-chart').bottom, r('activity-chart').bottom),
    tempTop: r('temp-chart').top + window.scrollY,
    panelTop: r('chart-hover-panel').top,
    panelHeight: panel.offsetHeight,
    html: panel.innerHTML,
    mode: panel.dataset.mode || '',
    time: panel.querySelector('.chp-time') ? panel.querySelector('.chp-time').textContent : '',
  };
});

for (const pg of PAGES) {
  for (const vp of VIEWPORTS) {
    test.describe(`hover readout below the chart: ${pg.name}, ${vp.name}`, () => {
      test.use({ viewport: vp.size });

      test('sits after the canvases, shows NOW idle, and follows a hover without moving the chart', async ({ page }) => {
        await openChart(page, pg.url);
        const idle = await geom(page);
        expect(idle.mode).toBe('rest');                       // Issue #1408: the values at NOW, not an empty hint
        expect(idle.time).toMatch(/^Now · /);
        expect(idle.html).toContain('Indoor');
        expect(idle.panelTop).toBeGreaterThanOrEqual(idle.canvasBottom - 0.5);
        expect(idle.panelTop).toBeGreaterThanOrEqual(idle.stackBottom - 0.5);

        const w = await page.evaluate(() => document.getElementById('temp-chart').getBoundingClientRect().width);
        await page.evaluate((x) => window.__triggerHoverAt(x), w / 2);
        await page.waitForTimeout(150);
        const hov = await geom(page);
        expect(hov.mode).toBe('hover');
        expect(hov.html).toContain('Indoor');
        expect(hov.panelHeight).toBeGreaterThanOrEqual(idle.panelHeight);   // never shrinks between NOW and a hover
        expect(hov.panelTop).toBeGreaterThanOrEqual(hov.canvasBottom - 0.5);
        expect(Math.abs(hov.tempTop - idle.tempTop)).toBeLessThan(0.5);   // the chart does not jump

        await page.evaluate(() => document.getElementById('chart-stack').dispatchEvent(new MouseEvent('mouseleave')));
        await page.waitForTimeout(100);
        const back = await geom(page);
        expect(back.mode).toBe('rest');
        expect(back.time).toMatch(/^Now · /);
        expect(back.panelHeight).toBe(hov.panelHeight);       // the tallest height is kept, so leaving does not jump
      });
    });
  }

  test.describe(`hover readout touch pin: ${pg.name}, phone`, () => {
    test.use({ viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true });

    test('a tap pins the readout below the chart; tapping the same spot again goes back to NOW', async ({ page }) => {
      await openChart(page, pg.url);
      const canvas = page.locator('#temp-chart');
      await canvas.scrollIntoViewIfNeeded();
      const box = await canvas.boundingBox();
      await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
      await page.waitForTimeout(200);
      const pinned = await geom(page);
      expect(pinned.html).toContain('Pinned');
      expect(pinned.html).toContain('Indoor');
      expect(pinned.panelTop).toBeGreaterThanOrEqual(pinned.canvasBottom - 0.5);
      await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
      await page.waitForTimeout(200);
      const released = await geom(page);
      expect(released.mode).toBe('rest');
      expect(released.time).toMatch(/^Now · /);
    });
  });
}

// Owner bug: with many lines on, the readout had a fixed height and a scroll bar that could not be used, because moving
// the mouse to the scroll bar changed the readout. It now grows to fit every row (no scroll bar), holds its tallest
// height (also across NOW and a hover, Issue #1408), and a click pins it so the values can be read.

async function allLinesOn(page) {
  for (let i = 0; i < 60; i++) {
    const clicked = await page.evaluate(() => {
      const chip = [...document.querySelectorAll('#chart-key-body [data-series]')]
        .find(c => !c.disabled && c.getAttribute('aria-pressed') === 'false');
      if (!chip) return false;
      chip.click();
      return true;
    });
    if (!clicked) break;
  }
  await page.waitForTimeout(150);
  return page.evaluate(() => [...document.querySelectorAll('#chart-key-body [data-series]')]
    .filter(c => !c.disabled && c.getAttribute('aria-pressed') === 'false').length);
}

const fit = (page) => page.evaluate(() => {
  const p = document.getElementById('chart-hover-panel');
  const pr = p.getBoundingClientRect();
  const st = getComputedStyle(p);
  const rows = [...p.querySelectorAll('.chp-row, .chp-time')].map(r => r.getBoundingClientRect());
  return {
    scrollH: p.scrollHeight, clientH: p.clientHeight, overflowY: st.overflowY, height: p.offsetHeight,
    rows: rows.length,
    allInside: rows.every(r => r.height > 0 && r.top >= pr.top - 0.5 && r.bottom <= pr.bottom + 0.5 && r.right <= pr.right + 0.5),
    pageScrollW: document.documentElement.scrollWidth, vw: window.innerWidth,
    html: p.innerHTML,
  };
});

const xFor = (page, frac) => page.evaluate((f) => {
  const c = document.getElementById('temp-chart');
  const xs = Chart.getChart(c).scales.x;
  const r = c.getBoundingClientRect();
  return { x: r.left + xs.left + (xs.right - xs.left) * f, y: r.top + r.height / 2 };
}, frac);

for (const pg of PAGES) {
  for (const vp of VIEWPORTS) {
    test.describe(`readout auto-sizes and pins: ${pg.name}, ${vp.name}`, () => {
      test.use({ viewport: vp.size });

      test('every line on: no scroll bar, every row visible, height held while over the chart', async ({ page }) => {
        await openChart(page, pg.url);
        expect(await allLinesOn(page)).toBe(0);
        await page.locator('#temp-chart').scrollIntoViewIfNeeded();
        const chartTop = await page.evaluate(() => document.getElementById('temp-chart').getBoundingClientRect().top + window.scrollY);
        let tallest = 0;
        for (const f of [0.15, 0.35, 0.5, 0.65, 0.85]) {
          const { x, y } = await xFor(page, f);
          await page.mouse.move(x, y);
          await page.waitForTimeout(80);
          const r = await fit(page);
          expect(r.rows).toBeGreaterThan(1);
          expect(r.overflowY).toBe('visible');
          expect(r.scrollH).toBeLessThanOrEqual(r.clientH);
          expect(r.allInside).toBe(true);
          expect(r.pageScrollW).toBeLessThanOrEqual(r.vw + 1);
          expect(r.height).toBeGreaterThanOrEqual(tallest);   // never shrinks while the pointer is over the chart
          tallest = Math.max(tallest, r.height);
        }
        const nowTop = await page.evaluate(() => document.getElementById('temp-chart').getBoundingClientRect().top + window.scrollY);
        expect(Math.abs(nowTop - chartTop)).toBeLessThan(0.5);
        // pointer leaves: the readout goes back to NOW and keeps its height (no jump, Issue #1408)
        await page.mouse.move(2, 2);
        await page.evaluate(() => document.getElementById('chart-stack').dispatchEvent(new MouseEvent('mouseleave')));
        await page.waitForTimeout(100);
        const idle = await fit(page);
        expect(idle.html).toMatch(/^<div class="chp-time">Now · /);
        expect(idle.height).toBe(tallest);
        expect(idle.scrollH).toBeLessThanOrEqual(idle.clientH);
      });

      test('a click pins the readout; mouse moves do not change it; a drag does not pin; click or Esc releases', async ({ page }) => {
        await openChart(page, pg.url);
        await allLinesOn(page);
        await page.locator('#temp-chart').scrollIntoViewIfNeeded();
        const a = await xFor(page, 0.4);
        await page.mouse.move(a.x, a.y);
        await page.mouse.click(a.x, a.y);
        await page.waitForTimeout(100);
        const pinned = (await fit(page)).html;
        expect(pinned).toContain('Pinned');
        expect(pinned).toContain('to release');
        for (const f of [0.1, 0.7, 0.9]) {
          const p = await xFor(page, f);
          await page.mouse.move(p.x, p.y, { steps: 4 });
        }
        await page.waitForTimeout(100);
        const still = await fit(page);
        expect(still.html).toBe(pinned);
        expect(still.scrollH).toBeLessThanOrEqual(still.clientH);
        // another click releases (the readout follows the pointer again, unpinned)
        const b = await xFor(page, 0.7);
        await page.mouse.click(b.x, b.y);
        await page.waitForTimeout(100);
        expect((await fit(page)).html).not.toContain('Pinned');
        // a drag (the zoom select) is not a click
        await page.mouse.move(b.x - 60, b.y);
        await page.mouse.down();
        await page.mouse.move(b.x + 60, b.y, { steps: 6 });
        await page.mouse.up();
        await page.waitForTimeout(150);
        expect((await fit(page)).html).not.toContain('Pinned');
        // pin again, then Esc releases
        const c = await xFor(page, 0.5);
        await page.mouse.click(c.x, c.y);
        await page.waitForTimeout(100);
        expect((await fit(page)).html).toContain('Pinned');
        await page.keyboard.press('Escape');
        await page.waitForTimeout(100);
        // released: back to resting, at NOW, or at the view's right edge when the drag above zoomed NOW out of view
        expect((await fit(page)).html).toMatch(/^<div class="chp-time">(Now · |[^<]* \(end of view\))/);
      });
    });
  }
}
