// Generates the i18n conformance fixtures from FormatJS + Intl in Node (Issue #1341).
//
//   node tools/i18n_conformance_gen.mjs [--imf <path to intl-messageformat.iife.js>]
//        writes tests/fixtures/i18n_conformance.json and tests/fixtures/i18n_plural_table.json
//   node tools/i18n_conformance_gen.mjs --tables
//        prints the per-language data tables (separators, list patterns, plural categories,
//        time/date templates) as JSON, from which the Python literals in i18n.py were written.
//
// The fixture "expected" values are FormatJS 10.7.16 / Intl output; the Python renderer and CAI18N.tw must equal them.
// The typed wire params (temp, tdelta, time, date, dur, list, msg, code) are decoded HERE by a third, independent
// implementation of the contract (see decode() below): Intl for non-English numbers/lists/times, a BigInt exact
// round-half-even for English temperatures (Python's legacy f-string), and the per-key fallback rules of
// docs/i18n-spec.md. Row shapes (all rows: lang, params, expected):
//   {msg}                        an inline ICU pattern (installed as the catalog key "conformance.row" of lang)
//   {key, catalogs}              render a catalog key from the inline catalogs {lang: {key: pattern}}
//   {msg|key, catalogs}          both: the catalogs also serve dur/msg/code params
//   strict: false                Python must run the row with strict mode off (a bad parameter / fallback row)
//   params may hold {"$f": "nan"|"inf"|"-inf"} (fixture-only float literals, JSON has no NaN); runners revive them.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const argVal = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : null; };
const imfPath = argVal('--imf') || path.join(ROOT, 'custom_components/climate_advisor/frontend/intl-messageformat.iife.js');
vm.runInThisContext(fs.readFileSync(imfPath, 'utf8'));
const IMF = globalThis.IntlMessageFormat.IntlMessageFormat;

// The one source of the shipped language list (Python and JS check their own copies against this file).
const INDEX = JSON.parse(fs.readFileSync(path.join(ROOT, 'custom_components/climate_advisor/frontend/i18n/index.json'), 'utf8'));
const LANGS = INDEX.languages;
const SYMBOL = { fahrenheit: '°F', celsius: '°C' };
const BAD = '—';

// Python's f"{x:.{d}f}" computed with exact integer arithmetic: round half to even on the exact binary value of x.
function exactHalfEvenFixed(x, d) {
  const neg = x < 0 || Object.is(x, -0);
  const dv = new DataView(new ArrayBuffer(8));
  dv.setFloat64(0, Math.abs(x));
  const hi = dv.getUint32(0), lo = dv.getUint32(4), exp = (hi >>> 20) & 0x7ff;
  let mant = (BigInt(hi & 0xfffff) << 32n) | BigInt(lo), e;
  if (exp === 0) e = -1074; else { mant |= 1n << 52n; e = exp - 1075; }
  let num = mant * 10n ** BigInt(d), den = 1n;
  if (e >= 0) num <<= BigInt(e); else den <<= BigInt(-e);
  let q = num / den;
  const twice = (num % den) * 2n;
  if (twice > den || (twice === den && (q & 1n) === 1n)) q += 1n;
  const s = q.toString().padStart(d + 1, '0');
  return (neg ? '-' : '') + s.slice(0, s.length - d) + (d > 0 ? '.' + s.slice(s.length - d) : '');
}
const roundHalfEven = (x) => { const f = Math.floor(x), r = x - f; return r > 0.5 ? f + 1 : r < 0.5 ? f : (f % 2 === 0 ? f : f + 1); };
const norm = (s) => s.replace(/[  ]/g, ' ');
const FLOATS = { nan: NaN, inf: Infinity, '-inf': -Infinity };
function revive(v) {
  if (Array.isArray(v)) return v.map(revive);
  if (v && typeof v === 'object') {
    if ('$f' in v) return FLOATS[v.$f];
    return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, revive(x)]));
  }
  return v;
}

function wall(ts) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(ts);
  if (!m) return null;
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]));
  return Number.isNaN(new Date(ts).getTime()) || d.getUTCDate() !== +m[3] || d.getUTCHours() !== +m[4] ? null : d;
}

