// Issue #1341: CAI18N language resolution order, code mapping, per-key fallback, failure tolerance, direction.
// Usage: node tests/i18n_runtime.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadRuntime } from './i18n_node_shim.mjs';

// "local" time zone tests need a known browser zone (Node reads TZ at run time).
process.env.TZ = 'America/Los_Angeles';

const HERE = path.dirname(fileURLToPath(import.meta.url));
// The resolution table is shared with tests/test_i18n_engine.py (i18n.resolve_language).
const RESOLUTION = JSON.parse(fs.readFileSync(path.join(HERE, 'fixtures', 'i18n_language_resolution.json'), 'utf8'));

let n = 0;
async function check(name, fn) {
  try { await fn(); n++; } catch (e) { console.error('FAIL ' + name + ': ' + (e && e.stack || e)); process.exit(1); }
}

const FILES = {
  'i18n/index.json': { languages: ['en', 'fr', 'de', 'ar', 'zh-Hans', 'pt'], areas: ['common', 'status'] },
  'i18n/common/en.json': { 'common.a': 'A en', 'common.only_en': 'only en {n, plural, one {# item} other {# items}}' },
  'i18n/common/de.json': { 'common.a': 'A de' },
  'i18n/status/en.json': { 'status.b': 'B en' },
  'i18n/status/de.json': {},
};

await check('resolution codes', async () => {
  const { CAI18N } = loadRuntime({});
  const r = (c) => CAI18N.resolveLanguage(c, RESOLUTION.languages);
  assert.ok(RESOLUTION.cases.length > 30);
  for (const [i, o] of RESOLUTION.cases) assert.equal(r(i), o, `${JSON.stringify(i)} -> ${o}`);
});

await check('order: ?lang= beats hass beats status beats navigator beats en', async () => {
  const all = { search: '?lang=fr', hassLanguage: 'de', navigatorLanguage: 'ar' };
  assert.equal(loadRuntime(all).CAI18N.lang, 'fr');
  assert.equal(loadRuntime({ hassLanguage: 'de', navigatorLanguage: 'ar' }).CAI18N.lang, 'de');
  const s = loadRuntime({ navigatorLanguage: 'ar' });
  await s.CAI18N.init({ statusLanguage: 'ja' });
  assert.equal(s.CAI18N.lang, 'ja');
  assert.equal(loadRuntime({ navigatorLanguage: 'ar' }).CAI18N.lang, 'ar');
  assert.equal(loadRuntime({}).CAI18N.lang, 'en');
  // a throwing parent lookup falls through
  const t = loadRuntime({ navigatorLanguage: 'ko' });
  t.sandbox.parent = { get document() { throw new Error('cross-origin'); } };
  t.sandbox.top = t.sandbox.parent;
  assert.equal(t.CAI18N.hass(), null);
});

await check('unsupported hass language resolves to nearest, not navigator', async () => {
  assert.equal(loadRuntime({ hassLanguage: 'pt-BR', navigatorLanguage: 'fr' }).CAI18N.lang, 'pt');
  assert.equal(loadRuntime({ hassLanguage: 'sv', navigatorLanguage: 'fr' }).CAI18N.lang, 'en');
});

await check('per-key fallback and warn-once for a key missing everywhere', async () => {
  const { CAI18N, warns } = loadRuntime({ hassLanguage: 'de', files: FILES });
  await CAI18N.init({});
  assert.equal(CAI18N.t('common.a'), 'A de');
  assert.equal(CAI18N.t('status.b'), 'B en');          // missing in de -> en
  assert.equal(CAI18N.t('common.only_en', { n: 3 }), 'only en 3 items'); // en ICU still formats
  assert.equal(CAI18N.t('common.nope'), 'common.nope');
  assert.equal(CAI18N.t('common.nope'), 'common.nope');
  assert.equal(warns.filter((w) => w.includes('common.nope')).length, 1);
  assert.equal(CAI18N.tw({ key: 'common.only_en', params: { n: 1 } }), 'only en 1 item');
  assert.equal(CAI18N.tw('plain'), 'plain');
});

await check('typed wire params and nested msg/code', async () => {
  const { CAI18N } = loadRuntime({ files: { ...FILES, 'i18n/common/en.json': {
    'common.t': 'It is {t}, {d} off, {l}; {m}; mode {c}', 'common.inner': 'inner {n}', 'common.mode.cool': 'cool' } } });
  await CAI18N.init({});
  const out = CAI18N.tw({ key: 'common.t', params: {
    t: { $t: 'temp', f: 72.4, unit: 'fahrenheit', d: 0 }, d: { $t: 'tdelta', f: 9, unit: 'celsius', d: 1 },
    l: { $t: 'list', items: ['a', 'b', 'c'], style: 'and' },
    m: { $t: 'msg', key: 'common.inner', params: { n: 5 } }, c: { $t: 'code', key: 'common.mode.cool' } } });
  assert.equal(out, 'It is 72°F, 5.0°C off, a, b, and c; inner 5; mode cool');
  assert.equal(CAI18N.fmt.temp(32, 'celsius', 0), '0°C');
});

