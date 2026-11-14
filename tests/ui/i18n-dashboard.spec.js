const { test, expect } = require('@playwright/test');

// Issue #1341 (lane L9a): the dashboard shell, the Status cards, the banners and the night card read their words from the
// catalog. Two specs of the mechanism, neither needing a finished translation:
//   en-XA  the pseudo-locale (?lang=en-XA): every word the shell, banners, reading cards and night card show is a catalog
//          message, so every visible text node there is accented and bracketed. A text node that is plain English bypassed
//          the catalog. The only plain text allowed is data the API sends (names, prose fields), listed below.
//   de     German (?lang=de) from a stub catalog: language selection, <html lang>, per-key English fallback for a key the
//          catalog does not have, locale number format and the status-word messages.

const STATUS = {
  language: 'de', unit: 'fahrenheit', version: '0.9.9', coordinator_healthy: true, automation_enabled: false,
  day_type: 'mild', hvac_mode: 'heat', hvac_action: 'idle', current_setpoint: 68, ca_target_heat: 70, occupancy_mode: 'away',
  indoor_temp: 71, outdoor_temp: 65, compliance_score: 0.84, hvac_runtime_today: 95, trend_direction: 'warming', trend_magnitude: 3,
  automation_status: 'active (away)', whf_status: 'inactive', hvac_fan_status: 'running (manual override)',
  fan_remote_timer_hours: 2, fan_remote_timer_ends: '2026-11-14T22:30:00-05:00', fan_remote_speed: 3,
  manual_override_active: true, grace_active: false, paused_by_door: true, pause_suppressed_classification_text: 'Door held open',
  contact_status: 'all closed',
  contact_sensors: [{ friendly_name: 'Back door', unreadable: true, last_reading: null, treated_as: 'closed' }],
  entity_health_issues: [{ friendly_name: 'Weather source', entity_id: 'weather.home', config_key: 'weather_entity', status: 'unavailable', detail: '' }],
  promise_summary: { total_hits: 2, promises: [{ label: '1: Windows closed at night', promise: 'x', hits: 2 }] },
  next_action: 'Close the windows at 9 PM', next_automation_action: 'Set back to 62°F', next_automation_time: '10:00 PM',
  zone_count: 1, zones: [{ entry_id: 'mock-zone', title: 'Home' }],
};

