const { test, expect } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

// Issue #1430: the Status chart key's Predicted rows follow the engine roles chosen in Settings.
// Occupant view: with v4 as the main prediction and v3 as the comparison, the key listed v3 first on a "Today" row and
// v4 below it on "v4 with plan" / "v4 no plan", so it read as if v3 were the main engine; and a sensor v4 has only
// past (backtest) predictions for had a greyed "(history only)" no-plan chip that could never draw anything.
// Now: a "Main · <primary>" block first, then "Comparison · <shadow>", and a history-only sensor has one chip on the
// main engine's "Past predictions only" row. The lines (series ids, colours, dashes) are unchanged.
//
// Data: the live 0.9.5 Zone 1 shape (tests/fixtures/chart_v2_zone1_live_095.json): v4 primary, v3 comparison; v4 lines
// for "_ca" (forecast + no-plan) and the thermostat's finer sensor (past only, from the backtest).

const LIVE = JSON.parse(fs.readFileSync(path.join(__dirname, '../fixtures/chart_v2_zone1_live_095.json'), 'utf8'));
const FINER = 'sensor.santa_maria_hallway_current_temperature';
const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

function shifted(deltaMs) {
  const walk = (o) => {
    if (typeof o === 'string' && ISO.test(o)) { const t = Date.parse(o); return isNaN(t) ? o : new Date(t + deltaMs).toISOString(); }
    if (Array.isArray(o)) return o.map(walk);
    if (o && typeof o === 'object') { const r = {}; Object.keys(o).forEach(k => { r[k] = walk(o[k]); }); return r; }
    return o;
  };
  return walk(LIVE.chart_data);
}

// roles: { primary, shadow } overrides the capture's v4 primary / v3 comparison.
function route(roles) {
  return async (r) => {
    const body = shifted(Date.now() - Date.parse(LIVE.now));
    if (roles) {
      const pe = body.prediction_engines;
      pe.primary = roles.primary;
      pe.shadow = roles.shadow;
      pe.engines.forEach(e => { e.primary = e.id === roles.primary; e.shadow = e.id === roles.shadow; });
      pe.control = Object.assign({}, pe.control, { engine: roles.primary, source: roles.primary });
      if (roles.mutate) roles.mutate(body);
    }
    await r.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  };
}

async function open(page, url, roles) {
  await page.route('**/api/climate_advisor/chart_data**', route(roles));
  await page.goto(url);
  await page.waitForFunction(() => document.getElementById('temp-chart') && typeof Chart !== 'undefined'
    && !!Chart.getChart(document.getElementById('temp-chart')), { timeout: 8000 });
  await expect(page.locator('#chart-key-body [data-series="ind:_ca"]')).toHaveCount(1);
  if (!(await page.evaluate(() => document.getElementById('chart-key').open))) await page.locator('#chart-key > summary').click();
}

// The Predicted group in key order: each role block's heading, its rows' labels and the series ids on each row.
const predictedLayout = (page) => page.evaluate(() => {
  const g = document.querySelector('#chart-key-body [aria-label="Prediction lines"]');
  return [...g.querySelectorAll('.key-role')].map(b => ({
    id: b.id || b.dataset.role, engine: b.dataset.engine, head: b.querySelector('.key-role-label').textContent.trim(),
    rows: [...b.querySelectorAll(':scope > .key-row')].map(r => ({
      label: (r.querySelector('.key-sub') || {}).textContent,
      ids: [...r.querySelectorAll('[data-series]')].map(c => c.dataset.series),
    })),
  }));
});

const SERIES_IDS = ['v3', 'v4:_ca', 'base:_ca', 'v4:' + FINER];

