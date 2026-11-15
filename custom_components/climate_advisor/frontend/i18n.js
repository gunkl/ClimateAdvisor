/* Issue #1341: dashboard i18n runtime (window.CAI18N). Spec: docs/i18n-spec.md.
   Design adapted from lboue's closed PR #1562 (a t() with per-key English fallback, data-i18n style helpers and a
   ?lang= override), thanks. Different here: catalogs are JSON files shared with the Python side (frontend/i18n/),
   messages are ICU MessageFormat rendered by the bundled FormatJS (intl-messageformat.iife.js, load it first), and
   the language comes from Home Assistant, not only the browser.

   Loaded FIRST on new_dashboard.html, entry.html and prediction_accuracy.html (never index.html: classic stays
   English). A failed catalog fetch never breaks a page: init() always resolves; t() then falls back to English,
   then to the key itself. */
(function (root) {
  'use strict';

  // Same 16 codes as frontend/i18n/index.json (the file wins when it loads; these are the offline fallback).
  var LANGUAGES = ['en', 'fr', 'es', 'de', 'nl', 'ja', 'pt', 'it', 'pl', 'uk', 'ko', 'hi', 'ar', 'ru', 'zh-Hans', 'tr'];
  var AREAS = ['common', 'notify', 'status', 'timeline', 'learning', 'night', 'activity', 'briefing', 'dashboard',
    'settings', 'errors', 'ai'];
  var RTL = ['ar'];
  // #1341: the page layout flips to right-to-left (<html dir="rtl"> for ar) because the dashboard CSS uses logical
  // properties (the physical_css ratchet is 0 outside the classic index.html, which is never right-to-left). Setting
  // this false keeps <html dir> ltr for every language while CAI18N.dir still reports 'rtl' for text code.
  var RTL_LAYOUT_READY = true;
  var FETCH_TIMEOUT_MS = 8000;

  var state = {
    languages: LANGUAGES.slice(),
    areas: AREAS.slice(),
    cat: {},          // lang -> { key: ICU string }
    compiled: {},     // lang + NUL + key -> IntlMessageFormat instance (or null when it will not parse)
    warned: {},       // one console.warn per missing key
    base: 'i18n/'
  };

  function warnOnce(id, msg) {
    if (state.warned[id]) return;
    state.warned[id] = true;
    try { console.warn('CAI18N: ' + msg); } catch (e) { /* no console */ }
  }

  // ---------- language resolution (same rules as i18n.resolve_language in Python) ----------
  function resolveLanguage(code, languages) {
    var langs = languages || state.languages;
    if (!code || typeof code !== 'string') return 'en';
    code = code.trim().replace(/_/g, '-');
    if (!code) return 'en';
    if (langs.indexOf(code) >= 0) return code;
    var lower = code.toLowerCase(), i;
    for (i = 0; i < langs.length; i++) if (langs[i].toLowerCase() === lower) return langs[i];
    var parts = lower.split('-');
    if (parts[0] === 'zh') {
      // zh, zh-CN, zh-SG, zh-Hans-* (any region) -> Simplified. zh-TW / zh-HK / zh-Hant -> en until Traditional ships.
      // An explicit Hans script wins over any region (zh-Hans-TW is Simplified). Same as i18n.resolve_language.
      var rest = parts.slice(1), hans = rest.indexOf('hans') >= 0;
      var hant = ['hant', 'tw', 'hk', 'mo'].some(function (m) { return rest.indexOf(m) >= 0; });
      if (!hans && hant) return 'en';
      return langs.indexOf('zh-Hans') >= 0 ? 'zh-Hans' : 'en';
    }
    for (i = 0; i < langs.length; i++) if (langs[i].toLowerCase() === parts[0]) return langs[i];
    return 'en';
  }

  // The one lookup of Home Assistant's own state from this frame: the parent (the panel frame) or the top window.
  function getHass() {
    var wins = [], i;
    try { wins.push(root.parent); } catch (e) { /* ignore */ }
    try { wins.push(root.top); } catch (e) { /* ignore */ }
    for (i = 0; i < wins.length; i++) {
      try {
        var ha = wins[i].document.querySelector('home-assistant');
        if (ha && ha.hass) return ha.hass;
      } catch (e) { /* cross-origin or not ready */ }
    }
    return null;
  }

  function queryLang() {
    try {
      var v = new URLSearchParams(root.location.search).get('lang');
      return v || null;
    } catch (e) { return null; }
  }

  // Order: ?lang= -> parent hass.language -> status payload language -> navigator.language -> en.
  // The first candidate that is a non-empty string wins (an unsupported one resolves to its nearest shipped
  // language, or en: the dashboard never mixes two languages).
  function pickLanguage(statusLanguage) {
    var q = queryLang();
    if (q && String(q).toLowerCase() === 'en-xa') return PSEUDO; // pseudo-locale: only ever from ?lang=
    var hass = getHass();
    var cands = [
      queryLang(),
      hass && hass.language,
      statusLanguage,
      root.navigator && (root.navigator.language || (root.navigator.languages && root.navigator.languages[0]))
    ];
    for (var i = 0; i < cands.length; i++) {
      if (cands[i] && typeof cands[i] === 'string') return resolveLanguage(cands[i]);
    }
    return 'en';
  }

  // ---------- escaping ----------
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // A dynamic Latin name (entity id, model, repo) inside right-to-left text: isolated with U+2068..U+2069, as the Python
  // renderer does for ar. The one helper every page module uses.
  function iso(x) { return root.CAI18N && root.CAI18N.dir === 'rtl' ? '\u2068' + x + '\u2069' : String(x); }

  // ---------- formatters ----------
  // Intl.* based, and byte-identical to the Python formatters in i18n.py (the shared conformance fixture proves
  // it row by row), EXCEPT where noted: plain {n} numbers are String(n), not locale formatted.
  // Anything that cannot be rendered (None, NaN seconds, a non-datetime, malformed wire) shows as BAD, like Python.
  var BAD = '—';

  function L(lang) { return lang || CAI18N.lang || 'en'; }
  // Intl and FormatJS need a real locale: the pseudo-locale formats numbers and dates as English.
  function IL(lang) { lang = L(lang); return lang === 'en-XA' ? 'en' : lang; }

  function number(x, opts, lang) {
    try { return new Intl.NumberFormat(IL(lang), opts || {}).format(x); } catch (e) { return String(x); }
  }

  // Python's f"{x:.{d}f}": round half to even on the exact binary value of x (not on its shortest decimal text).
  // Needed for English temperatures, where the legacy f-string output is the contract; Intl rounds half away from
  // zero on the shortest decimal ("1.005" -> 1.01) and would differ on exact ties and near-ties.
  function incrementDigits(s) {
    var a = s.split(''), i = a.length - 1;
    while (i >= 0) {
      if (a[i] === '9') { a[i] = '0'; i--; } else { a[i] = String.fromCharCode(a[i].charCodeAt(0) + 1); break; }
    }
    return (i < 0 ? '1' : '') + a.join('');
  }
  function fixedHalfEven(x, d) {
    var neg = x < 0 || (x === 0 && 1 / x < 0), a = Math.abs(x), s;
    s = a >= 1e21 ? BigInt(a).toString() : a.toFixed(100); // toFixed expands the exact binary value
    var dot = s.indexOf('.');
    var ip = dot < 0 ? s : s.slice(0, dot), fp = dot < 0 ? '' : s.slice(dot + 1);
    while (fp.length < d + 1) fp += '0';
    var keep = ip + fp.slice(0, d), next = fp.charCodeAt(d) - 48, rest = fp.slice(d + 1);
    var odd = (keep.charCodeAt(keep.length - 1) - 48) % 2 === 1;
    if (next > 5 || (next === 5 && (/[1-9]/.test(rest) || odd))) keep = incrementDigits(keep);
    var cut = keep.length - d;
    return (neg ? '-' : '') + keep.slice(0, cut) + (d > 0 ? '.' + keep.slice(cut) : '');
  }

  function toDate(d) { return d instanceof Date ? d : new Date(d); }

  // Which zone clock times are shown in, following Home Assistant's own per-user setting (profile > time zone):
  //   an IANA id  - "server" (the HA default): hass.config.time_zone
  //   'local'     - "local": the browser's own zone
  //   null        - no hass reachable (or no zone known): the wall clock written in the wire ts is shown as is
  function timeZone() {
    var h = getHass();
    if (!h) return null;
    var loc = h.locale || CAI18N.locale;
    if (loc && loc.time_zone === 'local') return 'local';
    var tz = h.config && h.config.time_zone;
    return typeof tz === 'string' && tz ? tz : null;
  }
  // "2026-11-14T15:05:00-05:00" -> the wall clock written in the string, as a UTC-shifted Date (null if not ISO)
  function wallClock(s) {
    var m = /^\s*(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(s);
    if (!m) return null;
    var y = +m[1], mo = +m[2], d = +m[3], h = +(m[4] || 0), mi = +(m[5] || 0);
    var dt = new Date(Date.UTC(y, mo - 1, d, h, mi));
    if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d ||
        dt.getUTCHours() !== h) return null;
    return dt;
  }
  function dtf(lang, opts, zone) {
    var o = {}, k;
    for (k in opts) o[k] = opts[k];
    if (zone) o.timeZone = zone;
    var loc = CAI18N.locale;
    if (o.hour && loc && (loc.time_format === '12' || loc.time_format === '24')) o.hour12 = loc.time_format === '12';
    try { return new Intl.DateTimeFormat(IL(lang), o); } catch (e) { return null; }
  }
  // Format an instant (Date, number or ISO string) per timeZone(); null when it is not a valid instant.
  function fmtInstant(d, lang, opts) {
    if (!(d instanceof Date) && typeof d !== 'number' && typeof d !== 'string') return null; // null would be the epoch
    var inst = toDate(d);
    if (isNaN(inst.getTime())) return null;
    var tz = timeZone(), f = null, date = inst;
    if (tz && tz !== 'local') f = dtf(lang, opts, tz);          // an unknown zone id falls through to the wall clock
    if (!f && tz === 'local') f = dtf(lang, opts, null);
    if (!f && typeof d === 'string') {
      var wall = wallClock(d);
      if (wall) { date = wall; f = dtf(lang, opts, 'UTC'); }
    }
    if (!f) f = dtf(lang, opts, null);
    if (!f) return null;
    try { return f.format(date).replace(/[  ]/g, ' '); } catch (e) { return null; }
  }
  function time(d, lang) {
    return fmtInstant(d, lang, { hour: 'numeric', minute: '2-digit' }) || BAD;
  }
  // Same zone as time()/date(): hour labels (night-card axis, chart ticks) follow timeZone(), never the browser zone.
  function hour(d, lang) {
    return fmtInstant(d, lang, { hour: 'numeric' }) || String(toDate(d).getUTCHours());
  }
  // A zone-aware Intl.DateTimeFormat for any options (the one way the pages format an instant they draw themselves,
  // e.g. a chart tick): timeZone() applies as for time(), and the user's 12/24 h setting applies when `hour` is shown.
  // `lang` may be a language code, or a locales array (e.g. [] = the browser locale, ['en-US'] for a stable key).
  function intl(opts, lang) {
    var tz = timeZone(), f = null;
    var arr = Array.isArray(lang);
    var mk = function (zone) {
      if (!arr) return dtf(lang, opts, zone);
      var o = {}, k;
      for (k in opts) o[k] = opts[k];
      if (zone) o.timeZone = zone;
      var loc = CAI18N.locale;
      if (o.hour && loc && (loc.time_format === '12' || loc.time_format === '24')) o.hour12 = loc.time_format === '12';
      try { return new Intl.DateTimeFormat(lang, o); } catch (e) { return null; }
    };
    if (tz && tz !== 'local') f = mk(tz);
    return f || mk(null);
  }
  function date(d, lang) {
    return fmtInstant(d, lang, { month: 'short', day: 'numeric' }) || BAD;
  }
  // A calendar day given by its parts (a record's local day, "2026-11-07"): shown as that day whatever zone the page
  // formats in (a plain Date(y, m, d) would drift a day across the browser/HA zone difference). weekday: also the weekday.
  function calendarDay(y, m, d, lang, weekday) {
    var at = new Date(Date.UTC(y, m - 1, d, 12));
    var f = function (o) { o.timeZone = 'UTC'; return new Intl.DateTimeFormat(IL(lang), o).format(at).replace(/[\u202f\u00a0]/g, ' '); };
    try {
      var s = f({ month: 'short', day: 'numeric' });
      return weekday ? f({ weekday: 'short' }) + ' ' + s : s;   // "Sat Nov 7": the two parts, as the classic wording
    } catch (e) { return BAD; }
  }
  // date() plus the year when the instant is not in the current year (in the display zone): for "last observed" /
  // "fitted" style dates that can be old. date() itself stays "Nov 14" (it is byte-identical to the Python formatter).
  function dateYear(d, lang) {
    var y = fmtInstant(d, 'en', { year: 'numeric' });
    if (y == null || y === fmtInstant(new Date(), 'en', { year: 'numeric' })) return date(d, lang);
    return fmtInstant(d, lang, { year: 'numeric', month: 'short', day: 'numeric' }) || BAD;
  }
  function dateTime(d, lang) {
    return fmtInstant(d, lang, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) || BAD;
  }
  // Python's round(): halves go to the even neighbour (format_duration in i18n.py mirrors this exactly).
  function roundHalfEven(x) {
    var f = Math.floor(x), d = x - f;
    if (d > 0.5) return f + 1;
    if (d < 0.5) return f;
    return f % 2 === 0 ? f : f + 1;
  }
  // Same thresholds as i18n.format_duration: N min / Nh / N.Nh (one decimal, half-even like Python's f"{x:.1f}"),
  // rendered from the common.duration.* messages.
  function duration(seconds, lang) {
    if (typeof seconds !== 'number' || !isFinite(seconds)) return BAD;
    var minutes = Math.max(1, roundHalfEven(seconds / 60));
    if (minutes >= 60 && minutes % 60 === 0) return renderKey('common.duration.hours', { n: minutes / 60 }, lang);
    if (minutes >= 60) {
      return renderKey('common.duration.hours_frac', { n: Number(fixedHalfEven(minutes / 60, 1)) }, lang);
    }
    return renderKey('common.duration.min', { n: minutes }, lang);
  }
  // Items are already-rendered strings or plain numbers (a number is locale formatted, as Python's ListOf does).
  function list(items, style, lang) {
    var arr = (items || []).map(function (i) {
      return typeof i === 'number' ? number(i, undefined, lang) : (typeof i === 'string' ? i : BAD);
    });
    try {
      return new Intl.ListFormat(IL(lang), { type: style === 'or' ? 'disjunction' : 'conjunction', style: 'long' })
        .format(arr);
    } catch (e) { return arr.join(', '); }
  }
  // Internal temperature is Fahrenheit everywhere; the wire carries it that way plus the display unit.
  function tempValue(f, unit, delta) {
    if (unit === 'celsius') return delta ? f * 5 / 9 : (f - 32) * 5 / 9;
    return f;
  }
  function tempText(f, unit, decimals, delta, lang) {
    if (typeof f !== 'number') return BAD;
    var d = typeof decimals === 'number' && Number.isInteger(decimals) && decimals >= 0 && decimals <= 20 ? decimals : 0;
    var v = tempValue(f, unit, delta), symbol = unit === 'celsius' ? '°C' : '°F', lg = L(lang);
    if (!isFinite(v)) return number(v, { useGrouping: false }, lg) + symbol;       // NaN / infinity as Intl prints them
    // English keeps the legacy f-string (round half even on the exact binary value); every other language is Intl.
    if (resolveLanguage(lg) === 'en') return fixedHalfEven(v, d) + symbol;
    return number(v, { minimumFractionDigits: d, maximumFractionDigits: d, useGrouping: false }, lg) + symbol;
  }
  function temp(f, unit, decimals, lang) { return tempText(f, unit, decimals, false, lang); }
  function tempDelta(f, unit, decimals, lang) { return tempText(f, unit, decimals, true, lang); }

  // ---------- typed wire params (same names as the Python side: Temp, TempDelta, Time, Date, ...) ----------
  // Plain strings and numbers pass through; everything else that is not a known typed param is BAD (Python raises
  // in strict mode and shows BAD otherwise).
  function decodeParam(p, lang) {
    if (typeof p === 'string' || typeof p === 'number') return p;
    if (p === null || typeof p !== 'object' || Array.isArray(p) || !p.$t) return BAD;
    try {
      switch (p.$t) {
        case 'temp': return temp(p.f, p.unit, p.d, lang);
        case 'tdelta': return tempDelta(p.f, p.unit, p.d, lang);
        case 'time': return typeof p.ts === 'string' ? time(p.ts, lang) : BAD;
        case 'date': return typeof p.ts === 'string' ? date(p.ts, lang) : BAD;
        case 'dur': return duration(p.s, lang);
        case 'list':
          if (!Array.isArray(p.items)) return BAD;
          return list(p.items.map(function (i) { return decodeParam(i, lang); }), p.style, lang);
        case 'msg': return renderKey(p.key, p.params, lang);
        case 'code': return typeof p.key === 'string' ? renderKey(p.key, null, lang) : BAD;
        default: return BAD;
      }
    } catch (e) { return BAD; }
  }
  function decodeParams(params, lang) {
    var out = {};
    if (params && typeof params === 'object' && !Array.isArray(params)) {
      for (var k in params) {
        if (Object.prototype.hasOwnProperty.call(params, k)) out[k] = decodeParam(params[k], lang);
      }
    }
    return out;
  }

  // ---------- ICU rendering ----------
  function imfClass() {
    var ns = root.IntlMessageFormat;
    return ns && (ns.IntlMessageFormat || (typeof ns === 'function' ? ns : null));
  }
  // The library is missing: plain {name} substitution, never a throw.
  function plainFormat(msg, params) {
    return String(msg).replace(/\{(\w+)\}/g, function (m, n) {
      return params && params[n] != null ? String(params[n]) : m;
    });
  }
  // Python raises for a number/plural argument that is not a number (FormatJS would coerce it); check the same.
  function checkArgTypes(nodes, params) {
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if ((n.type === 2 || n.type === 6) && typeof params[n.value] !== 'number') {
        throw new Error('argument ' + n.value + ' must be a number');
      }
      if (n.options) {
        for (var k in n.options) {
          if (Object.prototype.hasOwnProperty.call(n.options, k)) checkArgTypes(n.options[k].value, params);
        }
      }
    }
  }
  // Render a pattern for a language with already-decoded params; null when it does not parse or format.
  function tryFormat(pattern, params, lang, cacheKey) {
    var IMF = imfClass();
    if (!IMF) return plainFormat(pattern, params);
    var ck = lang + '\u0000' + (cacheKey || pattern);
    var inst = Object.prototype.hasOwnProperty.call(state.compiled, ck) ? state.compiled[ck] : undefined;
    if (inst === undefined) {
      try { inst = new IMF(pattern, lang === PSEUDO ? 'en' : lang); } catch (e) {
        warnOnce('parse:' + ck, 'message does not parse (' + (cacheKey || pattern) + '): ' + e.message);
        inst = null;
      }
      state.compiled[ck] = inst;
    }
    if (!inst) return null;
    try {
      if (typeof inst.getAst === 'function') checkArgTypes(inst.getAst(), params || {});
      return String(inst.format(params || {}));
    } catch (e) {
      warnOnce('fmt:' + ck, 'message does not format (' + (cacheKey || pattern) + '): ' + e.message);
      return null;
    }
  }
  // Render an ICU message string for a language with already-decoded params (test hook; never throws).
  function formatMsg(msg, params, lang) {
    var out = tryFormat(msg, params, lang, null);
    return out === null ? plainFormat(msg, params) : out;
  }
  function lookup(lang, key) {
    var c = state.cat[lang];
    return c && Object.prototype.hasOwnProperty.call(c, key) ? c[key] : undefined;
  }
  // Same fallbacks as Python's _render_msg:
  //   1. a key missing in the language: the English pattern, formatted with the REQUESTED language's number/list/
  //      plural rules;
  //   2. a translation that does not parse or format: the English message, in English;
  //   3. otherwise the key (a key missing in English is a programming error: the tests catch it).
  function renderKey(key, params, lang) {
    lang = L(lang);
    if (typeof key !== 'string' || !key) { warnOnce('key:' + String(key), 'message with no key'); return ''; }
    var msg = lookup(lang, key), src = lang;
    if (msg === undefined && lang !== 'en') { msg = lookup('en', key); src = 'en'; }
    if (msg === undefined) {
      warnOnce('key:' + key, 'missing key ' + key);
      return key;
    }
    var out = tryFormat(msg, decodeParams(params, lang), lang, key + '\u0001' + src);
    if (out !== null) return out;
    var english = lang !== 'en' ? lookup('en', key) : undefined;
    if (english !== undefined) {
      out = tryFormat(english, decodeParams(params, 'en'), 'en', key + '\u0001en');
      if (out !== null) return out;
    }
    return key;
  }

  // ---------- en-XA pseudo-locale (?lang=en-XA only) ----------
  // Generated on the fly from en so a branch can be checked before any translation exists: every letter is accented,
  // text grows ~35% and each message is bracketed. ICU syntax (argument names, plural/select keywords, #, quoting,
  // number skeletons) is left alone; only the literal text is changed. Not a shipped language: it is never listed in
  // index.json, and python never sees it.
  var PSEUDO = 'en-XA';
  var ACCENT = {
    a: 'à', b: 'ƀ', c: 'ç', d: 'ð', e: 'é', f: 'ƒ', g: 'ĝ', h: 'ĥ', i: 'î', j: 'ĵ', k: 'ķ', l: 'ļ', m: 'ɱ', n: 'ñ',
    o: 'ö', p: 'þ', q: 'ǫ', r: 'ŕ', s: 'š', t: 'ţ', u: 'û', v: 'ṽ', w: 'ŵ', x: 'ẋ', y: 'ý', z: 'ž',
    A: 'Â', B: 'Ɓ', C: 'Ç', D: 'Ð', E: 'É', F: 'Ƒ', G: 'Ĝ', H: 'Ĥ', I: 'Î', J: 'Ĵ', K: 'Ķ', L: 'Ļ', M: 'Ṁ', N: 'Ñ',
    O: 'Ö', P: 'Þ', Q: 'Ǫ', R: 'Ŕ', S: 'Š', T: 'Ţ', U: 'Û', V: 'Ṽ', W: 'Ŵ', X: 'Ẋ', Y: 'Ý', Z: 'Ž'
  };
  function pseudoText(t, acc) {
    var out = '';
    for (var i = 0; i < t.length; i++) {
      var c = t.charAt(i);
      if (Object.prototype.hasOwnProperty.call(ACCENT, c)) { out += ACCENT[c]; acc.n++; } else out += c;
    }
    return out;
  }
  // Matching close brace for the '{' at index i (quoted braces do not count); -1 when unbalanced.
  function closeBrace(s, i) {
    var depth = 0;
    for (var k = i; k < s.length; k++) {
      var c = s.charAt(k);
      if (c === "'") {
        var nx = s.charAt(k + 1);
        if (nx === "'") { k++; continue; }
        if (nx === '{' || nx === '}') { var q = s.indexOf("'", k + 2); if (q < 0) return -1; k = q; continue; }
      } else if (c === '{') depth++;
      else if (c === '}') { depth--; if (depth === 0) return k; }
    }
    return -1;
  }
  function pseudoBody(s, acc) {
    var out = '', run = '', i = 0;
    function flush() { if (run) { out += pseudoText(run, acc); run = ''; } }
    while (i < s.length) {
      var c = s.charAt(i);
      if (c === "'") {
        var nx = s.charAt(i + 1);
        if (nx === "'") { flush(); out += "''"; i += 2; continue; }
        if (nx === '{' || nx === '}' || nx === '#') {
          var q = s.indexOf("'", i + 2);
          if (q < 0) q = s.length - 1;
          flush(); out += s.slice(i, q + 1); i = q + 1; continue;
        }
        run += c; i++; continue;
      }
      if (c === '{') {
        var end = closeBrace(s, i);
        if (end < 0) { run += s.slice(i); i = s.length; continue; }
        flush(); out += pseudoArg(s.slice(i + 1, end), acc); i = end + 1; continue;
      }
      if (c === '#') { flush(); out += c; i++; continue; }
      run += c; i++;
    }
    flush();
    return out;
  }
  function pseudoArg(inner, acc) {
    var m = /^(\s*[^,\s]+\s*),(\s*)(plural|select|selectordinal)(\s*),/.exec(inner);
    if (!m) return '{' + inner + '}';
    var out = m[0], rest = inner.slice(m[0].length), i = 0;
    while (i < rest.length) {
      var ob = rest.indexOf('{', i);
      if (ob < 0) { out += rest.slice(i); break; }
      var cb = closeBrace(rest, ob);
      if (cb < 0) { out += rest.slice(i); break; }
      out += rest.slice(i, ob + 1) + pseudoBody(rest.slice(ob + 1, cb), acc) + '}';
      i = cb + 1;
    }
    return '{' + out + '}';
  }
  function pseudoMessage(msg) {
    var acc = { n: 0 };
    var body = pseudoBody(String(msg), acc);
    var pad = acc.n >= 4 ? new Array(Math.ceil(acc.n * 0.35) + 1).join('~') : '';
    return '[' + body + pad + ']';
  }
  function pseudoCatalog(en) {
    var out = {};
    for (var k in en) if (Object.prototype.hasOwnProperty.call(en, k)) out[k] = pseudoMessage(en[k]);
    Object.defineProperty(out, '__loaded', { value: true, enumerable: false });
    return out;
  }

  // ---------- HTML-safe rendering with <bdi> isolates ----------
  // th(): like t() but returns a safe HTML string: the message text is escaped, and every string param (a formatted
  // temperature, a time, a name) is wrapped in <bdi> so it cannot reorder inside right-to-left text. Number params
  // stay numbers (plural selection needs them). Never put th() output anywhere but innerHTML.
  function thRender(key, params, lang) {
    var vals = [], p = {}, k;
    for (k in (params || {})) {
      if (!Object.prototype.hasOwnProperty.call(params, k)) continue;
      var v = params[k];
      if (v != null && typeof v !== 'number' && typeof v !== 'boolean' && (typeof v !== 'object' || !v.$t)) {
        p[k] = '' + vals.length + ''; vals.push(String(v));
      } else p[k] = v;
    }
    var html = esc(renderKey(key, p, lang));
    return html.replace(/(\d+)/g, function (m, n) { return '<bdi>' + esc(vals[+n]) + '</bdi>'; });
  }
  // Fill [data-i18n] (textContent) and [data-i18n-attr="title:key;aria-label:key"] (setAttribute) under root.
  // Never innerHTML. A key with no catalog entry leaves the element's English markup in place.
  function hasKey(key) { return lookup(CAI18N.lang, key) !== undefined || lookup('en', key) !== undefined; }
  function apply(rootEl) {
    var scope = rootEl || (root.document && root.document.documentElement);
    if (!scope || !scope.querySelectorAll) return;
    var els = scope.querySelectorAll('[data-i18n]'), i;
    for (i = 0; i < els.length; i++) {
      var key = els[i].getAttribute('data-i18n');
      if (!hasKey(key)) continue;
      var params = null, raw = els[i].getAttribute('data-i18n-params');
      if (raw) { try { params = JSON.parse(raw); } catch (e) { params = null; } } // {"n":6} for a message with arguments
      els[i].textContent = CAI18N.t(key, params);
    }
    var attrs = scope.querySelectorAll('[data-i18n-attr]');
    attrs = Array.prototype.slice.call(attrs);
    attrs.forEach(function (el) {
      (el.getAttribute('data-i18n-attr') || '').split(';').forEach(function (pair) {
        var ix = pair.indexOf(':');
        if (ix < 1) return;
        var an = pair.slice(0, ix).trim(), ak = pair.slice(ix + 1).trim();
        if (ak && hasKey(ak)) el.setAttribute(an, CAI18N.t(ak));
      });
    });
  }

  // ---------- catalog loading ----------
  function fetchJson(url) {
    if (typeof root.fetch !== 'function') return Promise.resolve(null);
    var ctl = typeof root.AbortController === 'function' ? new root.AbortController() : null;
    var timer = ctl ? root.setTimeout(function () { ctl.abort(); }, FETCH_TIMEOUT_MS) : null;
    return root.fetch(url, ctl ? { signal: ctl.signal } : undefined).then(function (r) {
      if (!r || !r.ok) throw new Error('HTTP ' + (r && r.status));
      return r.json();
    }).then(function (j) {
      if (timer) root.clearTimeout(timer);
      return j;
    }, function (e) {
      if (timer) root.clearTimeout(timer);
      warnOnce('fetch:' + url, 'could not load ' + url + ' (' + (e && e.message) + ')');
      return null;
    });
  }
  function loadLang(lang) {
    if (state.cat[lang] && state.cat[lang].__loaded) return Promise.resolve();
    if (lang === PSEUDO) return loadLang('en').then(function () { state.cat[PSEUDO] = pseudoCatalog(state.cat.en || {}); });
    return Promise.all(state.areas.map(function (a) {
      return fetchJson(state.base + a + '/' + lang + '.json');
    })).then(function (parts) {
      var merged = {}, any = false;
      parts.forEach(function (p) {
        if (p && typeof p === 'object') { any = true; for (var k in p) merged[k] = p[k]; }
      });
      // Marked loaded only when something arrived, so a later init() retries after a failed fetch.
      if (any) Object.defineProperty(merged, '__loaded', { value: true, enumerable: false });
      state.cat[lang] = merged;
      // Any language may be showing this one's patterns as its English fallback: drop every compiled message.
      state.compiled = {};
    });
  }

  function applyDocument(lang) {
    try {
      var el = root.document && root.document.documentElement;
      if (el) {
        el.setAttribute('lang', lang);
        el.setAttribute('dir', RTL_LAYOUT_READY && RTL.indexOf(lang) >= 0 ? 'rtl' : 'ltr');
      }
    } catch (e) { /* no document */ }
  }

  function setLang(lang) {
    CAI18N.lang = lang;
    CAI18N.dir = RTL.indexOf(lang) >= 0 ? 'rtl' : 'ltr';
  }

  // ---------- public API ----------
  var CAI18N = {
    lang: 'en',
    locale: null,
    dir: 'ltr',
    ready: null,
    languages: LANGUAGES.slice(),
    resolveLanguage: resolveLanguage,
    hass: getHass,
    timeZone: timeZone,
    esc: esc,
    iso: iso,
    fmt: {
      number: number, time: time, hour: hour, date: date, dateYear: dateYear, calendarDay: calendarDay, dateTime: dateTime, intl: intl, duration: duration, list: list,
      temp: temp, tempDelta: tempDelta
    },
    t: function (key, params) { return renderKey(key, params, CAI18N.lang); },
    th: function (key, params) { return thRender(key, params, CAI18N.lang); },
    apply: apply,
    has: hasKey,
    // The prose of an API field: its `<field>_msg` wire sibling rendered in the viewer's language when the backend sent
    // one (and the catalog has that key), else the plain prose field the API still carries. The one call-site shape.
    text: function (obj, field) {
      if (!obj || typeof obj !== 'object') return '';
      var w = obj[field + '_msg'];
      if (w && typeof w === 'object' && w.key && hasKey(w.key)) return renderKey(w.key, w.params, CAI18N.lang);
      var v = obj[field];
      return v == null ? '' : String(v);
    },
    pseudo: pseudoMessage,
    // A {key, params} wire object from the API (the Python Msg.to_wire() format). Plain strings pass through.
    tw: function (wire) {
      if (wire == null) return '';
      if (typeof wire !== 'object') return String(wire);
      if (typeof wire.key !== 'string' || !wire.key) return '';
      return renderKey(wire.key, wire.params, CAI18N.lang);
    },
    // Render an ICU message string directly (tests and tools; the pages use t()/tw()).
    format: function (msg, wireParams, lang) {
      lang = L(lang);
      return formatMsg(msg, decodeParams(wireParams, lang), lang);
    },
    // Resolve the language synchronously (so lang/dir are right at once), then load the catalogs. The promise
    // always resolves: a failed fetch leaves English/key fallbacks. May be called again with statusLanguage.
    init: function (opts) {
      opts = opts || {};
      if (opts.base) state.base = opts.base;
      var hass = getHass();
      CAI18N.locale = (hass && hass.locale) || null;
      setLang(pickLanguage(opts.statusLanguage));
      applyDocument(CAI18N.lang);
      CAI18N.ready = fetchJson(state.base + 'index.json').then(function (idx) {
        if (idx && Array.isArray(idx.languages) && idx.languages.length) {
          state.languages = idx.languages.slice();
          CAI18N.languages = idx.languages.slice();
          if (CAI18N.lang !== PSEUDO && state.languages.indexOf(CAI18N.lang) < 0) {
            setLang(resolveLanguage(CAI18N.lang));
            applyDocument(CAI18N.lang);
          }
        }
        if (opts.areas && opts.areas.length) state.areas = opts.areas.slice(); // a page that needs only some areas
        else if (idx && Array.isArray(idx.areas) && idx.areas.length) state.areas = idx.areas.slice();
        var langs = CAI18N.lang === 'en' ? ['en'] : CAI18N.lang === PSEUDO ? [PSEUDO] : ['en', CAI18N.lang];
        return Promise.all(langs.map(loadLang));
      }).then(function () { return CAI18N; }, function () { return CAI18N; });
      return CAI18N.ready;
    },
    // Test hooks (tests/i18n_conformance.mjs): never used by the pages.
    _test: {
      setCatalog: function (lang, obj) {
        state.cat[lang] = Object.assign({}, obj);
        Object.defineProperty(state.cat[lang], '__loaded', { value: true, enumerable: false });
        state.compiled = {};
      },
      setLang: setLang,
      reset: function () { state.cat = {}; state.compiled = {}; state.warned = {}; }
    }
  };

  root.CAI18N = CAI18N;
  // Resolve the language right away so CAI18N.lang is correct even before a page calls init().
  setLang(pickLanguage(null));
  applyDocument(CAI18N.lang);
})(typeof window !== 'undefined' ? window : this);