function decode(p, lang, cats) {
  if (typeof p === 'string' || typeof p === 'number') return p;
  if (p === null || typeof p !== 'object' || Array.isArray(p) || !p.$t) return BAD;
  switch (p.$t) {
    case 'temp': case 'tdelta': {
      if (typeof p.f !== 'number') return BAD;
      const d = Number.isInteger(p.d) && p.d >= 0 && p.d <= 20 ? p.d : 0;
      let v = p.f;
      if (p.unit === 'celsius') v = p.$t === 'temp' ? (v - 32.0) * 5.0 / 9.0 : v * 5.0 / 9.0;
      const sym = SYMBOL[p.unit] || '°F';
      if (!Number.isFinite(v)) return new Intl.NumberFormat(lang, { useGrouping: false }).format(v) + sym;
      if (lang === 'en') return exactHalfEvenFixed(v, d) + sym;   // legacy f-string: half-even on the exact value
      return new Intl.NumberFormat(lang, { minimumFractionDigits: d, maximumFractionDigits: d, useGrouping: false }).format(v) + sym;
    }
    case 'list': {
      if (!Array.isArray(p.items)) return BAD;
      const items = p.items.map((i) => {
        const x = decode(i, lang, cats);
        return typeof x === 'number' ? new Intl.NumberFormat(lang).format(x) : x;
      });
      return new Intl.ListFormat(lang, { type: p.style === 'or' ? 'disjunction' : 'conjunction', style: 'long' }).format(items);
    }
    case 'time': case 'date': {
      const w = typeof p.ts === 'string' ? wall(p.ts) : null;
      if (!w) return BAD;
      const o = p.$t === 'time' ? { hour: 'numeric', minute: '2-digit' } : { month: 'short', day: 'numeric' };
      return norm(new Intl.DateTimeFormat(lang, { ...o, timeZone: 'UTC' }).format(w));
    }
    case 'dur': {
      if (typeof p.s !== 'number' || !Number.isFinite(p.s)) return BAD;
      const mins = Math.max(1, roundHalfEven(p.s / 60));
      if (mins >= 60 && mins % 60 === 0) return renderKey(lang, 'common.duration.hours', { n: mins / 60 }, cats);
      if (mins >= 60) return renderKey(lang, 'common.duration.hours_frac', { n: Number(exactHalfEvenFixed(mins / 60, 1)) }, cats);
      return renderKey(lang, 'common.duration.min', { n: mins }, cats);
    }
    case 'msg': return renderKey(lang, p.key, p.params, cats);
    case 'code': return typeof p.key === 'string' ? renderKey(lang, p.key, null, cats) : BAD;
    default: return BAD;
  }
}

function checkArgTypes(nodes, params) {
  for (const n of nodes) {
    if ((n.type === 2 || n.type === 6) && typeof params[n.value] !== 'number') throw new Error('not a number: ' + n.value);
    if (n.options) for (const o of Object.values(n.options)) checkArgTypes(o.value, params);
  }
}
function tryFormat(pattern, lang, params, cats) {
  const decoded = {};
  for (const [k, v] of Object.entries(params || {})) decoded[k] = decode(v, lang, cats);
  try {
    const inst = new IMF(pattern, lang);
    checkArgTypes(inst.getAst(), decoded);
    return String(inst.format(decoded));
  } catch { return null; }
}
// docs/i18n-spec.md "Fallbacks": a key missing in the language uses the English pattern with the REQUESTED language's
// formats; a translation that fails falls back to the English message in English; else the key.
function renderKey(lang, key, params, cats) {
  if (typeof key !== 'string' || !key) return '';
  let pattern = cats?.[lang]?.[key];
  if (pattern === undefined && lang !== 'en') pattern = cats?.en?.[key];
  if (pattern === undefined) return key;
  let out = tryFormat(pattern, lang, params, cats);
  if (out !== null) return out;
  const english = lang !== 'en' ? cats?.en?.[key] : undefined;
  if (english !== undefined) { out = tryFormat(english, 'en', params, cats); if (out !== null) return out; }
  return key;
}

const KEY = 'conformance.row';
function oracle(row) {
  const cats = {};
  for (const [l, m] of Object.entries(row.catalogs || {})) cats[l] = { ...m };
  if (row.msg !== undefined) cats[row.lang] = { ...(cats[row.lang] || {}), [KEY]: row.msg };
  return renderKey(row.lang, row.key === undefined ? KEY : row.key, revive(row.params), cats);
}