async function openWith(page, query, extra) {
  await page.route('**/api/climate_advisor/status*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(Object.assign({}, STATUS, extra || {})) }));
  await page.goto('/new_dashboard.html' + query);
  await page.waitForFunction(() => document.querySelector('#status-grid .tile') && document.querySelector('#night-card .rec, #night-card .empty, #night-card .err'), null, { timeout: 8000 });
}

// The visible text of each block inside the parts this lane owns (the chart card and the plan list belong to the chart
// module). Blocks, not text nodes: a message with a number in it is several nodes because the number sits in <bdi>.
function visibleText(page) {
  return page.evaluate(() => {
    const scopes = ['header.top', '#banners', '.status-next', '#status-grid', '#status-plan > .card-head', '#status-slot-night', '#status-briefing'];
    const BLOCK = 'div, li, td, th, h1, h2, h3, button, label, a, summary, p, caption, option, select';
    const seen = new Set(), out = [];
    for (const sel of scopes) {
      for (const root of document.querySelectorAll(sel)) {
        const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        for (let n = w.nextNode(); n; n = w.nextNode()) {
          const el = n.parentElement;
          if (!n.nodeValue.trim() || !el || el.closest('[hidden], script, style, option') || !(el.offsetWidth || el.offsetHeight || el.getClientRects().length)) continue;
          const blk = el.closest(BLOCK) || el;
          if (seen.has(blk)) continue;
          seen.add(blk);
          out.push(blk.textContent.replace(/\s+/g, ' ').trim());
        }
      }
    }
    return out.filter(Boolean);
  });
}

test.describe('Dashboard i18n (Issue #1341, lane L9a)', () => {
  test('en-XA: every word on the shell, banners, reading cards and night card comes from the catalog', async ({ page }) => {
    const errors = [];
    page.on('pageerror', (e) => errors.push(String(e)));
    await openWith(page, '?lang=en-XA');
    expect(await page.evaluate(() => document.documentElement.lang)).toBe('en-XA');
    const texts = await visibleText(page);
    expect(texts.length).toBeGreaterThan(30);
    // Data the API sends (names, prose fields, the sample night plan) is shown as sent; a payload-supplied word is not a
    // catalog bypass. The brand name is not translated.
    const DATA = ['Climate Advisor', 'Hallway', 'Bedroom', 'Back door', 'Weather source', 'weather.home', 'Door held open',
      'Close the windows at 9 PM', 'Set back to 62°F', '10:00 PM', 'Windows closed at night', 'All closed', 'Test briefing',
      '(manual override)']; // the fan status suffix is backend prose until the status carries a code (waits on lane L4/L7)
    // the night card's own prose (labels, headline, tradeoffs, the options' names) is API data too
    const nightData = await page.evaluate(() => Array.from(document.querySelectorAll('#night-card .rec > div:nth-child(2), #night-card #night-headline, #night-card #night-tradeoffs li, #night-card #night-source-note, #night-card .note-box, #night-card tbody td:first-child, #night-card .pill, #night-card tbody td:last-child'))
      .map((e) => e.textContent.replace(/\s+/g, ' ').trim()).filter(Boolean));
    const NUM = /[+\u2212-]?\d+(?:[.,:]\d+)*(?:\s?[AaPp][Mm])?(?:\s?°\s?[CF])?(?:\s?[–-]\s?\d+(?:[.,]\d+)*(?:\s?°\s?[CF])?)?%?/g;
    const bypass = [];
    for (const raw of texts) {
      let t = raw;
      for (const d of DATA.concat(nightData).sort((a, b) => b.length - a.length)) t = t.split(d).join(' ');
      for (let prev = null; prev !== t;) { prev = t; t = t.replace(/\[[^\[\]]*\]/g, ' '); } // innermost first: a message can carry another as a param
      t = t.replace(NUM, ' ');
      if (/\p{L}/u.test(t)) bypass.push(raw + '   <-- leftover: ' + t.trim());
    }
    expect(bypass, 'text that bypassed the catalog (plain English under en-XA)').toEqual([]);
    expect(texts.some((t) => /\[[^\]]*[ñšţ][^\]]*\]/.test(t))).toBe(true);
    expect(errors).toEqual([]);
  });

  test('de: language, per-key fallback and locale formatting from a stub catalog', async ({ page }) => {
    await page.route('**/i18n/dashboard/de.json', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      'dashboard.core.tab_status': 'Übersicht',
      'dashboard.core.for_you': 'Für dich',
      'dashboard.core.health_line': '{health, select, ok {Gesund} other {Nicht verfügbar}}{version, select, none {} other { · v{version}}} · Automatik {automation, select, on {an} other {aus}}',
      'dashboard.banner.off_title': 'Automatik ist aus',
      'dashboard.banner.promises_title': '{n, plural, one {# Haushaltsversprechen} other {# Haushaltsversprechen}} in den letzten 30 Tagen verpasst',
      'dashboard.status.card_today': 'Heute',
      'dashboard.status.day_type_mild': 'Mild',
      'dashboard.status.hvac_heating_to': 'Heizen auf {temp}',
      'dashboard.status.act_idle': 'gerade im Leerlauf',
      'dashboard.status.comfort_score': 'Komfortwert {score, number, ::percent}',
    }) }));
    await openWith(page, '?lang=de', { automation_enabled: true, manual_override_active: false, paused_by_door: false, contact_sensors: [], entity_health_issues: [] });
    expect(await page.evaluate(() => document.documentElement.lang)).toBe('de');
    expect(await page.evaluate(() => CAI18N.lang)).toBe('de');
    await expect(page.locator('#tab-btn-status')).toHaveText('Übersicht');
    await expect(page.locator('.status-next .tile .l').first()).toHaveText('Für dich');
    await expect(page.locator('#health-text')).toHaveText('Gesund · v0.9.9 · Automatik an');
    await expect(page.locator('#status-card-today .l')).toHaveText('Heute');
    await expect(page.locator('#status-hvac-line')).toContainText('Heizen auf 68°F · gerade im Leerlauf');
    await expect(page.locator('#status-card-occupancy')).toContainText('Komfortwert 84');
    // a key the German catalog does not have falls back to English, per key (not to the key, not to a blank)
    await expect(page.locator('#tab-btn-analysis')).toHaveText('Analysis');
    // the banner for two missed promises is German with the plural applied
    await page.route('**/api/climate_advisor/settings_extra*', (route) => route.fulfill({ status: 404, body: '{}' }));
    await expect(page.locator('#banners')).toContainText('2 Haushaltsversprechen in den letzten 30 Tagen verpasst');
    // locale number format: German writes the decimal comma
    await expect(page.locator('#status-card-hallway .v')).toContainText('71,0°F');
  });

  test('the Actions menu stays open when its label text (now a child span) is clicked', async ({ page }) => {
    await openWith(page, '?lang=en');
    await page.locator('#actions-btn > span').click();
    await expect(page.locator('#actions-menu')).toBeVisible();
    await expect(page.locator('#actions-btn')).toHaveAttribute('aria-expanded', 'true');
    await page.locator('#actions-menu [data-action="refresh"]').click();
    await expect(page.locator('#actions-menu')).toBeHidden();
  });
});
