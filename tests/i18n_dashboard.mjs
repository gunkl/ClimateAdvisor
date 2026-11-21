// Issue #1341 (lane L9a): the dashboard additions to CAI18N: the en-XA pseudo-locale, th() (escaped HTML with <bdi>),
// text() (the `<field>_msg` wire sibling or the prose field), has(), and apply() (data-i18n / data-i18n-attr, textContent only).
// Usage: node tests/i18n_dashboard.mjs
import assert from 'node:assert/strict';
import { loadRuntime } from './i18n_node_shim.mjs';

let n = 0;
async function check(name, fn) {
  try { await fn(); n++; } catch (e) { console.error('FAIL ' + name + ': ' + (e && e.stack || e)); process.exit(1); }
}

await check('?lang=en-XA selects the pseudo-locale (and nothing else does)', async () => {
  assert.equal(loadRuntime({ search: '?lang=en-XA' }).CAI18N.lang, 'en-XA');
  assert.equal(loadRuntime({ search: '?lang=en-xa' }).CAI18N.lang, 'en-XA');
  assert.equal(loadRuntime({ hassLanguage: 'en-XA' }).CAI18N.lang, 'en'); // only ever from ?lang=
  assert.equal(loadRuntime({ navigatorLanguage: 'en-XA' }).CAI18N.lang, 'en');
});

await check('pseudo-localisation keeps ICU syntax and brackets, accents and grows the text', async () => {
  const { CAI18N } = loadRuntime({});
  const p = CAI18N.pseudo('{n, plural, one {# step} other {# steps}} for {who}');
  assert.match(p, /^\[.*\]$/);
  assert.ok(p.includes('{n, plural, one {# ') && p.includes('} other {# ') && p.includes('{who}'), p);
  assert.ok(!/[a-z]{3,}/.test(p.replace(/\{[^}]*\}/g, '').replace(/one|other|plural/g, '')), 'no plain ASCII words left: ' + p);
  assert.ok(p.includes('~'), 'padded for expansion: ' + p);
  // quoted braces and apostrophes survive, a skeleton is untouched
  assert.ok(CAI18N.pseudo("It's '{'x'}' {t, number, ::percent}").includes("'{'") );
  assert.ok(CAI18N.pseudo('{t, number, ::percent} done').startsWith('[{t, number, ::percent} '));
});

await check('the pseudo catalog is built from en and renders through the real engine', async () => {
  const files = { 'i18n/index.json': { languages: ['en', 'de'], areas: ['dashboard'] },
    'i18n/dashboard/en.json': { 'dashboard.core.x': '{n, plural, one {# step} other {# steps}} for you' } };
  const { CAI18N } = loadRuntime({ search: '?lang=en-XA', files });
  await CAI18N.init({});
  const one = CAI18N.t('dashboard.core.x', { n: 1 }), two = CAI18N.t('dashboard.core.x', { n: 2 });
  assert.ok(one.startsWith('[1 ') && two.startsWith('[2 ') && /ñ|š|ţ/.test(two), one + ' / ' + two);
  assert.equal(CAI18N.fmt.number(1234.5, { useGrouping: false }), '1234.5'); // Intl gets 'en', not 'en-XA'
  assert.equal(CAI18N.dir, 'ltr');
});

await check('th escapes the message and wraps string params in <bdi>; numbers stay numbers', async () => {
  const { CAI18N } = loadRuntime({});
  CAI18N._test.setCatalog('en', { 'dashboard.core.k': '{n, plural, one {# step & more} other {# steps}} at {when} for {who}' });
  const html = CAI18N.th('dashboard.core.k', { n: 2, when: '3:05 PM', who: '<img src=x onerror=alert(1)>' });
  assert.equal(html, '2 steps at <bdi>3:05 PM</bdi> for <bdi>&lt;img src=x onerror=alert(1)&gt;</bdi>');
  assert.ok(CAI18N.th('dashboard.core.k', { n: 1, when: 'x', who: 'y' }).startsWith('1 step &amp; more'));
});

await check('text() prefers the _msg wire sibling when its key is in the catalog, else the prose field', async () => {
  const { CAI18N } = loadRuntime({});
  CAI18N._test.setCatalog('en', { 'status.hot': 'It is {t} out' });
  assert.equal(CAI18N.text({ a: 'prose', a_msg: { key: 'status.hot', params: { t: 90 } } }, 'a'), 'It is 90 out');
  assert.equal(CAI18N.text({ a: 'prose', a_msg: { key: 'status.not_in_catalog', params: {} } }, 'a'), 'prose');
  assert.equal(CAI18N.text({ a: 'prose' }, 'a'), 'prose');
  assert.equal(CAI18N.text({ a: null }, 'a'), '');
  assert.equal(CAI18N.text(null, 'a'), '');
  assert.equal(CAI18N.has('status.hot'), true);
  assert.equal(CAI18N.has('status.nope'), false);
});