// ---------------------------------------------------------------- tables mode
function tables() {
  const out = {};
  const sep = (items) => {
    const lf = (t) => new Intl.ListFormat(t.lang, { type: t.type, style: 'long' });
    return (lang, type) => {
      const f = lf({ lang, type });
      const mk = (n) => Array.from({ length: n }, (_, i) => String.fromCharCode(1 + i));
      const split = (s, n) => { const parts = []; let rest = s; const marks = mk(n); for (const m of marks) { const idx = rest.indexOf(m); parts.push(rest.slice(0, idx)); rest = rest.slice(idx + 1); } parts.push(rest); return parts; };
      const two = split(f.format(mk(2)), 2);
      const three = split(f.format(mk(3)), 3);
      const four = split(f.format(mk(4)), 4);
      return { pair: two[1], start: four[1], middle: four[2], end: four[3], _three: three[2], _pre: two[0], _post: two[2] };
    };
  };
  const listSeps = sep();
  for (const lang of LANGS) {
    const nf = new Intl.NumberFormat(lang);
    const parts = new Intl.NumberFormat(lang, { minimumFractionDigits: 1 }).formatToParts(-1234567.5);
    const decimal = parts.find((p) => p.type === 'decimal').value;
    const group = parts.find((p) => p.type === 'group').value;
    const minus = parts.find((p) => p.type === 'minusSign').value;
    const pct = new Intl.NumberFormat(lang, { style: 'percent' }).format(0.5).replace('50', '{}');
    let minGrouping = 1;
    if (nf.format(1000) === '1000') minGrouping = (nf.format(10000) !== '10000') ? 2 : 99;
    const indian = nf.format(1234567) !== nf.format(1234567).replace(/,(\d{2}),/, ',$1,') ? false : /\d,\d\d,\d{3}$/.test(nf.format(1234567));
    const conj = listSeps(lang, 'conjunction');
    const disj = listSeps(lang, 'disjunction');
    const dtf = new Intl.DateTimeFormat(lang, { hour: 'numeric', minute: '2-digit' });
    const norm = (s) => s.replace(/[  ]/g, ' ');
    const tmpl = (d, f, map) => norm(f.formatToParts(d).map((p) => (map[p.type] ? map[p.type] : p.value)).join(''));
    const pm = dtf.formatToParts(new Date(2026, 10, 14, 15, 5)).find((p) => p.type === 'dayPeriod');
    const am = dtf.formatToParts(new Date(2026, 10, 14, 9, 5)).find((p) => p.type === 'dayPeriod');
    const months = [];
    const dfm = new Intl.DateTimeFormat(lang, { month: 'short', day: 'numeric' });
    for (let m = 0; m < 12; m++) months.push(tmpl(new Date(2026, m, 14), dfm, { day: '{d}' }));
    out[lang] = {
      decimal, group, minus, nan: nf.format(NaN), percent: pct, min_grouping: minGrouping, indian,
      list_and: { pair: conj.pair, start: conj.start, middle: conj.middle, end: conj.end },
      list_or: { pair: disj.pair, start: disj.start, middle: disj.middle, end: disj.end },
      plural_categories: new Intl.PluralRules(lang).resolvedOptions().pluralCategories.slice().sort(),
      hour_cycle: dtf.resolvedOptions().hourCycle,
      time: tmpl(new Date(2026, 10, 14, 15, 5), dtf, { hour: '{h}', minute: '{mm}', dayPeriod: '{p}' }),
      am: am ? norm(am.value) : null, pm: pm ? norm(pm.value) : null,
      date_by_month: months,
    };
  }
  process.stdout.write(JSON.stringify(out, null, 1) + '\n');
}

// ---------------------------------------------------------------- fixture rows
const rows = [];
const add = (lang, msg, params = {}, extra = {}) => rows.push({ lang, msg, params, ...extra });

