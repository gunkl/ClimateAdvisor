// Issue #1341: CAI18N language resolution order, code mapping, per-key fallback, failure tolerance, direction.
// Usage: node tests/i18n_runtime.mjs
import assert from 'node:assert/strict';
import { loadRuntime } from './i18n_node_shim.mjs';

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
  const r = (c) => CAI18N.resolveLanguage(c, ['en','fr','es','de','nl','ja','pt','it','pl','uk','ko','hi','ar','ru','zh-Hans','tr']);
  for (const [i, o] of [['de', 'de'], ['DE', 'de'], ['pt-BR', 'pt'], ['de-CH', 'de'], ['zh', 'zh-Hans'], ['zh-CN', 'zh-Hans'],
    ['zh-SG', 'zh-Hans'], ['zh-Hans-CN', 'zh-Hans'], ['zh-TW', 'en'], ['zh-HK', 'en'], ['zh-Hant', 'en'], ['xx', 'en'],
    ['', 'en'], [null, 'en'], ['zh-Hans', 'zh-Hans'], ['en_US', 'en']]) assert.equal(r(i), o, `${i} -> ${o}`);
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
  assert.equal(ar.attrs.lang, 'ar'); assert.equal(ar.attrs.dir, 'rtl'); assert.equal(ar.CAI18N.dir, 'rtl');
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

console.log(`i18n runtime: ${n} checks OK`);