for (const url of ['/', '/new_dashboard.html']) {
  test.describe(`chart key follows the engine roles (Issue #1430) (${url})`, () => {
    test('v4 main, v3 comparison: the Main block is v4 and comes first; a history-only sensor has one chip', async ({ page }) => {
      const errors = [];
      page.on('pageerror', (e) => errors.push(String(e)));
      await open(page, url);
      await expect(page.locator('#chart-main-model')).toContainText('Main prediction: v4');
      await expect(page.locator('#chart-main-model')).toContainText('Comparison: v3');
      const L = await predictedLayout(page);
      expect(L.map(b => [b.id, b.engine, b.head])).toEqual([
        ['chart-role-main', 'v4', 'Main · v4'],
        ['chart-role-comparison', 'v3', 'Comparison · v3'],
      ]);
      const main = L[0].rows.filter(r => r.ids.length);
      expect(main).toEqual([
        { label: 'Forecast with plan', ids: ['v4:_ca'] },
        { label: 'Forecast if Climate Advisor did nothing', ids: ['base:_ca'] },
        { label: 'Past predictions only', ids: ['v4:' + FINER] },
      ]);
      // the past-lines row and the "still learning" line sit in the v4 block
      await expect(page.locator('#chart-role-main #chart-lead-row')).toHaveCount(1);
      await expect(page.locator('#chart-role-main #chart-v4-learning')).toHaveText('v4 is still learning: thermostat, bedroom sensor');
      expect(L[1].rows).toEqual([{ label: 'Prediction', ids: ['v3'] }]);
      // the history-only sensor: exactly one chip, no dead no-plan chip, no "(history only)" suffix
      await expect(page.locator('#chart-key-body [aria-label="Prediction lines"] [data-series$="' + FINER + '"]')).toHaveCount(1);
      await expect(page.locator('#chart-key-body [data-series="base:' + FINER + '"]')).toHaveCount(0);
      await expect(page.locator('#chart-key-body')).not.toContainText('history only');
      await expect(page.locator('#chart-key-body')).not.toContainText("Today's prediction");
      // accessible names say what each line is and the engine's role
      await expect(page.locator('[data-series="v3"]')).toHaveAttribute('aria-label', /^Climate Advisor reading, v3 prediction \(comparison\)/);
      await expect(page.locator('[data-series="v4:_ca"]')).toHaveAttribute('aria-label', /^Climate Advisor reading, v4 forecast with plan \(main prediction\)/);
      await expect(page.locator('[data-series="base:_ca"]')).toHaveAttribute('aria-label', /^Climate Advisor reading, v4 forecast if Climate Advisor did nothing \(main prediction\)/);
      await expect(page.locator('[data-series="v4:' + FINER + '"]')).toHaveAttribute('aria-label', /, v4 past predictions only \(main prediction\)/);
      await expect(page.locator('#chart-role-main')).toHaveAttribute('aria-label', 'Main prediction: v4');
      expect(errors).toEqual([]);
    });

    test('v3 main, v4 comparison: the blocks swap', async ({ page }) => {
      await open(page, url, { primary: 'v3', shadow: 'v4' });
      await expect(page.locator('#chart-main-model')).toContainText('Main prediction: v3');
      const L = await predictedLayout(page);
      expect(L.map(b => [b.id, b.engine, b.head])).toEqual([
        ['chart-role-main', 'v3', 'Main · v3'],
        ['chart-role-comparison', 'v4', 'Comparison · v4'],
      ]);
      expect(L[0].rows).toEqual([{ label: 'Prediction', ids: ['v3'] }]);
      expect(L[1].rows.filter(r => r.ids.length).map(r => r.label)).toEqual(
        ['Forecast with plan', 'Forecast if Climate Advisor did nothing', 'Past predictions only']);
      await expect(page.locator('[data-series="v3"]')).toHaveAttribute('aria-label', /\(main prediction\)/);
      await expect(page.locator('[data-series="v4:_ca"]')).toHaveAttribute('aria-label', /\(comparison\)/);
    });

    test('no comparison engine: no Comparison block (v4 lines stay in the key, not selected)', async ({ page }) => {
      await open(page, url, { primary: 'v3', shadow: 'none' });
      await expect(page.locator('#chart-main-model')).toContainText('No comparison engine');
      await expect(page.locator('#chart-role-comparison')).toHaveCount(0);
      const L = await predictedLayout(page);
      expect(L.map(b => [b.id, b.head])).toEqual([['chart-role-main', 'Main · v3'], ['other', 'Not selected · v4']]);
    });

    test('while the main engine is learning the schedule estimate sits in the Main block', async ({ page }) => {
      const when = (m) => new Date(Date.now() + m * 60000).toISOString();
      await open(page, url, { primary: 'v4', shadow: 'v3', mutate: (b) => {
        Object.assign(b.prediction_engines, { fallback: true, primary_state: 'learning', text: 'Prediction: learning' });
        b.schedule_estimate = [1, 2, 3, 4].map((i) => ({ ts: when(i * 60), temp: 69 + i * 0.2 }));
      } });
      await expect(page.locator('#chart-role-main [data-series="schedule"]')).toHaveCount(1);
      await expect(page.locator('#chart-role-comparison [data-series="schedule"]')).toHaveCount(0);
      await expect(page.locator('[data-series="schedule"]')).toHaveAttribute('aria-label', /^Schedule estimate \(learning\), schedule estimate while the main engine is still learning/);
    });

    test('mirror: the hover readout and the folded summary list the main engine first', async ({ page }) => {
      const order = async () => {
        const t = await page.locator('#chart-hover-panel').innerText();
        return [t.indexOf('v4 Climate Advisor reading:'), t.indexOf('v3:')];
      };
      await open(page, url);
      await expect(page.locator('#chart-hover-panel')).toContainText('v3:');
      let [v4i, v3i] = await order();
      expect(v4i).toBeGreaterThanOrEqual(0);
      expect(v4i).toBeLessThan(v3i);
      await page.unrouteAll({ behavior: 'wait' });
      await open(page, url, { primary: 'v3', shadow: 'v4' });
      await expect(page.locator('#chart-hover-panel')).toContainText('v3:');
      [v4i, v3i] = await order();
      expect(v4i).toBeGreaterThan(v3i);
      await page.locator('#chart-key > summary').click();   // fold: "Showing: CA reading · v3 · v4 ..."
      await expect(page.locator('#chart-key > summary')).toContainText('Showing:');
      const sum = await page.locator('#chart-key > summary').innerText();
      expect(sum.indexOf('v3')).toBeLessThan(sum.indexOf('v4 '));
    });

    test('the plotted lines and their ids do not depend on the roles', async ({ page }) => {
      const lines = () => page.evaluate(() => Chart.getChart(document.getElementById('temp-chart')).data.datasets
        .filter(d => d.caId && /^(v3|v4:|base:|schedule)/.test(d.caId))
        .map(d => [d.caId, d.borderColor, JSON.stringify(d.borderDash), d.data.length]).sort());
      await open(page, url);
      const a = await lines();
      expect(a.map(x => x[0]).sort()).toEqual(SERIES_IDS.concat(['schedule']).sort());
      await page.unrouteAll({ behavior: 'wait' });
      await open(page, url, { primary: 'v3', shadow: 'v4' });
      expect(await lines()).toEqual(a);
    });
  });
}