// plural: every CLDR category in each language
const PLURAL_N = [0, 1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, 21, 22, 25, 100, 101, 102, 103, 111, 112, 1.5, 2.5, 0.5, 1000000];
const ALLCAT = '{n, plural, zero {zero:#} one {one:#} two {two:#} few {few:#} many {many:#} other {other:#}}';
for (const lang of LANGS) for (const n of PLURAL_N) add(lang, ALLCAT, { n });
for (const lang of ['en', 'fr', 'ar', 'ru', 'pl']) {
  for (const n of [0, 1, 2, 5]) add(lang, '{n, plural, =0 {none} =1 {exactly one} one {# item} other {# items}}', { n });
}
// select + other
for (const lang of ['fr', 'es', 'de', 'en']) {
  for (const g of ['f', 'm', 'x']) add(lang, '{g, select, f {la fenetre} m {le ventilateur} other {la chose}} {name}', { g, name: 'Salon' });
}
// nested select in plural; # is literal inside the select (FormatJS)
for (const lang of ['en', 'fr', 'ru', 'ar', 'pl', 'ja']) {
  for (const n of [1, 2, 5]) for (const g of ['f', 'z']) {
    add(lang, '{n, plural, one {{g, select, f {# F} other {# O}}} other {{g, select, f {# Fs (#)} other {# Os}}}}', { n, g });
  }
}
// apostrophes
for (const lang of ['fr', 'it', 'uk', 'en']) {
  add(lang, "L''hiver, c''est {n, plural, one {# jour} other {# jours}}", { n: 1 });
  add(lang, "L''hiver, c''est {n, plural, one {# jour} other {# jours}}", { n: 3 });
  add(lang, "l'ete {x} dell'aria", { x: 'v' });
  add(lang, "'{'literal'}' et {x}", { x: 'v' });
  add(lang, "'{x}' and ''{x}''", { x: 'v' });
  add(lang, "{n, plural, one {'#' # it's} other {'#' # ''x''}}", { n: 5 });
  add(lang, "{n, plural, one {'#' # it's} other {'#' # ''x''}}", { n: 1 });
  add(lang, "rock 'n' roll {x} ''''", { x: 'v' });
  add(lang, "'{abc", {});
  add(lang, "a '' b '}' c } d", {});
  add(lang, "# not plural here {x}", { x: 'v' });
  add(lang, "'<' and 5 < 6 {x}", { x: 'v' });
}
// number + skeletons
for (const lang of LANGS) {
  add(lang, '{n, number}', { n: 1234567.891 });
  add(lang, '{n, number}', { n: 1234 });
  add(lang, '{n, number}', { n: 12345 });
  add(lang, '{n, number}', { n: 12345678.5 });
  add(lang, '{n, number}', { n: -1234.5 });
  add(lang, '{n, number}', { n: 0.0005 });
  add(lang, '{n, number, ::percent}', { n: 0.256 });
  add(lang, '{n, number, ::percent}', { n: -0.5 });
  add(lang, '{n, number, ::percent}', { n: 12.345 });
  add(lang, '{n, number, ::precision-integer}', { n: 3.6 });
  add(lang, '{n, number, ::precision-integer}', { n: 12345.4 });
  add(lang, '{n, number, ::.0}', { n: 3.26 });
  add(lang, '{n, number, ::.0}', { n: 1234.5 });
  add(lang, '{n, number, ::.00}', { n: 3.256 });
  add(lang, '{n, number, ::.00}', { n: 7 });
  add(lang, '{n, number, ::.00}', { n: -1234.5 });
}
// plain numeric arguments are String(n) in FormatJS (no locale formatting)
for (const lang of ['en', 'fr', 'ar']) { add(lang, '{n} / {m}', { n: 1234.5, m: 3 }); }
// lists
for (const lang of LANGS) for (const style of ['and', 'or']) for (const k of [1, 2, 3, 4]) {
  add(lang, 'x {l}', { l: { $t: 'list', items: ['A', 'B', 'C', 'D'].slice(0, k), style } });
}
// temperatures and deltas
for (const lang of LANGS) {
  for (const [f, unit, d] of [[72.3, 'fahrenheit', 0], [72.3, 'fahrenheit', 1], [72.3, 'celsius', 0], [72.3, 'celsius', 1], [72.3, 'celsius', 2],
    [-4.1, 'celsius', 1], [1234.56, 'fahrenheit', 1], [20.4, 'fahrenheit', 0]]) {
    add(lang, '{t}', { t: { $t: 'temp', f, unit, d } });
    add(lang, '{t}', { t: { $t: 'tdelta', f, unit, d } });
  }
  add(lang, 'a {t} b {n}', { t: { $t: 'temp', f: 68.3, unit: 'celsius', d: 1 }, n: 'z' });
}


