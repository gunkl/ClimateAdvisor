const { test, expect } = require('@playwright/test');

// Issue #1399: the "Next 24 hours" list shows each step's time (with the weekday on another day) and who acts in its
// own columns, so the row's sentence must not open with that time again ("8:30 PM | CA | 8:30 PM: bedtime, CA ...")
// and must read as a sentence of its own (capital first letter, no "CA" beside the CA badge). The mock's steps carry
// the backend's canonical, time-first `text` and the `text_short` row wording (timeline_surfaces.plan_list_text).

const PAGES = [
  { name: 'classic Status', url: '/', list: '#plan-list' },
  { name: 'new dashboard Status', url: '/new_dashboard.html', list: '#status-plan-list' },
];

for (const p of PAGES) {
  test.describe('Next 24 hours row text (' + p.name + ')', () => {
    test('no row repeats its own time, starts with a capital, and a CA row does not start with "CA"', async ({ page }) => {
      await page.goto(p.url);
      await page.waitForFunction((sel) => document.querySelectorAll(sel + ' .plan-row').length > 0, p.list, { timeout: 8000 });
      const rows = await page.locator(p.list + ' > .plan-row').evaluateAll(els => els.map(e => ({
        actor: e.dataset.actor,
        time: e.querySelector('.plan-time').innerText.trim(),
        text: e.querySelector('.plan-text').innerText.trim(),
      })));
      expect(rows.length).toBe(4);
      for (const r of rows) {
        const clock = r.time.split(/\s+/).filter(w => /\d/.test(w))[0];   // "8:30" out of "Sun 8:30 PM"
        expect(r.text.startsWith(r.time), r.text).toBe(false);
        expect(r.text.startsWith(clock), r.text).toBe(false);
        expect(/^(?:[A-Za-z]{2,4}\.? )?\d{1,2}:\d{2}/.test(r.text), r.text).toBe(false);
        expect(/^[A-Z]/.test(r.text), r.text).toBe(true);
        if (r.actor === 'ca') expect(/^CA\b/.test(r.text), r.text).toBe(false);
      }
      // the wording the owner asked for
      const bed = await page.locator(p.list + ' > .plan-row[data-kind="sleep_band"] .plan-text').innerText();
      expect(bed).toMatch(/^Bedtime: switches to the sleep band, 66°F to 72°F, and reads the bedroom sensor until /);
      const pre = await page.locator(p.list + ' > .plan-row[data-kind="precool_start"] .plan-text').innerText();
      expect(pre).toMatch(/^Pre-cools the house to 70°F/);
    });
  });
}
