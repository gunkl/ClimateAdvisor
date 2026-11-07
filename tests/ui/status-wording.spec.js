const { test, expect } = require('@playwright/test');

// Issue #1399 (lane G): the Status reading cards word everything in plain language.
//  - A gap between two readings is worded as a gap ("Weather service says 54.0°F, 6.2° warmer than your sensor"),
//    never as a signed number with a unit that reads like a temperature ("sensor −6.2°F"). Both values arrive in the
//    display unit, so in °C the gap is the °C gap (5/9 of the °F gap, no −32).
//  - No card line is a bare state token ("active", "idle", "inactive"): the thermostat line says what it is doing,
//    the automation line is a sentence, fans are sentences.
//  - Today's classification is the big value like every other card, and (new dashboard) the big values of a row
//    share one top.
// CA_SHOT_DIR=<dir> saves light / dark / 375 px screenshots of the new dashboard's cards.

const F = { unit: 'fahrenheit', outdoor: 47.8, service: 54.0, indoor: 67.0, setpoint: 68 };
const C = { unit: 'celsius', outdoor: 8.8, service: 12.2, indoor: 19.4, setpoint: 20 };

function status(u) {
  return {
    unit: u.unit, day_type: 'warm', trend_direction: 'warming', trend_magnitude: u.unit === 'celsius' ? 1.7 : 3.0,
    hvac_mode: 'heat', hvac_action: 'idle', current_setpoint: u.setpoint, ca_target_heat: u.setpoint,
    indoor_temp: u.indoor, outdoor_temp: u.outdoor, automation_enabled: true, automation_status: 'active',
    occupancy_mode: 'home', compliance_score: 0.92, hvac_runtime_today: 42,
    whf_status: 'inactive', hvac_fan_status: 'active', fan_remote_speed: 'low',
    contact_status: 'all closed', contact_sensors: [{ entity_id: 'binary_sensor.door', friendly_name: 'Front door', unreadable: false }],
  };
}