await check('duration mirrors i18n.format_duration thresholds', async () => {
  const { CAI18N } = loadRuntime({ files: { ...FILES, 'i18n/common/en.json': {
    'common.duration.hours': '{n, plural, one {#h} other {#h}}', 'common.duration.hours_frac': '{n, number, ::.0}h',
    'common.duration.min': '{n, plural, one {# min} other {# min}}' } } });
  await CAI18N.init({});
  const d = (s) => CAI18N.fmt.duration(s);
  assert.equal(d(0), '1 min'); assert.equal(d(300), '5 min'); assert.equal(d(90), '2 min'); assert.equal(d(30), '1 min');
  assert.equal(d(3600), '1h'); assert.equal(d(5400), '1.5h'); assert.equal(d(7200), '2h');
});

await check('failed catalog fetch never throws; falls back to the key', async () => {
  const { CAI18N, warns } = loadRuntime({ hassLanguage: 'de', failFetch: true });
  const res = await CAI18N.init({});
  assert.equal(res, CAI18N);
  assert.equal(CAI18N.lang, 'de');
  assert.equal(CAI18N.t('common.a'), 'common.a');
  assert.ok(warns.length >= 1);
});

await check('html lang and dir; rtl only for ar', async () => {
  const ar = loadRuntime({ hassLanguage: 'ar', files: FILES });
  // RTL_LAYOUT_READY is true (#1341 PR C): <html dir> follows the language, so ar is rtl.
  assert.equal(ar.attrs.lang, 'ar'); assert.equal(ar.CAI18N.dir, 'rtl');
  await ar.CAI18N.init({});
  assert.equal(ar.attrs.dir, 'rtl');
  const de = loadRuntime({ hassLanguage: 'de' });
  assert.equal(de.attrs.lang, 'de'); assert.equal(de.attrs.dir, 'ltr');
});

await check('locale comes from hass.locale; esc escapes', async () => {
  const loc = { language: 'de', time_format: '24' };
  const { CAI18N } = loadRuntime({ hass: { language: 'de', locale: loc } });
  await CAI18N.init({});
  assert.deepEqual(CAI18N.locale, loc);
  assert.equal(CAI18N.esc(`<a href="x">'&'</a>`), '&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;');
});

await check('timeZone(): server zone, local and no hass; times follow it (S4)', async () => {
  const ts = '2026-11-14T15:05:00-05:00'; // 20:05 UTC, 12:05 in the (Los Angeles) browser, 05:05 next day in Tokyo
  const mk = (hass) => loadRuntime({ hass }).CAI18N;
  // no hass reachable: the wall clock written in ts
  let c = loadRuntime({}).CAI18N;
  assert.equal(c.timeZone(), null);
  assert.equal(c.fmt.time(ts, 'en'), '3:05 PM');
  assert.equal(c.fmt.date(ts, 'en'), 'Nov 14');
  assert.equal(c.fmt.time('2026-11-14T00:05:00+09:00', 'en'), '12:05 AM');
  assert.equal(c.fmt.dateTime(ts, 'en'), 'Nov 14, 3:05 PM');
  // hass.locale.time_zone === 'server' (the HA default): hass.config.time_zone
  const server = { language: 'en', locale: { time_zone: 'server' }, config: { time_zone: 'Asia/Tokyo' } };
  c = mk(server);
  assert.equal(c.timeZone(), 'Asia/Tokyo');
  assert.equal(c.fmt.time(ts, 'en'), '5:05 AM');
  assert.equal(c.fmt.date(ts, 'en'), 'Nov 15');
  assert.equal(c.fmt.time(ts, 'de'), '5:05');
  // a hass without the setting behaves as the HA default (server)
  c = mk({ language: 'en', config: { time_zone: 'Europe/Berlin' } });
  assert.equal(c.timeZone(), 'Europe/Berlin');
  assert.equal(c.fmt.time(ts, 'en'), '9:05 PM');
  // 'local': the browser's own zone
  c = mk({ language: 'en', locale: { time_zone: 'local' }, config: { time_zone: 'Asia/Tokyo' } });
  assert.equal(c.timeZone(), 'local');
  assert.equal(c.fmt.time(ts, 'en'), '12:05 PM');
  assert.equal(c.fmt.date(ts, 'en'), 'Nov 14');
  // server setting but no zone known, or an unknown zone id: back to the wall clock in ts
  c = mk({ language: 'en', locale: { time_zone: 'server' } });
  assert.equal(c.timeZone(), null);
  assert.equal(c.fmt.time(ts, 'en'), '3:05 PM');
  c = mk({ language: 'en', config: { time_zone: 'Nope/Zone' } });
  assert.equal(c.fmt.time(ts, 'en'), '3:05 PM');
  // Date objects and epoch numbers are instants: hass zone, else the browser zone
  c = mk(server);
  assert.equal(c.fmt.time(new Date('2026-11-14T20:05:00Z'), 'en'), '5:05 AM');
  assert.equal(loadRuntime({}).CAI18N.fmt.time(new Date('2026-11-14T20:05:00Z'), 'en'), '12:05 PM');
  // not an instant: an em dash, never a throw
  for (const bad of ['garbage', '2026-13-45T25:61:00+00:00', new Date(NaN), undefined, null, {}]) {
    assert.equal(c.fmt.time(bad, 'en'), '—');
    assert.equal(c.fmt.date(bad, 'en'), '—');
  }
  // ICU's narrow no-break spaces are normalised to a plain space, like the Python tables
  assert.ok(!/[  ]/.test(loadRuntime({}).CAI18N.fmt.time(ts, 'en')));
});