// ---- Stage 1 review fixes (S2/S3/S4): typed params, fallbacks, bad params, NaN/inf, times and dates ----
const nonStrict = { strict: false };
const addKey = (lang, key, catalogs, params = {}, extra = {}) => rows.push({ lang, key, params, catalogs, ...extra });
const F = (v) => ({ $f: v });

// NaN / infinity: number formats, plural operands and plain {n} (Intl text; "other" plural category)
for (const lang of LANGS) {
  for (const v of ['nan', 'inf', '-inf']) {
    add(lang, '{n, number}', { n: F(v) });
    add(lang, '{n, number, ::percent}', { n: F(v) });
    add(lang, '{n, number, ::.0}', { n: F(v) });
    add(lang, '{n, plural, one {o #} other {x #}}', { n: F(v) });
    add(lang, '{n}', { n: F(v) });
  }
}
for (const lang of LANGS) add(lang, '{n, number}', { n: 1e300 });

// list items that are numbers use the locale number format (Python's ListOf does)
for (const lang of LANGS) for (const style of ['and', 'or']) {
  add(lang, '{l}', { l: { $t: 'list', items: [1234.5, 'x', 3, 1000000], style } });
}

// English temperatures are Python's legacy f-string: round half to even on the exact binary value.
// "expect" values are hand-authored from CPython's f"{x:.{d}f}" (format_temp) and cross-checked by the oracle below.
const ties = [
  [0.5, 'fahrenheit', 0, '0°F'], [1.5, 'fahrenheit', 0, '2°F'], [2.5, 'fahrenheit', 0, '2°F'], [72.5, 'fahrenheit', 0, '72°F'],
  [73.5, 'fahrenheit', 0, '74°F'], [72.25, 'fahrenheit', 1, '72.2°F'], [72.75, 'fahrenheit', 1, '72.8°F'],
  [0.125, 'fahrenheit', 2, '0.12°F'], [0.375, 'fahrenheit', 2, '0.38°F'], [1.005, 'fahrenheit', 2, '1.00°F'],
  [2.675, 'fahrenheit', 2, '2.67°F'], [-2.5, 'fahrenheit', 0, '-2°F'], [-0.5, 'fahrenheit', 0, '-0°F'],
  [-0.04, 'fahrenheit', 1, '-0.0°F'],
];
for (const [f, unit, d, expect] of ties) add('en', '{t}', { t: { $t: 'temp', f, unit, d } }, { expect });
add('en', '{t}', { t: { $t: 'tdelta', f: 4.5, unit: 'celsius', d: 0 } }, { expect: '2°C' });
add('en', '{t}', { t: { $t: 'tdelta', f: 13.5, unit: 'celsius', d: 0 } }, { expect: '8°C' });
// every other language rounds half away from zero on the shortest decimal (Intl)
for (const lang of ['de', 'fr', 'ja', 'ar']) {
  for (const [f, unit, d] of [[2.5, 'fahrenheit', 0], [0.125, 'fahrenheit', 2], [1.005, 'fahrenheit', 2], [-2.5, 'fahrenheit', 0]]) {
    add(lang, '{t}', { t: { $t: 'temp', f, unit, d } });
  }
}
// NaN / infinity temperatures print as Intl does, in every language
for (const lang of LANGS) {
  add(lang, '{t}', { t: { $t: 'temp', f: F('nan'), unit: 'celsius', d: 1 } });
  add(lang, '{t}', { t: { $t: 'tdelta', f: F('-inf'), unit: 'fahrenheit', d: 0 } });
}