async function routeAll(page, u, sensors) {
  await page.route('**/api/climate_advisor/status*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(status(u)) }));
  await page.route('**/api/climate_advisor/chart_data*', async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    // The weather service's figure for now is the newest state_log forecast (the cards read it from there).
    body.state_log = body.state_log.map((e) => Object.assign({}, e, { pred_outdoor: u.service, pred_outdoor_avg: null }));
    body.unit = u.unit;
    if (sensors) { // a long sensor name wraps its header: the big values must still line up
      const t = new Date().toISOString();
      body.indoor_sensors = [
        { id: 'climate.hall', name: 'Downstairs hallway thermostat by the stairs', role: 'thermostat', primary: true },
        { id: 'sensor.bed', name: 'Bedroom', role: 'bedroom sensor' },
      ];
      body.actual_indoor_sensors = { 'climate.hall': [{ time: t, temp: u.indoor }], 'sensor.bed': [{ time: t, temp: u.indoor - 1.7 }] };
    }
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
}

async function openNew(page, u, sensors) {
  await routeAll(page, u, sensors);
  await page.goto('/new_dashboard.html');
  await page.waitForSelector('#status-card-outdoor');
  await page.waitForFunction(() => /says/.test((document.querySelector('#status-card-outdoor') || {}).textContent || ''), null, { timeout: 8000 });
}
async function openClassic(page, u) {
  await routeAll(page, u);
  await page.goto('/');
  await page.waitForFunction(() => /weather service says/.test((document.getElementById('status-grid') || {}).textContent || ''), null, { timeout: 8000 });
}

// A line that is only a state word (or "Name: state") says nothing about what it is.
const BARE = /^(?:[\w ()]+:\s*)?(active|inactive|idle|off|on|running|disabled|unknown|enabled|low|medium|high|true|false)$/i;

test.describe('Status wording helpers (Issue #1399)', () => {
  for (const pg of [{ name: 'new dashboard', open: openNew, words: 'ND.statusWords' }, { name: 'classic', open: openClassic, words: 'window.CAStatusWords' }]) {
    test(`${pg.name}: a gap is worded warmer / cooler / matches, in °F and °C`, async ({ page }) => {
      await pg.open(page, F);
      const out = await page.evaluate((w) => {
        const W = eval(w);
        return [
          W.saysVs('Weather service', 54.0, 47.8, '°F', 'your sensor'),
          W.saysVs('Weather service', 45.0, 47.8, '°F', 'your sensor'),
          W.saysVs('Weather service', 48.2, 47.8, '°F', 'your sensor'),
          W.saysVs('Weather service', 12.2, 8.8, '°C', 'your sensor'),
          W.saysVs('Weather service', 7.2, 8.8, '°C', 'your sensor'),
          W.saysVs('Weather service', 8.6, 8.8, '°C', 'your sensor'),
          W.saysVs('Weather service', 48.3, 47.8, '°F', 'your sensor'),
          W.trendWords('warming', 3), W.trendWords('cooling', 1.7), W.trendWords('stable', 1),
          W.automationPhrase('active'), W.automationPhrase('active (away)'), W.automationPhrase('nat-vent'), W.automationPhrase('disabled'),
          W.fanSentence('Whole-house fan', 'inactive'), W.fanSentence('HVAC fan', 'running (manual override)'),
          W.fanSentence('Whole-house fan', 'active (unconfirmed)'),
        ];
      }, pg.words);
      expect(out).toEqual([
        'Weather service says 54.0°F, 6.2° warmer than your sensor',
        'Weather service says 45.0°F, 2.8° cooler than your sensor',
        'Weather service says 48.2°F, matches your sensor',
        'Weather service says 12.2°C, 3.4° warmer than your sensor',
        'Weather service says 7.2°C, 1.6° cooler than your sensor',
        'Weather service says 8.6°C, matches your sensor',
        'Weather service says 48.3°F, 0.5° warmer than your sensor',
        '↑ Tomorrow 3.0° warmer than today', '↓ Tomorrow 1.7° cooler than today', '→ Tomorrow about the same as today',
        'following today’s plan', 'following the away plan', 'cooling with outside air (nat-vent)', 'paused, not changing the thermostat',
        'Whole-house fan is off', 'HVAC fan is running (manual override)', 'Whole-house fan should be running (not confirmed yet)',
      ]);
      // The thermostat line: what it is heating to, what it is doing now, and CA's figure only when they disagree.
      const hv = await page.evaluate((w) => {
        const W = eval(w);
        return [
          W.hvacSentence({ hvac_mode: 'heat', hvac_action: 'idle', current_setpoint: 68, ca_target_heat: 68 }, '°F'),
          W.hvacSentence({ hvac_mode: 'cool', hvac_action: 'cooling', current_setpoint: 24, ca_target_cool: 22 }, '°C'),
          W.hvacSentence({ hvac_mode: 'heat_cool', target_temp_low: 66, target_temp_high: 74 }, '°F'),
          W.hvacSentence({ hvac_mode: 'off', hvac_action: 'off' }, '°F'),
        ];
      }, pg.words);
      expect(hv[0]).toBe('Heating to 68°F · idle now');
      expect(hv[1]).toContain('Cooling to 24°C · cooling now');
      expect(hv[1]).toContain('(Climate Advisor wants 22°C)');
      expect(hv[2]).toBe('Keeping between 66°F and 74°F');
      expect(hv[3]).toBe('Thermostat is off');
    });
  }
});

test.describe('Outdoor card and reading cards (Issue #1399)', () => {
  for (const u of [F, C]) {
    const sym = u.unit === 'celsius' ? '°C' : '°F';
    const want = u.unit === 'celsius' ? 'The weather service says 12.2°C, 3.4° warmer than your outdoor sensor' : 'The weather service says 54.0°F, 6.2° warmer than your outdoor sensor';
    test(`new dashboard outdoor card, ${sym}: the gap reads as a gap`, async ({ page }) => {
      await openNew(page, u);
      const card = page.locator('#status-card-outdoor');
      await expect(card.locator('.v')).toHaveText(u.outdoor.toFixed(1) + sym);
      await expect(card).toContainText(want);
      const txt = await card.innerText();
      // No negative number with a degree sign anywhere on the card (the gap is never shown like a reading).
      expect(txt).not.toMatch(/[−-]\s*\d+(\.\d+)?\s*°/);
      expect(await card.locator('.n', { hasText: 'weather service says' }).getAttribute('title')).toContain('not a temperature');
    });
    test(`classic outdoor card, ${sym}: the gap reads as a gap`, async ({ page }) => {
      await openClassic(page, u);
      const card = page.locator('.status-item', { hasText: 'Outdoor sensor' });
      await expect(card.locator('.value')).toHaveText(u.outdoor.toFixed(1) + sym);
      await expect(card).toContainText(want);
      expect(await card.innerText()).not.toMatch(/[−-]\s*\d+(\.\d+)?\s*°/);
    });
  }

  test('new dashboard: every card line says what it is, Today leads with the day type', async ({ page }) => {
    await openNew(page, F);
    const lines = await page.$$eval('#status-grid .tile .n, #status-grid .tile .v', (els) => els.map((e) => e.innerText.trim()).filter(Boolean));
    expect(lines.filter((l) => BARE.test(l))).toEqual([]);
    await expect(page.locator('#status-hvac-line')).toHaveText('Heating to 68°F · idle now');
    await expect(page.locator('#status-automation-line')).toHaveText('Climate Advisor: following today’s plan');
    await expect(page.locator('#status-card-today .v')).toHaveText('Warm');
    await expect(page.locator('#status-card-today .badge')).toHaveCount(0);
    await expect(page.locator('#status-card-today')).toContainText('Tomorrow 3.0° warmer than today');
    await expect(page.locator('#status-card-doors')).toContainText('Whole-house fan is off');
    await expect(page.locator('#status-card-doors')).toContainText('HVAC fan is running');
    await expect(page.locator('#status-card-doors')).toContainText('The remote set the fan speed to low');
    await expect(page.locator('#status-card-occupancy .v')).toHaveText('Home');
  });

  test('classic: every card line says what it is, Today leads with the day type', async ({ page }) => {
    await openClassic(page, F);
    const lines = await page.$$eval('#status-grid .status-item .note, #status-grid .status-item .value', (els) => els.map((e) => e.innerText.trim()).filter(Boolean));
    expect(lines.filter((l) => BARE.test(l))).toEqual([]);
    const today = page.locator('.status-item', { hasText: 'Today' }).first();
    await expect(today.locator('.value')).toHaveText('Warm');
    await expect(today.locator('.badge')).toHaveCount(0);
    await expect(page.locator('#status-grid')).toContainText('Heating to 68°F · idle now');
    await expect(page.locator('.status-item', { hasText: 'Status' }).first().locator('.value')).toHaveText('Following today’s plan');
  });

  for (const vp of [{ name: 'desktop', width: 1280, height: 900, scheme: 'light' }, { name: 'desktop-dark', width: 1280, height: 900, scheme: 'dark' }, { name: 'phone', width: 375, height: 800, scheme: 'light' }]) {
    test(`new dashboard, ${vp.name}: the big values of a row share one top`, async ({ page }) => {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.emulateMedia({ colorScheme: vp.scheme });
      await openNew(page, F, true);
      await page.waitForSelector('#status-card-bedroom');
      const rows = await page.$$eval('#status-grid > .tile', (tiles) => {
        const by = {};
        tiles.forEach((t) => {
          const top = Math.round(t.getBoundingClientRect().top);
          (by[top] = by[top] || []).push(t.querySelector('.v').getBoundingClientRect().top);
        });
        return Object.values(by);
      });
      expect(rows.length).toBeGreaterThan(0);
      for (const tops of rows) expect(Math.max(...tops) - Math.min(...tops)).toBeLessThanOrEqual(2);
      // Long headers end in an ellipsis and keep the full name as a tooltip.
      const titled = await page.$$eval('#status-grid .tile .l', (els) => els.every((e) => e.getAttribute('title') === e.textContent));
      expect(titled).toBe(true);
      if (process.env.CA_SHOT_DIR) await page.locator('#status-grid').screenshot({ path: `${process.env.CA_SHOT_DIR}/status-cards-${vp.name}.png` });
    });
  }
});
