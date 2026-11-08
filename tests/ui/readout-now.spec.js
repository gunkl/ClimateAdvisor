const { test, expect } = require('@playwright/test');

// Issue #1408: with the pointer off the temperature chart, the readout below it is never blank. It shows what a hover
// at NOW would (indoor per sensor, predictions from the primary and the comparison engine, overlays, the target band,
// the thermostat setting and the plan step), labelled "Now · <time>", with a small hint that keeps hover and pin
// discoverable. Over the chart it follows the hover; on leaving it returns to NOW; a pin wins over both. When NOW is
// outside the view (scrolled into history) it shows the view's right edge, labelled "<day> <time> (end of view)".

const PAGES = [
  { name: 'classic', url: '/' },
  { name: 'new dashboard', url: '/new_dashboard.html' },
];
const VIEWPORTS = [
  { name: 'desktop', size: { width: 1280, height: 900 } },
  { name: 'phone', size: { width: 375, height: 812 } },
];

const engines = {
  primary: 'v4', shadow: 'v3', fallback: false, fitting: false, primary_state: 'ready', text: '',
  engines: [
    { id: 'v3', label: 'v3', running: true, primary: false, shadow: true, usable: true, confidence: 'high' },
    { id: 'v4', label: 'v4', running: true, primary: true, shadow: false, usable: true, confidence: 'ok' },
  ],
};

function v4Block(now) {
  const iso = (ms) => new Date(ms).toISOString();
  const half = 1800000;
  const past = Array.from({ length: 48 }, (_, i) => ({ ts: iso(now - (48 - i) * half), temp: 68 + (i % 5) * 0.2 }));
  const future = Array.from({ length: 48 }, (_, i) => ({ ts: iso(now + i * half), temp: 69 + (i % 7) * 0.2 }));
  return {
    ready: true, reason: 'ok', scores: null, model: { confidence: 'ok', fitted_at: iso(now - 9 * 3600000) },
    leads: ['1h', '4h', '12h', '24h'], sensors: ['_ca'],
    past: { _ca: { '1h': past, '4h': past, '12h': past, '24h': past } },
    future: { _ca: future }, future_at: iso(now), baseline: { _ca: future }, breaks: { _ca: [] },
    corrected_outdoor: { past: [], future: [] },
  };
}

// The mock chart_data plus v4 (primary) / v3 (comparison), the band, humidity, feels-like, the target and the
// commanded thermostat setting. `bump` shifts every indoor reading (a later data refresh).
function enrich(body, now, bump) {
  const iso = (ms) => new Date(ms).toISOString();
  body.prediction_engines = engines;
  body.v4 = v4Block(now);
  body.actual_indoor = body.actual_indoor.map(p => Object.assign({}, p, { temp: p.temp + bump }));
  body.state_log = body.state_log.map(e => Object.assign({}, e, { indoor: e.indoor + bump, upper: 74, lower: 68, humidity: 45, feelslike: 70 }));
  body.target_band = Array.from({ length: 24 }, (_, i) => ({ ts: iso(now + (i + 1) * 3600000), upper: 74, lower: 68 }));
  body.effective_target_history = Array.from({ length: 48 }, (_, i) => ({ ts: iso(now - (47 - i) * 1800000), target: 70 }));
  body.effective_target_forecast = Array.from({ length: 24 }, (_, i) => ({ ts: iso(now + (i + 1) * 3600000), target: 70 }));
  body.commanded_setpoint_history = Array.from({ length: 48 }, (_, i) => ({ ts: iso(now - (47 - i) * 1800000), target: 71 }));
  return body;
}