// durations: half-even minutes and tenths of an hour, from inline common.duration.* catalogs
const DUR_EN = {
  'common.duration.hours': '{n, plural, one {#h} other {#h}}', 'common.duration.hours_frac': '{n, number, ::.0}h',
  'common.duration.min': '{n, plural, one {# min} other {# min}}',
};
const DUR_FR = {
  'common.duration.hours': '{n, plural, one {# h} other {# h}}', 'common.duration.hours_frac': '{n, number, ::.0} h',
  'common.duration.min': '{n, plural, one {# min} other {# min}}',
};
for (const lang of ['en', 'fr', 'de']) {
  for (const s of [0, 30, 45, 90, 150, 300, 3600, 4500, 5400, 6300, 7200, 8100, 9000, 10800, 86400, 90000]) {
    addKey(lang, 'conformance.row', { en: { ...DUR_EN, 'conformance.row': 'in {d}' }, fr: { ...DUR_FR, 'conformance.row': 'dans {d}' } },
      { d: { $t: 'dur', s } });
  }
}
// 75 and 135 minutes: 1.25h and 2.25h are exact ties, Python's f"{x:.1f}" gives 1.2 and 2.2
for (const s of [75 * 60, 135 * 60, 105 * 60, 165 * 60]) {
  addKey('en', 'conformance.row', { en: { ...DUR_EN, 'conformance.row': '{d}' } }, { d: { $t: 'dur', s } });
}
// per-key fallback: the English pattern is formatted with the REQUESTED language's number, plural and list formats
const FB = { 'conformance.row': '{n, plural, one {# item} other {# items}} / {n, number} / {l} / {t}' };
for (const lang of ['fr', 'de', 'ru', 'ar', 'pl', 'ja']) {
  for (const n of [1, 1.5, 2.5, 1234.5]) {
    addKey(lang, 'conformance.row', { en: FB }, { n, l: { $t: 'list', items: ['a', 'b', 3], style: 'and' }, t: { $t: 'temp', f: 70.25, unit: 'celsius', d: 1 } }, nonStrict);
  }
}
// a broken translation falls back to the English message, rendered in English (parameters decoded in English)
const T1 = { $t: 'temp', f: 70.25, unit: 'celsius', d: 1 };
addKey('de', 'conformance.row', { en: { 'conformance.row': '{n} items ({t})' }, de: { 'conformance.row': '{n, plural, one {# x} other {# y' } }, { n: 1234.5, t: T1 }, nonStrict);
addKey('fr', 'conformance.row', { en: { 'conformance.row': '{n} items ({t})' }, fr: { 'conformance.row': '{n} {missing_arg}' } }, { n: 1234.5, t: T1 }, nonStrict);
addKey('ru', 'conformance.row', { en: { 'conformance.row': '{n, plural, one {# item} other {# items}}' }, ru: { 'conformance.row': '{n, plural, one {# a} few {# b}}' } }, { n: 3 }, nonStrict);
// a broken translation and a broken English message: the key
addKey('de', 'conformance.row', { en: { 'conformance.row': '{n, plural, one {#' }, de: { 'conformance.row': '{n, plural, one {#' } }, { n: 3 }, nonStrict);
addKey('en', 'conformance.row', { en: { 'conformance.row': '{n, plural, one {# a}' } }, { n: 3 }, nonStrict);
// a key missing everywhere: the key
addKey('fr', 'common.nowhere', { en: {} }, { n: 3 }, nonStrict);
// a message with no key renders as nothing (wire without a key)
addKey('de', '', { en: {} }, {}, nonStrict);

// parameters that cannot be rendered show as an em dash (Python: strict mode raises, non-strict shows it)
const BADS = {
  temp_none: { $t: 'temp', f: null, unit: 'celsius', d: 1 }, temp_str: { $t: 'temp', f: '72', unit: 'fahrenheit', d: 0 },
  temp_missing: { $t: 'temp', unit: 'celsius' }, tdelta_none: { $t: 'tdelta', f: null, unit: 'celsius', d: 0 },
  time_garbage: { $t: 'time', ts: 'garbage' }, time_missing: { $t: 'time' }, time_number: { $t: 'time', ts: 5 },
  time_overflow: { $t: 'time', ts: '2026-13-45T25:61:00+00:00' }, date_garbage: { $t: 'date', ts: 'nope' }, date_missing: { $t: 'date' },
  dur_none: { $t: 'dur', s: null }, dur_str: { $t: 'dur', s: '300' }, dur_missing: { $t: 'dur' },
  list_missing: { $t: 'list', style: 'and' }, list_not_array: { $t: 'list', items: 'ab' },
  unknown_type: { $t: 'nope', x: 1 }, marked_bad: { $t: 'bad' }, code_missing: { $t: 'code' },
  plain_null: null, plain_true: true, plain_array: [1, 2], plain_object: { a: 1 },
};
for (const lang of ['en', 'de', 'ja']) {
  for (const [name, p] of Object.entries(BADS)) add(lang, `${name}: {x}`, { x: p }, nonStrict);
  add(lang, 'items: {l}', { l: { $t: 'list', items: ['a', null, 5, { $t: 'temp', f: null }, true], style: 'and' } }, nonStrict);
  add(lang, 'a {x} b {y}', { x: { $t: 'dur', s: null }, y: { $t: 'time', ts: 'garbage' } }, nonStrict);
}
// a bad parameter in a number / plural position is a render error: the English message, then the key
for (const lang of ['en', 'de']) {
  for (const p of [null, { $t: 'dur', s: null }, 'five', true]) {
    add(lang, '{n, plural, one {# item} other {# items}}', { n: p }, nonStrict);
    add(lang, '{n, number}', { n: p }, nonStrict);
  }
}
// nested messages and codes with malformed wire
addKey('de', 'conformance.row', { en: { 'conformance.row': 'a {m} b {c}', 'common.x': 'X {n}' }, de: { 'common.x': 'X-de {n}' } },
  { m: { $t: 'msg', key: 'common.x', params: { n: 3 } }, c: { $t: 'code', key: 'common.x' } }, nonStrict);