await check('hour/intl/dateYear/calendarDay follow timeZone(); iso isolates only in rtl (S2/N1/N5)', async () => {
  const instant = new Date('2026-11-14T20:05:00Z'); // 05:05 next day in Tokyo, 12:05 in the (Los Angeles) browser
  const server = { language: 'en', locale: { time_zone: 'server' }, config: { time_zone: 'Asia/Tokyo' } };
  const tokyo = loadRuntime({ hass: server }).CAI18N;
  const browser = loadRuntime({}).CAI18N;
  // fmt.hour: the same zone as fmt.time (it used to be the browser's)
  assert.equal(tokyo.fmt.hour(instant, 'en'), '5 AM');
  assert.equal(browser.fmt.hour(instant, 'en'), '12 PM');
  assert.equal(loadRuntime({ hass: { language: 'en', locale: { time_zone: 'local' }, config: { time_zone: 'Asia/Tokyo' } } }).CAI18N.fmt.hour(instant, 'en'), '12 PM');
  // fmt.intl: a zone-aware Intl.DateTimeFormat for any options; a locales array is used as given ([] = browser locale)
  assert.equal(tokyo.fmt.intl({ hour: 'numeric', minute: '2-digit' }, 'en').format(instant), '5:05 AM');
  assert.equal(tokyo.fmt.intl({ weekday: 'short' }, 'en').format(instant), 'Sun');
  assert.equal(browser.fmt.intl({ weekday: 'short' }, 'en').format(instant), 'Sat');
  assert.equal(tokyo.fmt.intl({ year: 'numeric', month: 'numeric', day: 'numeric' }, ['en-US']).format(instant), '11/15/2026');
  const h24 = loadRuntime({ hass: { language: 'en', locale: { time_zone: 'server', time_format: '24' }, config: { time_zone: 'Asia/Tokyo' } } }).CAI18N;
  h24.init && await h24.init({});
  assert.equal(h24.fmt.intl({ hour: 'numeric', minute: '2-digit' }, ['en-US']).format(instant), '05:05');
  // an unknown zone id falls back to the browser zone for an instant (never throws)
  assert.equal(loadRuntime({ hass: { language: 'en', config: { time_zone: 'Nope/Zone' } } }).CAI18N.fmt.intl({ hour: 'numeric' }, 'en').format(instant), '12 PM');
  // N5: the year shows when the date is not in the current year (in the display zone), never otherwise
  const now = new Date();
  const lastYear = new Date(now.getTime() - 400 * 86400000);
  assert.match(browser.fmt.dateYear(lastYear, 'en'), /^[A-Z][a-z]{2} \d{1,2}, \d{4}$/);
  assert.equal(browser.fmt.dateYear(now, 'en'), browser.fmt.date(now, 'en'));
  assert.equal(browser.fmt.dateYear('garbage', 'en'), '—');
  // a calendar day given by parts is that day in any zone
  assert.equal(tokyo.fmt.calendarDay(2026, 11, 7, 'en'), 'Nov 7');
  assert.equal(tokyo.fmt.calendarDay(2026, 11, 7, 'en', true), 'Sat Nov 7');
  // N1: one isolate helper
  assert.equal(browser.iso('x_y'), 'x_y');
  const ar = loadRuntime({ hassLanguage: 'ar' }).CAI18N;
  assert.equal(ar.iso('sensor.a'), '⁨sensor.a⁩');
});