function fakeEl(attrs) {
  const a = Object.assign({}, attrs); const el = { textContent: 'English', getAttribute: (k) => (k in a ? a[k] : null), setAttribute: (k, v) => { a[k] = v; }, a };
  return el;
}
await check('apply() fills textContent and attributes (never innerHTML); unknown keys keep the markup', async () => {
  const { CAI18N } = loadRuntime({});
  CAI18N._test.setCatalog('en', { 'k.t': 'T &amp; x <3', 'k.title': 'Ti', 'k.n': '{n}h' });
  const e1 = fakeEl({ 'data-i18n': 'k.t' }), e2 = fakeEl({ 'data-i18n': 'k.missing' }), e3 = fakeEl({ 'data-i18n-attr': 'title:k.title;aria-label:k.t' }),
    e4 = fakeEl({ 'data-i18n': 'k.n', 'data-i18n-params': '{"n":6}' });
  const root = { querySelectorAll: (sel) => (sel === '[data-i18n]' ? [e1, e2, e4] : sel === '[data-i18n-attr]' ? [e3] : []) };
  CAI18N.apply(root);
  assert.equal(e1.textContent, 'T &amp; x <3');
  assert.equal(e2.textContent, 'English');
  assert.equal(e3.a.title, 'Ti'); assert.equal(e3.a['aria-label'], 'T &amp; x <3');
  assert.equal(e4.textContent, '6h');
});

await check('word() looks a code up; an unknown or missing code falls back without throwing (Issue #1665)', async () => {
  const { CAI18N } = loadRuntime({});
  CAI18N._test.setCatalog('en', { 'p.high': 'high' });
  assert.equal(CAI18N.word('p.', 'high'), 'high');
  assert.equal(CAI18N.word('p.', 'brand_new'), 'brand_new');            // unknown: the code as sent
  assert.equal(CAI18N.word('p.', 'brand_new', 'brand new'), 'brand new');
  assert.equal(CAI18N.word('p.', null), '');
  assert.equal(CAI18N.word('p.', '', '—'), '—');
  assert.equal(CAI18N.word('p.', undefined, '—'), '—');
  assert.equal(CAI18N.word('p.', 7), '7');
});

// Issue #1665: each code the dashboard shows inside translated text, rendered from the shipped catalogs in every
// language. English keeps its text (the word is the code); the other languages show their own word, never the code.
const CODE_WORDS = [
  ['dashboard.night.level.', ['high', 'medium']],
  ['dashboard.analysis.level.', ['none', 'low', 'medium', 'high', 'na']],
  ['dashboard.analysis.day_type.', ['hot', 'warm', 'mild', 'cool', 'cold']],
  ['dashboard.analysis.trend.', ['warming', 'cooling', 'stable']],
  ['dashboard.analysis.days.skip_reason.', ['override_confirm_pending', 'occupancy', 'manual_override', 'paused_by_door', 'no_classification']],
  ['dashboard.analysis.source.', ['automation', 'manual', 'system', 'sensor', 'ai', 'fallback', 'error', 'unknown']],
  ['dashboard.chart.event.', ['classification_change', 'override', 'hvac_action_change', 'occupancy_change', 'occupancy_setback']],
  ['dashboard.status.fan_speed_', ['low', 'medium', 'high']],
  ['settings.ai.status.', ['active', 'disabled', 'circuit_open', 'budget_exceeded', 'inactive', 'error']],
  ['settings.ai.breaker.', ['closed', 'open', 'half_open']],
  ['common.hvac_mode_word.', ['heat', 'cool', 'off', 'auto']],
];
await check('every dashboard code word renders in all 17 languages; none is the raw code outside English', async () => {
  const { CAI18N } = loadRuntime({});
  const langs = CAI18N.languages;
  assert.equal(langs.length, 17);
  const raw = {};
  for (const lang of langs) {
    const rt = loadRuntime({ hassLanguage: lang });
    await rt.CAI18N.init({});
    assert.equal(rt.CAI18N.lang, lang);
    for (const [prefix, codes] of CODE_WORDS) {
      for (const code of codes) {
        const w = rt.CAI18N.word(prefix, code);
        assert.ok(w && w !== prefix + code && !w.includes('{'), lang + ' ' + prefix + code + ' -> ' + w);
        if (lang === 'en' && prefix.startsWith('dashboard.')) assert.equal(w, code === 'na' ? 'n/a' : code.replace(/_/g, ' '), prefix + code);
        // an underscored code left in another language is a raw code
        if (lang !== 'en') assert.ok(!/^[a-z]+_[a-z_]+$/.test(w), lang + ' raw ' + prefix + code + ' -> ' + w);
        if (lang !== 'en' && w === code) (raw[prefix + code] = raw[prefix + code] || []).push(lang);
      }
    }
    assert.equal(rt.CAI18N.word('dashboard.analysis.level.', 'brand_new'), 'brand_new'); // unknown code: as sent
    assert.equal(rt.warns.length, 0, lang + ': ' + rt.warns.join(' | '));
  }
  // a loanword equal to the code ("AI", "sensor") is fine in a few languages, never in most of them
  for (const [k, ls] of Object.entries(raw)) assert.ok(ls.length <= 4, k + ' untranslated in ' + ls.join(','));
});

console.log('i18n dashboard additions: ' + n + ' checks OK');