addKey('de', 'conformance.row', { en: { 'conformance.row': 'a {m} b' } }, { m: { $t: 'msg', params: { n: 3 } } }, nonStrict);
addKey('de', 'conformance.row', { en: { 'conformance.row': 'a {m} b', 'common.x': 'X' } }, { m: { $t: 'msg', key: 'common.x', params: 'junk' } }, nonStrict);

// times and dates: ts is an absolute instant with an offset; with no hass the wall clock written in ts is shown
const STAMPS = ['2026-11-14T15:05:00-05:00', '2026-11-14T00:05:00+09:00', '2026-03-01T12:00:00+00:00', '2026-01-05T07:30:00-08:00',
  '2026-12-31T23:59:00+01:00', '2026-07-04T09:00:00+05:30'];
for (const lang of LANGS) for (const ts of STAMPS) {
  add(lang, 't={t} d={d}', { t: { $t: 'time', ts }, d: { $t: 'date', ts } });
}
for (let m = 1; m <= 12; m++) {
  const ts = `2026-${String(m).padStart(2, '0')}-14T13:45:00+00:00`;
  for (const lang of LANGS) add(lang, '{d}', { d: { $t: 'date', ts } });
}

function fixture() {
  const out = rows.map((r) => {
    const { expect, ...row } = r;
    const expected = oracle(row);
    if (expect !== undefined && expect !== expected) throw new Error(`hand-authored expectation ${expect} != oracle ${expected} for ${JSON.stringify(row)}`);
    return { ...row, expected };
  });
  const header = {
    generator: 'tools/i18n_conformance_gen.mjs', node: process.version, icu: process.versions.icu,
    cldr: process.versions.cldr, unicode: process.versions.unicode, intl_messageformat: '10.7.16', rows: out.length,
  };
  fs.writeFileSync(path.join(ROOT, 'tests/fixtures/i18n_conformance.json'), JSON.stringify({ header, rows: out }, null, 1) + '\n');

  const CAT = { zero: 'z', one: 'o', two: 't', few: 'f', many: 'm', other: 'x' };
  const decimals = [0.0, 0.5, 1.0, 1.5, 2.5, 10.1, 100.25];
  const ints = {}; const decs = {};
  for (const lang of LANGS) {
    const pr = new Intl.PluralRules(lang);
    let s = ''; for (let n = 0; n <= 1000; n++) s += CAT[pr.select(n)];
    ints[lang] = s;
    decs[lang] = Object.fromEntries(decimals.map((d) => [String(d), CAT[pr.select(d)]]));
  }
  const cats = Object.fromEntries(LANGS.map((l) => [l, new Intl.PluralRules(l).resolvedOptions().pluralCategories.slice().sort()]));
  const ph = { ...header, rows: undefined, letters: CAT };
  fs.writeFileSync(path.join(ROOT, 'tests/fixtures/i18n_plural_table.json'),
    JSON.stringify({ header: ph, categories: cats, integers_0_to_1000: ints, decimals: decs }, null, 1) + '\n');
  console.log(`wrote ${out.length} rows`);
}

if (args.includes('--tables')) tables(); else fixture();
