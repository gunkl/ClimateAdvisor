const { test, expect } = require('@playwright/test');

// Issue #1404: during a time-of-use bank the Status and reading cards say what the thermostat is told.
// Occupant view: the house is Away and pre-cooling to 77°F for a 4:15 PM high-cost period. Before the fix the hallway
// card said "Cooling to 77°F (Climate Advisor wants 80°F)" (the Away band's ceiling) and the plan list called 60-77°F
// "the away band", so the person could not tell what the house was held to or when it went back. The backend now sends
// ca_target_cool = the bank target, the Status sentence and a current_hold block; the cards and the plan list show them.

const SENTENCE = 'Pre-cooling to 77°F until the 4:15 PM high-cost period, then back to the Away band 60–80°F';
const STATUS_LINE = 'pre-cooling to 77°F until the 4:15 PM high-cost period, then back to the Away band 60–80°F';

function hold() {
  return {
    kind: 'tou_bank', mode: 'cool', target: 77, ends: new Date(Date.now() + 2 * 3600000).toISOString(), schedule_id: 'peak1',
    heat: 60, cool: 77, schedule_heat: 60, schedule_cool: 80, after_heat: 60, after_cool: 80,
    text: SENTENCE, after_text: 'back to the Away band 60–80°F',
  };
}

function status(banked) {
  return {
    unit: 'fahrenheit', day_type: 'hot', trend_direction: 'stable', trend_magnitude: 0,
    hvac_mode: 'cool', hvac_action: 'cooling', current_setpoint: 77,
    // the bank band (banked) or the pre-#1404 payload (the Away band while the thermostat holds the bank)
    ca_target_heat: 60, ca_target_cool: banked ? 77 : 80,
    indoor_temp: 78.2, outdoor_temp: 91, automation_enabled: true,
    automation_status: banked ? STATUS_LINE : 'pre-cooling — TOU high-cost period (ends 4:15 PM)',
    next_action: banked ? 'Nothing needs you: ' + STATUS_LINE + '.' : 'Away mode: nothing needs you right now.',
    next_automation_action: banked ? 'High-cost period starts — back to the Away band 60–80°F' : 'No more actions today',
    next_automation_time: banked ? '4:15 PM' : '',
    occupancy_mode: 'away', compliance_score: 0.9, hvac_runtime_today: 30,
    current_hold: banked ? hold() : null,
  };
}

async function routeAll(page, banked) {
  await page.route('**/api/climate_advisor/status*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(status(banked)) }));
  await page.route('**/api/climate_advisor/timeline**', async (route) => {
    const resp = await route.fetch();
    const body = await resp.json();
    body.current_hold = banked ? hold() : null;
    await route.fulfill({ response: resp, body: JSON.stringify(body) });
  });
}

async function waitPlan(page, sel) {
  await page.waitForFunction((s) => document.querySelectorAll(s + ' .plan-row').length > 0, sel, { timeout: 8000 });
}

test.describe('Time-of-use bank on the Status cards (Issue #1404)', () => {
  test('new dashboard: the hallway card, the For you / CA next cards and the plan list tell the bank story', async ({ page }) => {
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    await routeAll(page, true);
    await page.goto('/new_dashboard.html');
    await page.waitForSelector('#status-hvac-line');
    await expect(page.locator('#status-hvac-line')).toHaveText('Cooling to 77°F · cooling now');
    await expect(page.locator('#status-hvac-line')).not.toContainText('wants');
    await expect(page.locator('#status-automation-line')).toHaveText('Climate Advisor: ' + STATUS_LINE);
    await expect(page.locator('#status-for-you')).toHaveText('Nothing needs you: ' + STATUS_LINE + '.');
    await expect(page.locator('#status-ca-next')).toHaveText('High-cost period starts — back to the Away band 60–80°F');
    await expect(page.locator('#status-ca-next-time')).toHaveText('4:15 PM');
    await waitPlan(page, '#status-plan-list');
    const first = page.locator('#status-plan-list > .plan-row').first();
    await expect(first).toHaveAttribute('data-kind', 'tou_bank');
    await expect(first.locator('.plan-time')).toHaveText('Now');
    await expect(first.locator('.plan-text')).toHaveText(SENTENCE);
    expect(errors).toEqual([]);
  });

  test('classic Status: the Status value and the plan list say the same', async ({ page }) => {
    await routeAll(page, true);
    await page.goto('/');
    await page.waitForFunction(() => /pre-cooling to 77/i.test((document.getElementById('status-grid') || {}).textContent || ''), null, { timeout: 8000 });
    const grid = await page.locator('#status-grid').innerText();
    expect(grid).toContain('Pre-cooling to 77°F until the 4:15 PM high-cost period, then back to the Away band 60–80°F');
    expect(grid).not.toContain('wants 80');
    await waitPlan(page, '#plan-list');
    await expect(page.locator('#plan-list > .plan-row').first().locator('.plan-text')).toHaveText(SENTENCE);
  });

  test('control: the pre-#1404 payload is exactly what made the card contradict the thermostat', async ({ page }) => {
    await routeAll(page, false);
    await page.goto('/new_dashboard.html');
    await page.waitForSelector('#status-hvac-line');
    await expect(page.locator('#status-hvac-line')).toContainText('(Climate Advisor wants 80°F)');
    await waitPlan(page, '#status-plan-list');
    await expect(page.locator('#status-plan-list [data-kind="tou_bank"]')).toHaveCount(0);
  });
});