async function open(page, url, opts) {
  const o = opts || {};
  const state = { bump: 0, loads: 0 };
  await page.route('**/api/climate_advisor/chart_data**', async (route) => {
    const resp = await route.fetch();
    const body = enrich(await resp.json(), Date.now(), state.bump);
    state.loads += 1;
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
  // a plan step 10 minutes ahead, so the readout at NOW names it
  await page.route('**/api/climate_advisor/timeline**', async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    body.steps.push({ ts: new Date(Date.now() + 10 * 60000).toISOString(), actor: 'ca', kind: 'precool_start',
      text: 'CA starts cooling.', text_short: 'Starts cooling to 72°F.', confidence: 'ok', lead_minutes: 0, source: 'test',
      meta: { id: 'soon', status: 'planned', facts: {} } });
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
  await page.goto(url);
  await page.waitForFunction(() => {
    const c = document.getElementById('temp-chart');
    const p = document.getElementById('chart-hover-panel');
    return c && typeof Chart !== 'undefined' && !!Chart.getChart(c) && !!Chart.getChart(c).scales.x
      && p && p.dataset.mode === 'rest' && /Starts cooling/.test(p.innerHTML) && /Thermostat set/.test(p.innerHTML);
  }, { timeout: 8000 });
  if (o.allOn) {
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
  }
  return state;
}

// A refresh can leave a request in flight when a test ends; let it go quietly.
test.afterEach(async ({ page }) => { await page.unrouteAll({ behavior: 'ignoreErrors' }); });

const readout = (page) => page.evaluate(() => {
  const p = document.getElementById('chart-hover-panel');
  const t = p.querySelector('.chp-time');
  const rows = {};
  p.querySelectorAll('.chp-grid .chp-row').forEach(r => {
    const l = r.querySelector('.chp-row-label');
    rows[l ? l.textContent : 'State'] = [...r.querySelectorAll('.chp-item')].map(e => e.textContent);
  });
  return {
    mode: p.dataset.mode, time: t ? t.textContent : '', hint: p.querySelector('.chp-hint') ? p.querySelector('.chp-hint').textContent : '',
    rows, names: [...p.querySelectorAll('.chp-grid .chp-item')].map(e => e.textContent.split(':')[0].trim()),
    height: p.offsetHeight, html: p.innerHTML,
    pageScrollW: document.documentElement.scrollWidth, vw: window.innerWidth,
  };
});

const xFor = (page, frac) => page.evaluate((f) => {
  const c = document.getElementById('temp-chart');
  const xs = Chart.getChart(c).scales.x;
  const r = c.getBoundingClientRect();
  return { x: r.left + xs.left + (xs.right - xs.left) * f, y: r.top + r.height / 2 };
}, frac);

const leave = async (page) => {
  await page.mouse.move(1, 1);
  await page.evaluate(() => document.getElementById('chart-stack').dispatchEvent(new MouseEvent('mouseleave')));
  await page.waitForTimeout(100);
};

const NOW_LABEL = /^Now · \d{1,2}:\d{2}\s?[AP]M/;

for (const pg of PAGES) {
  for (const vp of VIEWPORTS) {
    test.describe(`readout rests on NOW: ${pg.name}, ${vp.name}`, () => {
      test.use({ viewport: vp.size });

      test('filled on load with no hover: the rows a hover at NOW shows, labelled "Now ·"', async ({ page }) => {
        await open(page, pg.url);
        const r = await readout(page);
        expect(r.mode).toBe('rest');
        expect(r.time).toMatch(NOW_LABEL);
        expect(r.hint).toContain('click to pin');
        expect(Object.keys(r.rows)).toEqual(expect.arrayContaining(['Indoor', 'Predicted', 'Outdoor', 'Overlays']));
        const pred = r.rows.Predicted.join(' | ');
        expect(pred).toMatch(/v4/);   // the primary engine
        expect(pred).toMatch(/v3/);   // the comparison engine
        const ov = r.rows.Overlays.join(' | ');
        expect(ov).toMatch(/Band[^:]*: 68–74°/);
        expect(ov).toContain('Target: 70°');
        expect(ov).toContain('Thermostat set: 71°');
        expect(ov).toMatch(/CA \d{1,2}:\d{2}\s?[AP]M: Starts cooling to 72°F\./);
        expect(r.pageScrollW).toBeLessThanOrEqual(r.vw + 1);
        // the same row builder as a hover at NOW
        await page.evaluate(() => window.__triggerHoverAt(Chart.getChart(document.getElementById('temp-chart')).scales.x.getPixelForValue(Date.now())));
        const h = await readout(page);
        expect(h.mode).toBe('hover');
        expect(h.names).toEqual(r.names);
      });

      test('every line on: hover follows the pointer, leaving returns to NOW, the height never drops', async ({ page }) => {
        await open(page, pg.url, { allOn: true });
        await page.locator('#temp-chart').scrollIntoViewIfNeeded();
        const rest = await readout(page);
        expect(rest.mode).toBe('rest');
        expect(rest.rows.Overlays.join(' | ')).toContain('Target: 70');
        expect(rest.rows.Overlays.join(' | ')).not.toContain('Humidity');   // Issue #1414: the humidity line starts off
        const chartTop = await page.evaluate(() => document.getElementById('temp-chart').getBoundingClientRect().top + window.scrollY);
        let tallest = rest.height;
        for (const f of [0.15, 0.5, 0.85]) {
          const { x, y } = await xFor(page, f);
          await page.mouse.move(x, y);
          await page.waitForTimeout(80);
          const r = await readout(page);
          expect(r.mode).toBe('hover');
          expect(r.time).not.toMatch(/^Now/);
          expect(r.height).toBeGreaterThanOrEqual(tallest);
          tallest = Math.max(tallest, r.height);
        }
        await leave(page);
        const back = await readout(page);
        expect(back.mode).toBe('rest');
        expect(back.time).toMatch(NOW_LABEL);
        expect(back.names).toEqual(rest.names);
        expect(back.height).toBe(tallest);   // no jump on leave
        const nowTop = await page.evaluate(() => document.getElementById('temp-chart').getBoundingClientRect().top + window.scrollY);
        expect(Math.abs(nowTop - chartTop)).toBeLessThan(0.5);
      });

      test('a pin wins over hover and NOW; release goes back to NOW', async ({ page }) => {
        await open(page, pg.url);
        await page.locator('#temp-chart').scrollIntoViewIfNeeded();
        const a = await xFor(page, 0.3);
        await page.mouse.move(a.x, a.y);
        await page.mouse.click(a.x, a.y);
        await page.waitForTimeout(100);
        const pinned = await readout(page);
        expect(pinned.mode).toBe('pinned');
        expect(pinned.html).toContain('to release');
        await leave(page);
        expect((await readout(page)).html).toBe(pinned.html);              // off the chart: still pinned
        await page.evaluate(() => CAChartV2.redraw());
        await page.waitForTimeout(100);
        expect((await readout(page)).mode).toBe('pinned');                 // a redraw keeps the pin
        await page.locator('#chart-hover-panel .chp-clear').click();       // release from the readout (pointer off the chart)
        await page.waitForTimeout(100);
        const back = await readout(page);
        expect(back.mode).toBe('rest');
        expect(back.time).toMatch(NOW_LABEL);
        // pin, then Esc: back to NOW too (the button click may have scrolled the page)
        await page.locator('#temp-chart').scrollIntoViewIfNeeded();
        const b = await xFor(page, 0.3);
        await page.mouse.click(b.x, b.y);
        await page.waitForTimeout(100);
        expect((await readout(page)).mode).toBe('pinned');
        await page.keyboard.press('Escape');
        await page.waitForTimeout(100);
        expect((await readout(page)).time).toMatch(NOW_LABEL);
      });

      test('scrolled into history: the right edge of the view, labelled with its time', async ({ page }) => {
        await open(page, pg.url);
        // scroll back until NOW is well off the right edge (a view ending exactly at NOW still reads "Now")
        for (let i = 0; i < 4; i++) {
          if ((await page.evaluate(() => window.__chartState().win.max - Date.now())) < -5 * 60000) break;
          await page.evaluate(() => window.shiftChartWindow(-1));
          await page.waitForTimeout(250);
        }
        await page.evaluate(() => CAChartV2.redraw());
        const st = await page.evaluate(() => window.__chartState());
        expect(st.win.max).toBeLessThan(Date.now() - 5 * 60000);
        // the label is the view's right edge, "<weekday> <time> (end of view)"; the live window drifts with the clock,
        // so either side of a minute boundary is accepted
        const want = await page.evaluate((ms) => [ms - 60000, ms, ms + 60000].map(t => new Date(t).toLocaleDateString([], { weekday: 'short' }) + ' ' +
          new Date(t).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) + ' (end of view)'), st.win.max);
        const r = await readout(page);
        expect(r.mode).toBe('rest');
        expect(want.some(w => r.time.startsWith(w))).toBe(true);
        expect(r.hint).toContain('click to pin');
        expect(r.rows.Indoor).toBeTruthy();
        // back to the present: NOW again
        await page.evaluate(() => window.chartGoNow());
        await page.waitForFunction(() => /^Now · /.test(document.querySelector('#chart-hover-panel .chp-time').textContent), { timeout: 4000 });
      });

      test('a data refresh updates the NOW values', async ({ page }) => {
        const state = await open(page, pg.url);
        const before = (await readout(page)).rows.Indoor.join(' | ');
        state.bump = 5;
        await page.evaluate(() => CAChartV2.load());
        await expect.poll(async () => (await readout(page)).rows.Indoor.join(' | ')).not.toBe(before);
        const after = await readout(page);
        expect(after.mode).toBe('rest');
        expect(after.time).toMatch(NOW_LABEL);
        const num = (s) => parseFloat(/CA reading: ([\d.]+)/.exec(s)[1]);
        expect(num(after.rows.Indoor.join(' | ')) - num(before)).toBeCloseTo(5, 0);
      });
    });
  }

  test.describe(`readout on a touch-only phone: ${pg.name}`, () => {
    test.use({ viewport: { width: 375, height: 812 }, hasTouch: true, isMobile: true });

    test('NOW until tapped, the tap pins, tapping again goes back to NOW', async ({ page }) => {
      await open(page, pg.url);
      let r = await readout(page);
      expect(r.mode).toBe('rest');
      expect(r.time).toMatch(NOW_LABEL);
      expect(r.hint).toBe('Tap the chart to pin a time');
      expect(r.pageScrollW).toBeLessThanOrEqual(r.vw + 1);
      const canvas = page.locator('#temp-chart');
      await canvas.scrollIntoViewIfNeeded();
      const box = await canvas.boundingBox();
      await page.touchscreen.tap(box.x + box.width * 0.3, box.y + box.height / 2);
      await page.waitForTimeout(200);
      r = await readout(page);
      expect(r.mode).toBe('pinned');
      expect(r.html).toContain('tap to release');
      await page.touchscreen.tap(box.x + box.width * 0.3, box.y + box.height / 2);
      await page.waitForTimeout(200);
      r = await readout(page);
      expect(r.mode).toBe('rest');
      expect(r.time).toMatch(NOW_LABEL);
    });
  });
}
