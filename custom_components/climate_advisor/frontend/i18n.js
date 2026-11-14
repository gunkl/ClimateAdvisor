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
      // zh, zh-CN, zh-SG, zh-Hans-* -> Simplified. zh-TW / zh-HK / zh-Hant -> en until Traditional ships.
      if (parts.indexOf('hant') >= 0 || parts[1] === 'tw' || parts[1] === 'hk' || parts[1] === 'mo') return 'en';
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

  // ---------- formatters (Intl; the English output of temp/number matches the Python formatters) ----------
  function L(lang) { return lang || CAI18N.lang || 'en'; }

  function number(x, opts, lang) {
    try { return new Intl.NumberFormat(L(lang), opts || {}).format(x); } catch (e) { return String(x); }
  }
  function toDate(d) { return d instanceof Date ? d : new Date(d); }
  function dtf(lang, opts) {
    var o = {}, k;
    for (k in opts) o[k] = opts[k];
    var loc = CAI18N.locale;
    if (o.hour && loc && (loc.time_format === '12' || loc.time_format === '24')) o.hour12 = loc.time_format === '12';
    try { return new Intl.DateTimeFormat(L(lang), o); } catch (e) { return null; }
  }
  function time(d, lang) {
    var f = dtf(lang, { hour: 'numeric', minute: '2-digit' });
    return f ? f.format(toDate(d)) : toDate(d).toISOString().slice(11, 16);
  }
  function date(d, lang) {
    var f = dtf(lang, { month: 'short', day: 'numeric' });
    return f ? f.format(toDate(d)) : toDate(d).toISOString().slice(0, 10);
  }
  function dateTime(d, lang) {
    var f = dtf(lang, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    return f ? f.format(toDate(d)) : toDate(d).toISOString();
  }
  // Python's round(): halves go to the even neighbour (format_duration in i18n.py mirrors this exactly).
  function roundHalfEven(x) {
    var f = Math.floor(x), d = x - f;
    if (d > 0.5) return f + 1;
    if (d < 0.5) return f;
    return f % 2 === 0 ? f : f + 1;
  }
  // Same thresholds as i18n.format_duration: N min / Nh / N.Nh, rendered from the common.duration.* messages.
  function duration(seconds, lang) {
    var minutes = Math.max(1, roundHalfEven((Number(seconds) || 0) / 60));
    if (minutes >= 60 && minutes % 60 === 0) return renderKey('common.duration.hours', { n: minutes / 60 }, lang);
    if (minutes >= 60) {
      return renderKey('common.duration.hours_frac', { n: Number((minutes / 60).toFixed(1)) }, lang);
    }
    return renderKey('common.duration.min', { n: minutes }, lang);
  }
  function list(items, style, lang) {
    var arr = (items || []).map(String);
    try {
      return new Intl.ListFormat(L(lang), { type: style === 'or' ? 'disjunction' : 'conjunction', style: 'long' })
        .format(arr);
    } catch (e) { return arr.join(', '); }
  }
  // Internal temperature is Fahrenheit everywhere; the wire carries it that way plus the display unit.
  function tempValue(f, unit, delta) {
    f = Number(f);
    if (unit === 'celsius') return delta ? f * 5 / 9 : (f - 32) * 5 / 9;
    return f;
  }
  function tempText(f, unit, decimals, delta, lang) {
    var d = decimals || 0;
    var v = tempValue(f, unit, delta);
    var txt = number(v, { minimumFractionDigits: d, maximumFractionDigits: d, useGrouping: false }, lang);
    return txt + (unit === 'celsius' ? '°C' : '°F');
  }
  function temp(f, unit, decimals, lang) { return tempText(f, unit, decimals, false, lang); }
  function tempDelta(f, unit, decimals, lang) { return tempText(f, unit, decimals, true, lang); }

  // ---------- typed wire params (same names as the Python side: Temp, TempDelta, Time, Date, ...) ----------
  function decodeParam(p, lang) {
    if (p === null || typeof p !== 'object' || Array.isArray(p) || !p.$t) return p;
    switch (p.$t) {
      case 'temp': return temp(p.f, p.unit, p.d, lang);
      case 'tdelta': return tempDelta(p.f, p.unit, p.d, lang);
      case 'time': return time(p.ts, lang);
      case 'date': return date(p.ts, lang);
      case 'dur': return duration(p.s, lang);
      case 'list':
        return list((p.items || []).map(function (i) { return String(decodeParam(i, lang)); }), p.style, lang);
      case 'msg': return renderKey(p.key, p.params, lang);
      case 'code': return renderKey(p.key, null, lang);
      default: return p;
    }
  }
  function decodeParams(params, lang) {
    var out = {};
    if (params) {
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
  // The library is missing or the message does not parse: plain {name} substitution, never a throw.
  function plainFormat(msg, params) {
    return String(msg).replace(/\{(\w+)\}/g, function (m, n) {
      return params && params[n] != null ? String(params[n]) : m;
    });
  }
  // Render an ICU message string for a language with already-decoded params.
  function formatMsg(msg, params, lang, cacheKey) {
    var IMF = imfClass();
    if (!IMF) return plainFormat(msg, params);
    var ck = cacheKey ? lang + '\u0000' + cacheKey : null;
    var inst = ck && Object.prototype.hasOwnProperty.call(state.compiled, ck) ? state.compiled[ck] : undefined;
    if (inst === undefined) {
      try { inst = new IMF(msg, lang); } catch (e) {
        warnOnce('parse:' + (cacheKey || msg), 'message does not parse (' + (cacheKey || msg) + '): ' + e.message);
        inst = null;
      }
      if (ck) state.compiled[ck] = inst;
    }
    if (!inst) return plainFormat(msg, params);
    try { return String(inst.format(params || {})); } catch (e) {
      warnOnce('fmt:' + (cacheKey || msg), 'message does not format (' + (cacheKey || msg) + '): ' + e.message);
      return plainFormat(msg, params);
    }
  }
  function lookup(lang, key) {
    var c = state.cat[lang];
    return c && Object.prototype.hasOwnProperty.call(c, key) ? c[key] : undefined;
  }
  // Per-key fallback: requested language, then English. A key missing in English is a programming error (the
  // tests catch it); in production it warns once and shows the key.
  function renderKey(key, params, lang) {
    lang = L(lang);
    var dec = decodeParams(params, lang);
    var msg = lookup(lang, key), useLang = lang;
    if (msg === undefined && lang !== 'en') { msg = lookup('en', key); useLang = 'en'; }
    if (msg === undefined) {
      warnOnce('key:' + key, 'missing key ' + key);
      return key;
    }
    return formatMsg(msg, dec, useLang, key);
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
      Object.keys(state.compiled).forEach(function (ck) {
        if (ck.indexOf(lang + '\u0000') === 0) delete state.compiled[ck];
      });
    });
  }

  function applyDocument(lang) {
    try {
      var el = root.document && root.document.documentElement;
      if (el) {
        el.setAttribute('lang', lang);
        el.setAttribute('dir', RTL.indexOf(lang) >= 0 ? 'rtl' : 'ltr');
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
    esc: esc,
    fmt: {
      number: number, time: time, date: date, dateTime: dateTime, duration: duration, list: list,
      temp: temp, tempDelta: tempDelta
    },
    t: function (key, params) { return renderKey(key, params, CAI18N.lang); },
    // A {key, params} wire object from the API (the Python Msg.to_wire() format). Plain strings pass through.
    tw: function (wire) {
      if (wire == null) return '';
      if (typeof wire !== 'object') return String(wire);
      return renderKey(wire.key, wire.params, CAI18N.lang);
    },
    // Render an ICU message string directly (tests and tools; the pages use t()/tw()).
    format: function (msg, wireParams, lang) {
      lang = L(lang);
      return formatMsg(msg, decodeParams(wireParams, lang), lang, null);
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
          if (state.languages.indexOf(CAI18N.lang) < 0) {
            setLang(resolveLanguage(CAI18N.lang));
            applyDocument(CAI18N.lang);
          }
        }
        if (idx && Array.isArray(idx.areas) && idx.areas.length) state.areas = idx.areas.slice();
        var langs = CAI18N.lang === 'en' ? ['en'] : ['en', CAI18N.lang];
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
