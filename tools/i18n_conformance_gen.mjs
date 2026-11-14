// Generates the i18n conformance fixtures from FormatJS + Intl in Node (Issue #1341).
//
//   node tools/i18n_conformance_gen.mjs [--imf <path to intl-messageformat.iife.js>]
//        writes tests/fixtures/i18n_conformance.json and tests/fixtures/i18n_plural_table.json
//   node tools/i18n_conformance_gen.mjs --tables
//        prints the per-language data tables (separators, list patterns, plural categories,
//        time/date templates) as JSON, from which the Python literals in i18n.py were written.
//
// The fixture "expected" values are FormatJS 10.7.16 / Intl output; the Python renderer must equal them.
// Typed wire params in the fixture rows are decoded here exactly as the Python renderer and CAI18N.tw
// must decode them (see decode() below).
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

const LANGS = ['en', 'fr', 'es', 'de', 'nl', 'ja', 'pt', 'it', 'pl', 'uk', 'ko', 'hi', 'ar', 'ru', 'zh-Hans', 'tr'];
const SYMBOL = { fahrenheit: '°F', celsius: '°C' };

function decode(p, lang) {
  if (p === null || typeof p !== 'object') return p;
  switch (p.$t) {
    case 'temp': case 'tdelta': {
      let v = p.f;
      if (p.unit === 'celsius') v = p.$t === 'temp' ? (v - 32.0) * 5.0 / 9.0 : v * 5.0 / 9.0;
      const nf = new Intl.NumberFormat(lang, { minimumFractionDigits: p.d, maximumFractionDigits: p.d, useGrouping: false });
      return nf.format(v) + SYMBOL[p.unit];
    }
    case 'list':
      return new Intl.ListFormat(lang, { type: p.style === 'or' ? 'disjunction' : 'conjunction', style: 'long' })
        .format(p.items.map((i) => decode(i, lang)));
    default:
      throw new Error('unsupported typed param in fixture: ' + JSON.stringify(p));
  }
}

function render(lang, msg, params) {
  const decoded = {};
  for (const [k, v] of Object.entries(params)) decoded[k] = decode(v, lang);
  return new IMF(msg, lang).format(decoded);
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
      decimal, group, minus, percent: pct, min_grouping: minGrouping, indian,
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
const add = (lang, msg, params = {}) => rows.push({ lang, msg, params });

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

function fixture() {
  const out = rows.map((r) => ({ ...r, expected: render(r.lang, r.msg, r.params) }));
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