await check('tw: a wire without a key renders as nothing (N5)', async () => {
  const { CAI18N } = loadRuntime({ files: FILES });
  await CAI18N.init({});
  for (const w of [{}, { key: '' }, { key: 5 }, { params: { n: 1 } }, [], null, undefined]) assert.equal(CAI18N.tw(w), '', JSON.stringify(w));
  assert.equal(CAI18N.tw({ key: 'common.a', params: 'junk' }), 'A en');
});

const DUR = { 'i18n/common/en.json': {
  'common.duration.hours': '{n, plural, one {#h} other {#h}}', 'common.duration.hours_frac': '{n, number, ::.0}h',
  'common.duration.min': '{n, plural, one {# min} other {# min}}' } };
await check('duration: half-even tenths and minutes like Python; unusable input is an em dash (S3)', async () => {
  const { CAI18N } = loadRuntime({ files: { ...FILES, ...DUR } });
  await CAI18N.init({});
  const d = (s) => CAI18N.fmt.duration(s);
  assert.equal(d(75 * 60), '1.2h'); assert.equal(d(135 * 60), '2.2h'); assert.equal(d(105 * 60), '1.8h'); assert.equal(d(165 * 60), '2.8h');
  assert.equal(d(150), '2 min'); assert.equal(d(90), '2 min'); assert.equal(d(210), '4 min');
  for (const bad of [NaN, Infinity, -Infinity, null, undefined, '300', {}]) assert.equal(d(bad), '—', String(bad));
});

await check('english temperatures are the legacy f-string: half-even on the exact value; other languages are Intl (S3)', async () => {
  const { CAI18N } = loadRuntime({});
  const t = (f, u, d, l) => CAI18N.fmt.temp(f, u, d, l);
  for (const [f, d, want] of [[0.5, 0, '0'], [1.5, 0, '2'], [2.5, 0, '2'], [72.5, 0, '72'], [73.5, 0, '74'], [72.25, 1, '72.2'],
    [72.75, 1, '72.8'], [0.125, 2, '0.12'], [0.375, 2, '0.38'], [1.005, 2, '1.00'], [2.675, 2, '2.67'], [-2.5, 0, '-2'],
    [-0.5, 0, '-0'], [-0.04, 1, '-0.0']]) assert.equal(t(f, 'fahrenheit', d, 'en'), want + '°F', `${f}/${d}`);
  assert.equal(CAI18N.fmt.tempDelta(4.5, 'celsius', 0, 'en'), '2°C');
  assert.equal(t(2.5, 'fahrenheit', 0, 'de'), '3°F');
  assert.equal(t(1.005, 'fahrenheit', 2, 'de'), '1,01°F');
  assert.equal(t(1e22, 'fahrenheit', 1, 'en'), '10000000000000000000000.0°F');
  assert.equal(t(null, 'celsius', 1, 'en'), '—'); assert.equal(t('72', 'celsius', 1, 'en'), '—');
  assert.equal(t(NaN, 'celsius', 1, 'en'), 'NaN°C'); assert.equal(t(Infinity, 'fahrenheit', 0, 'de'), '∞°F');
});

await check('list items that are numbers use the locale number format (S3)', async () => {
  const { CAI18N } = loadRuntime({});
  assert.equal(CAI18N.fmt.list([1234.5, 'x', 3], 'and', 'de'), '1.234,5, x und 3');
  assert.equal(CAI18N.format('{l}', { l: { $t: 'list', items: [1234.5, 2], style: 'or' } }, 'en'), '1,234.5 or 2');
});

await check('fallbacks: a missing key uses the English pattern with the requested language formats; a broken translation the English message (S3)', async () => {
  const files = { 'i18n/index.json': { languages: ['en', 'de', 'fr'], areas: ['common'] }, 'i18n/common/en.json': {
    'common.n': '{n, number} / {l}', 'common.b': '{n} items ({t})', 'common.c': '{n, plural, one {# item} other {# items}}' },
  'i18n/common/de.json': { 'common.b': '{n, plural, one {# x} other {# y', 'common.c': '{n, plural, one {# a} few {# b}}' } };
  const { CAI18N } = loadRuntime({ hassLanguage: 'de', files });
  await CAI18N.init({});
  const list = { $t: 'list', items: ['a', 'b'], style: 'and' };
  assert.equal(CAI18N.t('common.n', { n: 1234.5, l: list }), '1.234,5 / a und b');          // not "1,234.5 / a and b"
  const tmp = { $t: 'temp', f: 70.25, unit: 'celsius', d: 1 };
  assert.equal(CAI18N.t('common.b', { n: 1234.5, t: tmp }), '1234.5 items (21.2°C)');   // English message, not raw source
  assert.equal(CAI18N.t('common.c', { n: 3 }), '3 items');
  assert.equal(CAI18N.t('common.c', { n: 'x' }), 'common.c');                                  // fails everywhere: the key
});

console.log(`i18n runtime: ${n} checks OK`);
